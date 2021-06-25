"""
Module to interact with Ansible, perform ansible-playbook and extract metadata from Ansible vars

A sample configuration:

- hosts: all
  vars:
    ansible_bender:

      base_image: fedora:28

      target_image:
        name: asdqwe
        # configure environment variables: same name as ansible
        environment:
          X: Y
        labels:
          key: value
        # default working directory
        working_dir: /path

      working_container:
        user: 12345  # TODO
        volumes:
        - { src: /path/to/my/code, dest: /src:Z }

tasks:
  ...

"""
import copy
import datetime
import importlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import jsonschema
import yaml

import ansible_bender
from ansible_bender import callback_plugins
from ansible_bender.conf import ImageMetadata, Build
from ansible_bender.constants import TIMESTAMP_FORMAT, TIMESTAMP_FORMAT_TOGETHER
from ansible_bender.exceptions import ABBuildUnsuccesful, ABValidationError
from ansible_bender.schema import PLAYBOOK_SCHEMA
from ansible_bender.utils import run_cmd, ap_command_exists, random_str, graceful_get, \
    is_ansibles_python_2

logger = logging.getLogger(__name__)
A_CFG_TEMPLATE = """\
[defaults]
# when user is changed, ansible might not be able to write to /.ansible
remote_tmp = /tmp
callback_plugins={0}
callback_whitelist=snapshoter\n
"""


def run_playbook(playbook_path, inventory_path, a_cfg_path, connection, extra_variables=None,
                 ansible_args=None, debug=False, environment=None, try_unshare=True,
                 provide_output=True, log_stderr=False):
    """
    run selected ansible playbook and return output from ansible-playbook run

    :param playbook_path:
    :param inventory_path:
    :param a_cfg_path:
    :param connection:
    :param extra_variables:
    :param ansible_args: list of str, extra arguments for a-p
    :param debug:
    :param environment:
    :param try_unshare: bool, do `buildah unshare` if uid > 0
    :param provide_output: bool, present output to user
    :param log_stderr: bool, log errors coming from stderr to our logger

    :return: output
    """
    ap = ap_command_exists()
    if is_ansibles_python_2(ap):
        # I just realized it could work, we would just had to disable the
        # callback plugin: no caching and layering
        raise RuntimeError(
            "ansible-bender is written in python 3 and does not work in python 2,\n"
            f"it seems that {ap} is using python 2 - ansible-bender will not"
            "work in such environment\n"
        )
    cmd_args = [
        ap,
        "-c", connection,

    ]
    if inventory_path:
        cmd_args += ["-i", inventory_path]
    if debug:
        cmd_args += ["-vvv"]
    if extra_variables:
        cmd_args += ["--extra-vars"] + \
                    [" ".join(
                        ["{}={}".format(k, v)
                         for k, v in extra_variables.items()]
                    )]
    if ansible_args:
        cmd_args += ansible_args
    cmd_args += [playbook_path]
    logger.debug("%s", " ".join(cmd_args))

    env = os.environ.copy()
    env["ANSIBLE_RETRY_FILES_ENABLED"] = "0"
    if debug:
        env["ANSIBLE_STDOUT_CALLBACK"] = "debug"
    if environment:
        env.update(environment)
    if a_cfg_path:
        env["ANSIBLE_CONFIG"] = a_cfg_path

    if try_unshare and os.getuid() != 0:
        logger.info("we are running rootless, prepending `buildah unshare`")
        # rootless, we need to `buildah unshare` for sake of `buildah mount`
        # https://github.com/containers/buildah/issues/1271
        # the need for `--` https://github.com/containers/buildah/issues/1374
        cmd_args = ["buildah", "unshare", "--"] + cmd_args

    # ansible has no official python API, the API they have is internal and said to break compat
    try:
        return run_cmd(
            cmd_args,
            print_output=provide_output,
            save_output_in_exc=True,
            env=env,
            return_all_output=provide_output,
            log_stderr=log_stderr,
        )
    except subprocess.CalledProcessError as ex:
        raise ABBuildUnsuccesful("ansible-playbook execution failed: %s" % ex, ex.output)


class AnsibleRunner:
    """
    Run ansible on provided artifact using the Builder interface
    """

    def __init__(self, playbook_path, builder, build, debug=False):
        self.build_i = build
        self.pb = playbook_path
        self.builder = builder
        self.debug = debug

    def _create_inventory_file(self, fd, python_interpreter):
        fd.write(
            '%s ansible_connection="%s" ansible_python_interpreter="%s"\n' % (
                self.builder.ansible_host,
                self.builder.ansible_connection,
                python_interpreter
            )
        )

    def _create_ansible_cfg(self, fd):
        callback_plugins_dir = os.path.dirname(callback_plugins.__file__)
        fd.write(A_CFG_TEMPLATE.format(callback_plugins_dir))

    def _get_path_our_site(self):
        """ return a path to a directory which contains ansible_bender installation """
        # pip in Fedora installs to /usr/local which is on default pythonpath
        # but when ansible invokes the callback plugin, that directory is not on sys.path: wat?
        # hence, let's add the site ab is installed in to sys.path
        return os.path.dirname(os.path.dirname(ansible_bender.__file__))

    def build(self, db_path):
        """
        run the playbook against the container

        :param db_path, str, path to ab's database

        :return: str, output
        """
        tmp = tempfile.mkdtemp(prefix="ab")

        pythonpath = os.environ.get("PYTHONPATH", "")
        site_path = self._get_path_our_site()
        if pythonpath:
            pythonpath = f"{site_path}:{pythonpath}"
        else:
            pythonpath = site_path

        try:
            environment = {
                "AB_BUILD_ID": self.build_i.build_id,
                "AB_DB_PATH": db_path,
                "PYTHONPATH": pythonpath,  # TODO write an e2e test for this
            }
            inv_path = os.path.join(tmp, "inventory")
            logger.info("creating inventory file %s", inv_path)
            with open(inv_path, "w") as fd:
                self._create_inventory_file(fd, self.build_i.python_interpreter)
            a_cfg_path = os.path.join(tmp, "ansible.cfg")
            with open(a_cfg_path, "w") as fd:
                self._create_ansible_cfg(fd)

            tmp_pb_path = os.path.join(tmp, "p.yaml")
            with open(self.pb, "r") as fd_r:
                pb_dict = yaml.safe_load(fd_r)
                host_present = False
            for idx, doc in enumerate(pb_dict):
                try:
                    host = doc["hosts"]
                    host_present = True
                except:
                    continue
                finally:
                    if host_present:
                    logger.debug("play[%s], host = %s", idx, host)
                    doc["hosts"] = self.builder.ansible_host
                    break
            with open(tmp_pb_path, "w") as fd:
                yaml.safe_dump(pb_dict, fd)
            playbook_base = os.path.basename(self.pb).split(".", 1)[0]
            timestamp = datetime.datetime.now().strftime(TIMESTAMP_FORMAT)
            symlink_name = f".{playbook_base}-{timestamp}-{random_str()}.yaml"
            playbook_dir = os.path.dirname(self.pb)
            symlink_path = os.path.join(playbook_dir, symlink_name)
            os.symlink(tmp_pb_path, symlink_path)

            extra_args = None
            try:
                if self.build_i.ansible_extra_args:
                    extra_args = shlex.split(self.build_i.ansible_extra_args)
                return run_playbook(
                    symlink_path, inv_path, a_cfg_path, self.builder.ansible_connection,
                    debug=self.debug, environment=environment, ansible_args=extra_args
                )
            finally:
                os.unlink(symlink_path)
        finally:
            shutil.rmtree(tmp)


class PbVarsParser:
    def __init__(self, playbook_path):
        """
        :param playbook_path: str, path to playbook
        """
        self.playbook_path = playbook_path
        self.build = Build()
        self.metadata = ImageMetadata()
        self.build.metadata = self.metadata

    def _check_selinux_iz_gud(self):
        """
        This is a workaround for a weird behavior of ansible: if selinux is
        in the enforcing mode and python3-libselinux is not installed, ansible freezes

        https://bugzilla.redhat.com/show_bug.cgi?id=1696706
        :return:
        """
        try:
            enforcing_status = Path("/sys/fs/selinux/enforce").read_text()
        except FileNotFoundError:
            logger.debug("this system is not using selinux, /sys/fs/selinux/enforce is not present")
            return
        logger.debug(f"selinux enforce status = {enforcing_status}")
        # it can be enforcing or not, selinux python module needs to be present
        try:
            importlib.import_module("selinux")
        except ModuleNotFoundError:
            raise RuntimeError(
                "\nThis system is using selinux(8) and selinux python module is not installed. "
                "There is a known issue in ansible that it freezes in this setup:\n"
                "  https://bugzilla.redhat.com/show_bug.cgi?id=1696706\n"
                "Please install libselinux python bindings (on Fedora the package name is python3-libselinux)."
            )

    def expand_pb_vars(self):
        """
        populate vars from a playbook, defined in vars section

        :return: dict with the content of ansible_bender var
        """
        self._check_selinux_iz_gud()
        with open(self.playbook_path) as fd:
            plays = yaml.safe_load(fd)

        for play in plays[1:]:
            bender_vars = graceful_get(play, "vars", "ansible_bender")
            if bender_vars:
                logger.warning("Variables are loaded only from the first play.")

        try:
            # we care only about the first play, we don't want to merge dicts
            d = plays[0]
        except IndexError:
            raise RuntimeError("Invalid playbook, can't access the first document.")

        bender_vars = graceful_get(d, "vars", "ansible_bender")
        if not bender_vars:
            logger.info("no bender data found in the playbook")
            return {}

        tmp = tempfile.mkdtemp(prefix="ab")
        json_data_path = os.path.join(tmp, "j.json")

        # we cannot use "vars" variable because the variables are not expanded in there
        pb_vars = copy.deepcopy(d["vars"])
        while True:
            # just in case the variable is already defined
            timestamp = datetime.datetime.now().strftime(TIMESTAMP_FORMAT_TOGETHER)
            ab_vars_key = f"ab_vars_{timestamp}"
            if ab_vars_key not in pb_vars:
                logger.debug("ab vars key = %s", ab_vars_key)
                pb_vars[ab_vars_key] = d["vars"]
                break
        jinja_pb_vars_key = '{{ %s }}' % ab_vars_key
        pb = {
            "name": "Let Ansible expand variables",
            "hosts": "localhost",
            "vars": pb_vars,
            "vars_files": d.get("vars_files", []),
            "gather_facts": False,
            "tasks": [
                {"debug": {"msg": jinja_pb_vars_key}},
                {
                    "copy": {
                        "dest": json_data_path,
                        "content": jinja_pb_vars_key
                    }
                }
            ]
        }

        i_path = os.path.join(tmp, "i")
        with open(i_path, "w") as fd:
            fd.write("localhost ansible_connection=local")

        # json is easier to parse than yaml
        tmp_pb_path = os.path.join(tmp, "p.json")
        with open(tmp_pb_path, "w") as fd:
            json.dump([pb], fd)

        playbook_base = os.path.basename(self.playbook_path).split(".", 1)[0]
        symlink_name = f".{playbook_base}-{timestamp}-{random_str()}.yaml"
        playbook_dir = os.path.dirname(self.playbook_path)
        symlink_path = os.path.join(playbook_dir, symlink_name)
        os.symlink(tmp_pb_path, symlink_path)

        # yeah, ansible is not very smart for connection=local
        args = ["-e", f"ansible_python_interpreter={sys.executable}"]

        try:
            run_playbook(symlink_path, i_path, None, connection="local", try_unshare=False,
                         provide_output=False, log_stderr=True, ansible_args=args)

            with open(json_data_path) as fd:
                return json.load(fd)["ansible_bender"]
        finally:
            os.unlink(symlink_path)
            shutil.rmtree(tmp)

    def process_pb_vars(self, bender_data):
        """
        accept variables from the playbook and update the Build and ImageMetadata objects with them

        :param bender_data: dict with the content of ansible_bender vars
        :return:
        """
        if not bender_data:
            return
        try:
            # validation to error out unknown keys in /vars/ansible_bender
            jsonschema.validate(bender_data, PLAYBOOK_SCHEMA)
        except jsonschema.ValidationError as validation_error:
            if validation_error.validator == "type":
                # error is due to invalid value datatype
                path = "/" + "/".join(validation_error.path)
                expected_types = validation_error.validator_value
                if isinstance(validation_error.validator_value, list):
                    expected_types = ", ".join(validation_error.validator_value)
                message = f"variable {path} is set to {validation_error.instance}" \
                          f", which is not of type {expected_types}"
                raise ABValidationError(message) from validation_error
            else:
                # error is due to absence of a required key, unknown keys playbook or any other kind
                raise ABValidationError(validation_error.message) from validation_error

        self.metadata.update_from_configuration(bender_data.get("target_image", {}))
        self.build.update_from_configuration(bender_data)

    def get_build_and_metadata(self):
        """
        extra vars from the selected playbook

        :return: Build(), ImageMetadata()
        """
        bender_data = self.expand_pb_vars()

        self.process_pb_vars(bender_data)

        return self.build, self.metadata
