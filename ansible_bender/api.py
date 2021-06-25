import datetime
import logging
import os
import sys
from typing import Tuple, List

from ansible_bender.builder import get_builder
from ansible_bender.builders.base import BuildState
from ansible_bender.conf import Build
from ansible_bender.constants import OUT_LOGGER, OUT_LOGGER_FORMAT, TIMESTAMP_FORMAT
from ansible_bender.core import AnsibleRunner
from ansible_bender.db import Database
from ansible_bender.exceptions import ABBuildUnsuccesful
from ansible_bender.utils import set_logging


logger = logging.getLogger(__name__)
out_logger = logging.getLogger(OUT_LOGGER)


class Application:
    def __init__(self, debug=False, db_path=None, verbose=False, init_logging=True):
        """
        :param debug: bool, provide debug output if True
        :param db_path: str, path to json file where the database stores the data persistently
        :param verbose: bool, print verbose output
        :param init_logging: bool, set up logging if True
        """
        if init_logging:
            self.set_logging(debug=debug, verbose=verbose)
        self.verbose = verbose
        self.debug = debug
        self.db = Database(db_path=db_path)
        self.db_path = self.db.db_root_path

    @staticmethod
    def set_logging(debug: bool = False, verbose: bool = False):
        """ configure logging """
        if debug:
            set_logging(level=logging.DEBUG)
        elif verbose:
            set_logging(level=logging.INFO)
            set_logging(logger_name=OUT_LOGGER, level=logging.INFO, format=OUT_LOGGER_FORMAT,
                        handler_kwargs={"stream": sys.stdout})
        else:
            set_logging(level=logging.WARNING)
            set_logging(logger_name=OUT_LOGGER, level=logging.INFO, format=OUT_LOGGER_FORMAT,
                        handler_kwargs={"stream": sys.stdout})

    def build(self, build: Build):
        """
        build container image

        :param build: instance of Build
        """
        if not os.path.isfile(build.playbook_path):
            raise RuntimeError("No such file or directory: %s" % build.playbook_path)

        build.validate()
        build.metadata.validate()

        build.debug = self.debug
        build.verbose = self.verbose

        # we have to record as soon as possible
        self.db.record_build(build)

        try:
            builder = self.get_builder(build)
            builder.sanity_check()

            # before we start messing with the base image, we need to check for its presence first
            if not builder.is_base_image_present():
                builder.pull()
                build.pulled = True

            builder.check_container_creation()

            # let's record base image as a first layer
            base_image_id = builder.get_image_id(build.base_image)
            build.record_layer(None, base_image_id, None, cached=True)

            a_runner = AnsibleRunner(build.playbook_path, builder, build, debug=self.debug)

            # we are about to perform the build
            build.build_start_time = datetime.datetime.now()
            self.db.record_build(build, build_state=BuildState.IN_PROGRESS)

            if not build.python_interpreter:
                build.python_interpreter = builder.find_python_interpreter()

            builder.create()
        except Exception:
            self.db.record_build(
                None,
                build_id=build.build_id,
                build_state=BuildState.FAILED,
                set_finish_time=True
            )
            raise

        try:
            try:
                output = a_runner.build(self.db_path)
            except ABBuildUnsuccesful as ex:
                b = self.db.record_build(None, build_id=build.build_id,
                                         build_state=BuildState.FAILED,
                                         set_finish_time=True)
                b.log_lines = ex.output.split("\n")
                self.db.record_build(b)
                timestamp = datetime.datetime.now().strftime(TIMESTAMP_FORMAT)
                image_name = build.target_image + "-" + timestamp + "-failed"
                b.target_image = image_name
                image_id = builder.commit(image_name)
                b.final_layer_id = image_id
                self.record_progress(b, None, image_id)
                out_logger.info("Image build failed /o\\")
                out_logger.info("The progress is saved into image '%s'", image_name)
                raise

            b = self.db.record_build(None, build_id=build.build_id, build_state=BuildState.DONE,
                                     set_finish_time=True)
            b.log_lines = output
            # commit the final image and apply all metadata
            b.final_layer_id = builder.commit(build.target_image, final_image=True)

            if not b.is_layering_on():
                if b.squash:
                    # reset layers if squashing
                    b.layers = []
                    # we could also skip committing when squashing but we would lose caching
                self.record_progress(b, None, b.final_layer_id)
            else:
                self.db.record_build(b)

            out_logger.info("Image '%s' was built successfully \\o/",  build.target_image)
        finally:
            builder.clean()

    def get_build(self, build_id: str = None) -> Build:
        """
        get selected build or latest build if build_id is None

        :param build_id: str or None
        :return: build
        """
        if build_id is None:
            return self.db.get_latest_build()
        return self.db.get_build(build_id)

    def get_logs(self, build_id: str = None) -> List[str]:
        """
        get logs for a specific build, if build_id is not, select the latest build

        :param build_id: str or None
        :return: list of str
        """
        build = self.get_build(build_id=build_id)
        return build.log_lines

    def list_builds(self) -> List[Build]:
        return self.db.load_builds()

    def inspect(self, build_id: str = None):
        """
        provide detailed information about the selected build

        :param build_id: str or None
        :return: dict
        """
        build = self.get_build(build_id=build_id)
        di = build.to_dict()
        del di["log_lines"]  # we have a dedicated command for that
        del di["layer_index"]  # internal info
        return di

    def push(self, target, build_id: str = None, force: bool = False):
        """
        push built image into a remote location, this method raises an exception when:
         * the push failed or the image can't be found
         * the build haven't finished yet

        :param target: str, transport:details
        :param build_id: id of the build or None
        :param force: bool, bypass checks if True
        :return: None
        """
        build = self.get_build(build_id=build_id)
        builder = self.get_builder(build)
        builder.push(build, target, force=force)

    def get_builder(self, build: Build):
        return get_builder(build.builder_name)(build, debug=self.debug)

    def maybe_load_from_cache(self, content: str, build_id: str) -> str:
        if not content:
            return

        build = self.db.get_build(build_id)
        builder = self.get_builder(build)

        if not build.cache_tasks:
            return

        base_image_id, layer_id = self.record_progress(build, content, None)
        builder.swap_working_container()
        return layer_id

    def get_layer(self, content: str, base_image_id: str) -> str:
        """
        provide a layer for given content and base_image_id; if there
        is such layer in cache store, return its layer_id

        :param content:
        :param base_image_id:
        :return:
        """
        return self.db.get_cached_layer(content, base_image_id)

    def record_progress(self, build: Build, content: str, layer_id: str, build_id: str = None) -> Tuple[str, str]:
        """
        record build progress to the database

        :param build:
        :param content: str or None
        :param layer_id:
        :param build_id:
        :return:
        """
        if build_id:
            build = self.db.get_build(build_id)
        base_image_id = build.get_top_layer_id()
        was_cached = False
        if not layer_id:
            # skipped task, it was cached
            if content:
                layer_id = self.get_layer(content, base_image_id)
                builder = self.get_builder(build)
                if not builder.is_image_present(layer_id):
                    logger.info("layer %s for content %s does not exist", layer_id, content)
                    layer_id = None
            if not layer_id:
                return None, None
            was_cached = True
        build.record_layer(content, layer_id, base_image_id, cached=was_cached)
        self.db.record_build(build)
        return base_image_id, layer_id

    def create_new_layer(self, content: str, build: Build) -> Tuple[str, str, str]:
        """
        create new layer from the current state of the container of specified build

        :param content: task as a str
        :param build: Build instance
        :return:
        """
        builder = self.get_builder(build)
        image_name = ""
        if build.verbose_layer_names:
            timestamp = datetime.datetime.now().strftime(TIMESTAMP_FORMAT)
            image_name = "%s-%s" % (build.target_image, timestamp)
            # buildah doesn't accept upper case
            image_name = image_name.lower()
        layer_id = builder.commit(image_name, print_output=False)
        base_image_id, _ = self.record_progress(build, content, layer_id)
        return image_name, layer_id, base_image_id

    def cache_task_result(self, content: str, build: Build) -> str:
        """ snapshot the container after a task was executed """
        if not content:
            logger.info("no content provided, will not cache this layer")
            return
        image_name, layer_id, base_image_id = self.create_new_layer(content, build)
        if not build.cache_tasks:  # actually we could still cache results
            return
        self.db.save_layer(layer_id, base_image_id, content)
        return image_name

    def clean(self):
        self.db.release()

    def remove_build(self, build_id: str):
        self.db.delete_build(build_id)
