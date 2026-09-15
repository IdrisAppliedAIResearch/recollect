from copy import deepcopy

import pytest

from recollect.selfmod import native_containment as native
from recollect.selfmod import native_supervisor as supervisor
from recollect.selfmod.journal import IntegrityError, decode
from tests.selfmod_containment_helpers import CONTAINER_ID, inspection
from tests.test_selfmod_native_capture import spec as capture_spec


def spec():
    return native.NativeRuntimeSpec(capture_spec(), "sha256:" + "b" * 64,
                                    ("LANG=C.UTF-8",), "c" * 64)


def inspected(root, config):
    value = inspection(root, config)
    host = value["HostConfig"]
    host.update(Memory=2**30, MemorySwap=2**30, PidsLimit=256,
                CapAdd=list(native.CAPABILITIES), Tmpfs=dict(native.TMPFS),
                Ulimits=[{"Name": "core", "Hard": 0, "Soft": 0},
                         {"Name": "nofile", "Hard": 1024, "Soft": 1024}])
    value["Config"].update(Cmd=list(native.ENTRYPOINT), Env=[
        *config.image_environment, *(k + "=" + v for k, v in native.ENVIRONMENT.items())
    ])
    value["Mounts"][0]["Destination"] = "/authority"
    return value


def test_exact_native_profile_manifest_and_attestation(tmp_path):
    config = spec()
    native.attest(inspected(tmp_path, config), config, tmp_path, CONTAINER_ID)
    args = native.create_arguments(config, tmp_path)
    assert args[args.index("--network") + 1] == "none"
    assert args.count("--mount") == 1
    assert args[-6:] == [config.image_id, *native.ENTRYPOINT]
    frozen = {f.path: f.content for f in config.inputs.files}
    envelope = supervisor.load_manifest(frozen["runtime-spec.json"])
    assert envelope["sha256"] == config.sha256
    assert set(envelope["payload"]["helpers"]) == supervisor.HELPERS
    assert decode(config.control("release")) == {
        "kind": "release", "run_id": config.run_id, "runtime_sha256": config.sha256,
    }


def test_native_manifest_payload_cannot_drift_through_mutable_global(
    monkeypatch, tmp_path,
):
    config = spec()
    identity = config.sha256
    payload = config.payload
    payload["helpers"].clear()
    monkeypatch.setitem(native.TMPFS, "/work", "unsafe")
    assert config.sha256 == identity
    assert config.payload["isolation"]["tmpfs"]["/work"] != "unsafe"
    with pytest.raises(IntegrityError, match="profile changed"):
        native.create_arguments(config, tmp_path)
    with pytest.raises(IntegrityError, match="profile changed"):
        native.attest({}, config, tmp_path, CONTAINER_ID)


@pytest.mark.parametrize("section,key,value", [
    ("HostConfig", "Privileged", True), ("HostConfig", "ReadonlyRootfs", 1),
    ("HostConfig", "NetworkMode", "bridge"), ("HostConfig", "IpcMode", "host"),
    ("HostConfig", "PidMode", "host"), ("HostConfig", "UTSMode", "host"),
    ("HostConfig", "CgroupnsMode", "host"), ("HostConfig", "Memory", 0),
    ("HostConfig", "MemorySwap", -1), ("HostConfig", "PidsLimit", -1),
    ("HostConfig", "CapAdd", ["SYS_ADMIN"]), ("HostConfig", "CapDrop", []),
    ("HostConfig", "Tmpfs", {}), ("HostConfig", "Devices", [{}]),
    ("HostConfig", "DeviceRequests", [{}]), ("HostConfig", "Binds", ["/:/host"]),
    ("HostConfig", "ExtraHosts", ["host.docker.internal:host-gateway"]),
    ("HostConfig", "GroupAdd", ["0"]), ("HostConfig", "SecurityOpt", []),
    ("HostConfig", "MaskedPaths", []), ("HostConfig", "ReadonlyPaths", []),
    ("HostConfig", "Init", True), ("HostConfig", "OomKillDisable", True),
    ("HostConfig", "OomKillDisable", 0), ("HostConfig", "Ulimits", []),
    ("Config", "Tty", True), ("Config", "OpenStdin", False),
    ("Config", "Entrypoint", ["/bin/sh"]), ("Config", "Cmd", ["/work/agent.py"]),
    ("Config", "User", "65532:65532"), ("Config", "Env", []),
    ("Config", "Labels", {}), ("Config", "StopTimeout", True),
])
def test_weakened_native_policy_rejected(tmp_path, section, key, value):
    config = spec()
    actual = inspected(tmp_path, config)
    actual[section][key] = value
    with pytest.raises(IntegrityError):
        native.attest(actual, config, tmp_path, CONTAINER_ID)


@pytest.mark.parametrize("fault", ["writable", "other", "extra", "duplicate"])
def test_native_mount_changes_rejected(tmp_path, fault):
    config = spec()
    actual = inspected(tmp_path, config)
    if fault == "writable":
        actual["Mounts"][0]["RW"] = True
    elif fault == "other":
        actual["Mounts"][0]["Source"] = str(tmp_path / "other")
    elif fault == "duplicate":
        actual["Mounts"].append(deepcopy(actual["Mounts"][0]))
    else:
        actual["Mounts"].append({"Type": "tmpfs", "Destination": "/arbitrary"})
    with pytest.raises(IntegrityError, match="mount"):
        native.attest(actual, config, tmp_path, CONTAINER_ID)


def test_manifest_includes_exact_frozen_helper_bytes():
    config = spec()
    files = {f.path: f.content for f in config.inputs.files}
    assert decode(files["runtime-spec.json"])["payload"] == config.payload
    for name, expected in config.payload["helpers"].items():
        assert supervisor.digest(files[name]) == expected
