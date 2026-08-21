"""The production OpenCode boundary is fixed, attested, and fail-closed."""

from __future__ import annotations

import copy

import pytest

from recollect.config import RecollectConfig
from recollect.engine.sandbox import isolation
from recollect.engine.sandbox.isolation import IsolationError
from recollect.engine.sandbox.manager import SandboxManager, SandboxStartError


def _launch(tmp_path, monkeypatch):
    monkeypatch.setattr(isolation.shutil, "which", lambda name: f"/bin/{name}")
    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "config"
    workspace.mkdir()
    config_dir.mkdir()
    launch = isolation.build_container_launch(
        runtime="docker",
        image="recollect-opencode-sandbox:1.18.18",
        name="recollect-subagent-test",
        host_port=41234,
        workspace=workspace,
        config_dir=config_dir,
        password="secret",
        memory_mb=1024,
        pids=256,
        cpus=2.0,
    )
    return launch, workspace, config_dir


def _inspection(workspace, config_dir) -> list[dict]:
    return [
        {
            "Name": "/recollect-subagent-test",
            "State": {"Running": True},
            "Config": {
                "Image": "recollect-opencode-sandbox:1.18.18",
                "User": "65532:65532",
            },
            "HostConfig": {
                "Privileged": False,
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "NetworkMode": "bridge",
                "IpcMode": "none",
                "PidMode": "",
                "UTSMode": "",
                "UsernsMode": "",
                "CgroupnsMode": "private",
                "PidsLimit": 256,
                "Memory": 1024 * 1024 * 1024,
                "NanoCpus": 2_000_000_000,
                "Devices": [],
                "DeviceRequests": [],
                "PortBindings": {
                    "4096/tcp": [
                        {"HostIp": "127.0.0.1", "HostPort": "41234"}
                    ]
                },
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(workspace.resolve()),
                    "Destination": "/workspace",
                    "RW": True,
                },
                {
                    "Type": "bind",
                    "Source": str(config_dir.resolve()),
                    "Destination": "/config",
                    "RW": False,
                },
            ],
        }
    ]


def _attest(document, workspace, config_dir) -> None:
    isolation.attest_container(
        document,
        name="recollect-subagent-test",
        image="recollect-opencode-sandbox:1.18.18",
        workspace=workspace,
        config_dir=config_dir,
        host_port=41234,
        memory_mb=1024,
        pids=256,
        cpus=2.0,
    )


def test_container_command_has_no_escape_hatches(tmp_path, monkeypatch):
    launch, workspace, config_dir = _launch(tmp_path, monkeypatch)
    argv = launch.argv
    joined = " ".join(argv)

    assert argv[:2] == ["/bin/docker", "run"]
    for required in (
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        "--network bridge",
        "--ipc none",
        "--pids-limit 256",
        "--memory 1024m",
        "--publish 127.0.0.1:41234:4096",
    ):
        assert required in joined
    assert str(workspace.resolve()) in joined
    assert str(config_dir.resolve()) in joined
    assert "target=/config,readonly" in joined
    assert "/var/run/docker.sock" not in joined
    assert "--privileged" not in argv
    assert "--network host" not in joined


def test_loopback_model_url_is_routed_to_the_container_gateway():
    assert isolation.container_model_url("http://127.0.0.1:8000/v1") == (
        "http://host.docker.internal:8000/v1"
    )
    assert isolation.container_model_url("https://models.example/v1") == (
        "https://models.example/v1"
    )


def test_container_attestation_accepts_only_the_fixed_profile(
    tmp_path, monkeypatch
):
    _, workspace, config_dir = _launch(tmp_path, monkeypatch)
    baseline = _inspection(workspace, config_dir)
    _attest(baseline, workspace, config_dir)

    mutations = []
    for key, value in (
        ("Privileged", True),
        ("ReadonlyRootfs", False),
        ("NetworkMode", "host"),
        ("IpcMode", "host"),
        ("PidsLimit", 0),
        ("Memory", 0),
        ("NanoCpus", 0),
        ("PidMode", "host"),
        ("UTSMode", "host"),
        ("UsernsMode", "host"),
        ("CgroupnsMode", "host"),
    ):
        changed = copy.deepcopy(baseline)
        changed[0]["HostConfig"][key] = value
        mutations.append(changed)
    changed = copy.deepcopy(baseline)
    changed[0]["HostConfig"]["CapDrop"] = []
    mutations.append(changed)
    changed = copy.deepcopy(baseline)
    changed[0]["HostConfig"]["SecurityOpt"] = []
    mutations.append(changed)
    changed = copy.deepcopy(baseline)
    changed[0]["HostConfig"]["SecurityOpt"] = ["seccomp=unconfined"]
    mutations.append(changed)
    changed = copy.deepcopy(baseline)
    changed[0]["HostConfig"]["PortBindings"]["4096/tcp"][0]["HostIp"] = "0.0.0.0"
    mutations.append(changed)
    changed = copy.deepcopy(baseline)
    changed[0]["HostConfig"]["Devices"] = [{"PathOnHost": "/dev/sda"}]
    mutations.append(changed)
    changed = copy.deepcopy(baseline)
    changed[0]["Config"]["User"] = "root"
    mutations.append(changed)
    changed = copy.deepcopy(baseline)
    changed[0]["Mounts"].append(
        {
            "Type": "volume",
            "Source": "unexpected",
            "Destination": "/volume",
            "RW": True,
        }
    )
    mutations.append(changed)
    changed = copy.deepcopy(baseline)
    changed[0]["Mounts"].append(
        {
            "Type": "bind",
            "Source": "C:/Users",
            "Destination": "/host",
            "RW": True,
        }
    )
    mutations.append(changed)

    for document in mutations:
        with pytest.raises(IsolationError):
            _attest(document, workspace, config_dir)


async def test_production_manager_fails_closed_without_a_runtime(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(isolation.shutil, "which", lambda name: None)
    config = RecollectConfig(
        embedding_model_path=tmp_path / "embedding.gguf",
        data_dir=tmp_path / "var",
        sandbox_root=tmp_path / "sandboxes",
    )
    manager = SandboxManager(config)
    with pytest.raises(SandboxStartError, match="install Docker"):
        await manager.ensure("s1")
