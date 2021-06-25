"""
Make sure that we can build using buildah builder.
"""
import json
import logging
import os
import re
import subprocess
from pathlib import Path

import pytest

from ansible_bender.builders.buildah_builder import buildah, inspect_resource, \
    podman_run_cmd
from ..conftest import ab
from ..spellbook import basic_playbook_path, base_image, bad_playbook_path, random_word, basic_playbook_path_w_bv

logger = logging.getLogger("ansible_bender")


def test_output(target_image, tmpdir):
    cmd = ["build", basic_playbook_path, base_image, target_image]
    out = ab(cmd, str(tmpdir), return_output=True)
    assert f"Image '{target_image}' was built successfully \\o/" in out
    assert 'Getting image source signatures' in out
    assert not re.match(r'ERROR\s+Getting image source signatures', out)
    assert 'Copying ' in out
    assert not re.match(r'ERROR\s+Copying ', out)
    buildah("inspect", ["-t", "image", target_image])


def test_build_basic_image(target_image, tmpdir):
    cmd = ["build", basic_playbook_path, base_image, target_image]
    ab(cmd, str(tmpdir))

    p_inspect_data = json.loads(subprocess.check_output(["podman", "inspect", "-t", "image", target_image]))[0]
    image_id = p_inspect_data["Id"]

    cmd = ["inspect", "--json"]
    ab_inspect_data = json.loads(ab(cmd, str(tmpdir), return_output=True))
    top_layer_id = ab_inspect_data["layers"][-1]["layer_id"]
    final_image_id = ab_inspect_data["final_layer_id"]

    # these two are really different: top is w/o metadata
    assert image_id != top_layer_id
    assert image_id == final_image_id


def test_build_basic_image_with_env_vars(tmpdir, target_image):
    a_b = "A=B"
    x_y = "X=Y"
    cmd = ["build", "-e", a_b, x_y, "--",
           basic_playbook_path, base_image, target_image]
    ab(cmd, str(tmpdir))
    out = inspect_resource("image", target_image)
    assert a_b in out["OCIv1"]["config"]["Env"]
    assert x_y in out["OCIv1"]["config"]["Env"]
    e = podman_run_cmd(target_image, ["env"], return_output=True)
    assert a_b in e
    assert x_y in e


def test_build_basic_image_with_wrong_env_vars(tmpdir, target_image):
    la_la_la = "AB-LA-BLA-LA-LA-BLA"
    cmd = ["build", "-e", la_la_la, "--", basic_playbook_path, base_image, target_image]
    with pytest.raises(subprocess.CalledProcessError) as exc:
        ab(cmd, str(tmpdir))
        e = ("There was an error during execution: "
             "Environment variable {} doesn't seem to be "
             "specified in format 'KEY=VALUE'.".format(la_la_la))
        assert e in exc.value.message


def test_build_basic_image_with_labels(tmpdir, target_image):
    a_b = "A=B"
    x_y = "x=y"
    cmd = ["build", "-l", a_b, x_y, "--",
           basic_playbook_path, base_image, target_image]
    ab(cmd, str(tmpdir))
    out = inspect_resource("image", target_image)
    assert out["OCIv1"]["config"]["Labels"]["A"] == "B"
    assert out["OCIv1"]["config"]["Labels"]["x"] == "y"


def test_build_basic_image_with_build_volumes(tmpdir, target_image):
    real_tmp = str(tmpdir)
    with open(os.path.join(real_tmp, "file.txt"), "w") as fd:
        fd.write("Hello, hello!")
    container_mount = "/asdqwe"
    vol_spec = "%s:%s" % (real_tmp, container_mount)
    cmd = ["build", "--build-volumes", vol_spec, "--",
           basic_playbook_path_w_bv, base_image, target_image]
    ab(cmd, str(tmpdir))

    cmd = ["inspect", "--json"]
    ab_inspect_data = json.loads(ab(cmd, str(tmpdir), return_output=True))
    build_volumes = ab_inspect_data["build_volumes"]

    assert vol_spec in build_volumes


def test_build_basic_image_with_all_params(tmpdir, target_image):
    workdir_path = "/etc"
    l_a_b = "A=B"
    l_x_y = "x=y"
    e_a_b = "A=B"
    e_x_y = "X=Y"
    ann = "bohemian=rhapsody"
    cmd, cmd_e = "ls -lha", ["ls", "-lha"]
    entrypoint, entrypoint_e = "ls -lha", ["/bin/sh", "-c", "ls -lha"]
    # FIXME: this doesn't work with user namespaces
    # user = "1000"
    p_80, p_443 = "80", "443"
    runtime_volume = "/var/lib/asdqwe"
    cmd = ["build",
           "-w", workdir_path,
           "--label", l_a_b, l_x_y,
           "--annotation", ann,
           "-e", e_a_b, e_x_y,
           "--cmd", cmd,
           "--entrypoint", entrypoint,
           # "-u", user,
           "-p", p_80, p_443,
           "--runtime-volumes", runtime_volume,
           "--",
           basic_playbook_path, base_image, target_image]
    ab(cmd, str(tmpdir))
    out = inspect_resource("image", target_image)
    assert out['ImageAnnotations'] == {'bohemian': 'rhapsody'}
    co = out["Docker"]["config"]
    assert co["WorkingDir"] == workdir_path
    assert co["Labels"]["A"] == "B"
    assert co["Labels"]["x"] == "y"
    assert e_a_b in co["Env"]
    assert e_x_y in co["Env"]
    assert co["Cmd"] == cmd_e
    assert co["Entrypoint"] == entrypoint_e
    # assert co["User"] == user
    assert p_80 in co["ExposedPorts"]
    assert p_443 in co["ExposedPorts"]
    # FIXME: the volume is not set; a bug in buildah or ab?


def test_build_failure(tmpdir):
    target_image_name = "registry.example.com/ab-test-" + random_word(12)
    target_image_tag = "oldest"
    target_image = f"{target_image_name}:{target_image_tag}"
    cmd = ["build", bad_playbook_path, base_image, target_image]
    with pytest.raises(subprocess.CalledProcessError):
        ab(cmd, str(tmpdir))
    out = ab(["get-logs"], str(tmpdir), return_output=True).lstrip()
    assert out.startswith("PLAY [registry")

    # regex string for target image
    image_name_regex = "%s+[-]+[0-9]+[-]+[0-9]+-failed" %(target_image)

    cmd = ["inspect", "--json"]
    ab_inspect_data = json.loads(ab(cmd, str(tmpdir), return_output=True))
    top_layer_id = ab_inspect_data["layers"][-1]["layer_id"]
    final_image_id = ab_inspect_data["final_layer_id"]
    final_image_name = ab_inspect_data["target_image"]

    assert re.match(image_name_regex, final_image_name)

    p_inspect_data = json.loads(subprocess.check_output(["podman", "inspect", "-t", "image", final_image_name]))[0]
    image_id = p_inspect_data["Id"]

    assert image_id == top_layer_id
    assert image_id == final_image_id
    buildah("rmi", [final_image_name])


def test_two_runs(tmpdir, target_image):
    """ this is a naive test to verify race condition """
    cmd = ["python3", "-m", "ansible_bender.cli", "--database-dir", str(tmpdir),
           "build", basic_playbook_path, base_image,
           target_image]
    p1 = subprocess.Popen(cmd)
    p2 = subprocess.Popen(cmd)
    p1.wait()
    p2.wait()
    assert p1.returncode == 0
    assert p2.returncode == 0


def test_buildah_err_output(tmpdir, capfd):
    cmd = ["build", basic_playbook_path, base_image, "vrerv\\23&^&4//5B/F/BSFD/B"]
    ab(cmd, str(tmpdir), ignore_result=True)
    c = capfd.readouterr()
    assert "There was an error during execution" in c.err
    assert "buildah" in c.err


@pytest.mark.skipif(
    not os.path.exists("/var/run/docker.sock"),
    reason="Docker daemon is not running.")
def test_push_to_dockerd(target_image, tmpdir):
    cmd = ["build", basic_playbook_path, base_image, target_image]
    ab(cmd, str(tmpdir))
    target = "ab-test-" + random_word(12) + ":oldest"
    cmd = ["push", "docker-daemon:" + target]
    ab(cmd, str(tmpdir))
    try:
        cmd = ["docker", "run", "--rm", target, "cat", "/fun"]
        subprocess.check_call(cmd)

        docker_inspect = json.loads(subprocess.check_output(["docker", "inspect", target]))[0]
        p_inspect = json.loads(subprocess.check_output(
            ["podman", "inspect", "-t", "image", target_image]))[0]
        assert docker_inspect["RootFS"]["Layers"] == p_inspect["RootFS"]["Layers"]
        assert docker_inspect["Created"] == p_inspect["Created"]
        assert docker_inspect["Id"].endswith(p_inspect["Id"])
    finally:
        subprocess.check_call(["docker", "rmi", target])


# first buildah run command is setup, that should pass
# the second run command is our task, that one should fail
MAGIKO = """\
#!/bin/bash
set -x
echo $@ >>{tmpdir}/commands-executed
if [ "$1" == "run" ]; then
  if [ -f {tmpdir}/run-count ]; then
    run_count=$(cat {tmpdir}/run-count)
    echo $(( ++run_count )) >{tmpdir}/run-count
  else
    echo 1 >>{tmpdir}/run-count 
  fi
  run_count=$(cat {tmpdir}/run-count)
  if [ $run_count -ge 2 ]; then
    exit 42
  fi
fi
exec /usr/bin/buildah "$@"
"""


def test_tback_in_callback(tmpdir):
    # this code may look really crazy: we test a real-world scenario here
    # it may happen that something goes wrong while the callback plugin is running
    # in such case, the build should be aborted
    # that's exactly what we do here: we simulate buildah failure
    im = "registry.example.com/ab-will-fail-" + random_word(12) + ":oldest"
    new_path = f"{tmpdir}:" + os.environ["PATH"]
    new_buildah_path = os.path.join(str(tmpdir), "buildah")
    with open(new_buildah_path, "w") as fd:
        fd.write(MAGIKO.format(tmpdir=str(tmpdir)))
    os.chmod(new_buildah_path, 0o755)

    # make sure our new buildah gets called
    good_env = os.environ
    new_env = os.environ.copy()
    new_env["PATH"] = new_path
    new_env["ANSIBLE_STDOUT_CALLBACK"] = "debug"
    os.environ = new_env
    # assert subprocess.call(["env", f"PATH={new_path}", "buildah", "run"]) == 42

    try:
        cmd = ["--debug", "build", "--extra-ansible-args=-vvvvvv", basic_playbook_path, base_image, im]
        with pytest.raises(subprocess.CalledProcessError):
            ab(cmd, str(tmpdir), env=new_env)

        image_name_regex = "%s+[-]+[0-9]+[-]+[0-9]+-failed" %(im)
        try:
            cmd = ["inspect", "--json"]
            ab_inspect_data = json.loads(ab(cmd, str(tmpdir), return_output=True))
            assert ab_inspect_data["state"] == "failed"
            assert re.match(image_name_regex, ab_inspect_data["target_image"])
            assert len(ab_inspect_data["layers"]) == 2
            with pytest.raises(subprocess.CalledProcessError) as ex:
                podman_run_cmd(ab_inspect_data["target_image"], ["ls", "/fun"], return_output=True)
            assert "No such file or directory" in ex.value.output
        finally:
            subprocess.call(["buildah", "rmi", ab_inspect_data["target_image"]])
    finally:
        os.environ = good_env


def test_two_build_vols(tmpdir, target_image):
    vol1 = Path(tmpdir).joinpath("1")
    vol1.mkdir()
    vol2 = Path(tmpdir).joinpath("2")
    vol2.mkdir()
    container_mount1 = "/vol1"
    container_mount2 = "/vol2"
    vol_spec1 = f"{vol1}:{container_mount1}:Z"
    vol_spec2 = f"{vol2}:{container_mount2}:Z"
    cmd = ["build", "--build-volumes", vol_spec1, vol_spec2, "--",
           basic_playbook_path_w_bv, base_image, target_image]
    ab(cmd, str(tmpdir))

    cmd = ["inspect", "--json"]
    ab_inspect_data = json.loads(ab(cmd, str(tmpdir), return_output=True))
    build_volumes = ab_inspect_data["build_volumes"]

    assert vol_spec1 in build_volumes
    assert vol_spec2 in build_volumes


def test_squash(target_image, tmpdir):
    cmd = ["build", "--squash", basic_playbook_path, base_image, target_image]

    ab(cmd, str(tmpdir))

    p_inspect_data = json.loads(subprocess.check_output(["podman", "inspect", "-t", "image", target_image]))[0]
    assert len(p_inspect_data["RootFS"]["Layers"]) == 1

    cmd = ["inspect", "--json"]
    ab_inspect_data = json.loads(ab(cmd, str(tmpdir), return_output=True))
    assert len(ab_inspect_data["layers"]) == 1
