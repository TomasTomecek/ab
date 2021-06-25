import hashlib
import json
import logging
import os
import traceback
import pathlib
import gc

from ansible.executor.task_result import TaskResult
from ansible.playbook.task import Task
from ansible.plugins.callback import CallbackBase
from ansible.template import Templar

from ansible_bender.api import Application
from ansible_bender.builders.base import BuildState
from ansible_bender.constants import NO_CACHE_TAG

FILE_ACTIONS = ["file", "copy", "synchronize", "unarchive", "template"]
logger = logging.getLogger("ansible_bender")


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'hard-worker'
    CALLBACK_NAME = 'a_container_image_snapshoter'
    CALLBACK_NEEDS_WHITELIST = True

    def _get_app_and_build(self):
        build_id = os.environ["AB_BUILD_ID"]
        db_path = os.environ["AB_DB_PATH"]
        app = Application(init_logging=False, db_path=db_path)
        build = app.get_build(build_id)
        app.set_logging(debug=build.debug, verbose=build.verbose)
        return app, build

    def _snapshot(self, task_result):
        """
        snapshot the target container

        :param task_result: instance of TaskResult
        """
        if task_result._task.action in ["setup", "gather_facts", "include_role", "include_tasks"]:
            # we ignore setup
            # for include_role and include_tasks
            # ignore the parsing task and only cache following tasks in included file
            return
        if task_result.is_failed() or task_result._result.get("rc", 0) > 0:
            return
        a, build = self._get_app_and_build()
        if build.is_failed():
            return
        if "stop-layering" in getattr(task_result._task, "tags", []):
            build.stop_layering()
            a.db.record_build(build)
            self._display.display("detected tag 'stop-layering', tasks won't be cached nor layered any more")
            return
        if not build.is_layering_on():
            return
        if task_result.is_skipped() or getattr(task_result, "_result", {}).get("skip_reason", False):
            a.record_progress(None, self._task_content, None, build_id=build.build_id)
            return
        # # alternatively, we can guess it's a file action and do getattr(task, "src")
        # # most of the time ansible says changed=True even when the file is the same
        if task_result._task.action in FILE_ACTIONS:
            if not task_result.is_changed():
                status = a.maybe_load_from_cache(self._task_content, build_id=build.build_id)
                if status:
                    self._display.display("loaded from cache: '%s'" % status)
                    return
        image_name = a.cache_task_result(self._task_content, build)
        if image_name:
            self._display.display("caching the task result in an image '%s'" % image_name)

    @staticmethod
    def get_task_content(task: Task):

        def get_templated_ds(task: Task):
            # To get the correct fingerprint of a task,
            # we need to do the jinja2 templating for its ds.
            # The untemplated ds, if containing vars, will stay the same even if values of vars changed.
            #
            # We use ansible.template.Templar to help us do the templating,
            # similar to what is done in ansible.executor.task_executor.
            # However, the params needed by Templar (loader, shared_loader_obj, variables)
            # are not easily-obtainable via Task object.
            #
            # The hacky solution to this is to use ansible.executor.process.worker.WorkerProcess obj.
            # These params can be found in this obj.
            # The WorkerProcess obj references the Task obj while running,
            # so we can use gc.get_referrers method to find the WorkerProcess obj based on Task obj.
            try:
                required_fields = ["_loader", "_shared_loader_obj", "_task_vars"]
                worker_process_dict = None
                for referrer in gc.get_referrers(task):
                    if isinstance(referrer, dict) and all([field in referrer for field in required_fields]):
                        # Found the WorkerProcess obj referencing this Task obj.
                        worker_process_dict = referrer
                        break
                templar = Templar(
                    loader=worker_process_dict["_loader"],
                    shared_loader_obj=worker_process_dict["_shared_loader_obj"],
                    variables=worker_process_dict["_task_vars"],
                )
                return templar.template(task.get_ds())
            except:
                return task.get_ds()

        sha512 = hashlib.sha512()
        serialized_data = get_templated_ds(task)
        if not serialized_data:
            # ansible 2.8
            serialized_data = task.dump_attrs()
        if not serialized_data:
            logger.error("unable to obtain task content from ansible: caching will not work")
            return
        c = json.dumps(serialized_data, sort_keys=True)

        logger.debug("content = %s", c)
        sha512.update(c.encode("utf-8"))

        # If task is a file action, cache the src.
        #
        # Take the file stats of the src (if directory, get the stats
        # of every file within) and concatinate it with the task config
        # (assigned under serialized_data)
        #
        # The idea is that, if a file is changed, so will its modification time,
        # which will force the layer to be reloaded. Otherwise, load from cache.
        #
        # Note: serialized_data was grabbed above.
        task_config = task.dump_attrs()
        if( ('args' in task_config) and ('src' in task_config['args']) ):
            src = task_config['args']['src']
            src_path = os.path.join(task.get_search_path()[0], "files", src)

            if(not(os.path.exists(src_path))):
                src_path = os.path.join(task.get_search_path()[0], src)

            if os.path.isdir(src_path):
                dir_hash = CallbackModule.get_dir_fingerprint(src_path)
                sha512.update(dir_hash.encode("utf-8"))
            elif os.path.isfile(src_path):
                the_file = pathlib.Path(src_path)
                date_modified = str(the_file.stat().st_mtime)
                sha512.update(date_modified.encode("utf-8"))

        return sha512.hexdigest()

    @staticmethod
    def get_dir_fingerprint(directory):
        sha512 = hashlib.sha512()
        for root, dirs, files in os.walk(directory):
            for filename in files:
                the_file = pathlib.Path(os.path.join(directory, root, filename))

                if not the_file.exists():
                    continue

                date_modified = str(the_file.stat().st_mtime)
                sha512.update(date_modified.encode("utf-8"))

        return sha512.hexdigest()

    def _maybe_load_from_cache(self, task):
        """
        load image state from cache

        :param task: instance of Task
        """
        if task.action in ["setup", "gather_facts", "include_role", "include_tasks"]:
            # we ignore setup
            # for include_role and include_tasks
            # ignore the parsing task and only cache following tasks in included file
            return
        a, build = self._get_app_and_build()
        if build.is_failed():
            # build failed, skip the task
            task.when = "0"  # skip
            return
        if "stop-layering" in getattr(task, "tags", []):
            build.stop_layering()
            a.db.record_build(build)
            return
        if NO_CACHE_TAG in getattr(task, "tags", []):
            self._display.display("detected tag '%s': won't load from cache from now" % NO_CACHE_TAG)
            build.cache_tasks = False
            a.db.record_build(build)
            return
        if not build.was_last_layer_cached():
            return
        if not build.is_layering_on():
            return
        logger.debug("hash = %s", self._task_content)
        status = a.maybe_load_from_cache(self._task_content, build_id=build.build_id)
        if status:
            self._display.display("loaded from cache: '%s'" % status)
            task.when = "0"  # skip

    def abort_build(self):
        logger.debug("%s", traceback.format_exc())
        a, build = self._get_app_and_build()
        a.db.record_build(build, build_state=BuildState.FAILED)

    def v2_runner_on_start(self, host, task):
        self._task_content = self.get_task_content(task)
        try:
            return self._maybe_load_from_cache(task)
        except Exception as ex:
            logger.error("error while running the build: %s", ex)
            self.abort_build()

    def runner_on_ok_or_skipped(self, *args, **kwargs):
        try:
            first_arg = args[0]
        except IndexError:
            return
        if isinstance(first_arg, TaskResult):
            try:
                return self._snapshot(first_arg)
            except Exception as ex:
                logger.error("error while running the build: %s", ex)
                self.abort_build()

    def v2_runner_on_ok(self, *args, **kwargs):
        self.runner_on_ok_or_skipped(*args, **kwargs)

    def v2_runner_on_skipped(self, *args, **kwargs):
        self.runner_on_ok_or_skipped(*args, **kwargs)
