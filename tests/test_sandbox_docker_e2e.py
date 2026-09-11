"""Opt-in lifecycle and containment checks against the real Docker image."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from recollect.config import RecollectConfig
from recollect.engine.sandbox.manager import SandboxManager

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RECOLLECT_RUN_DOCKER_TESTS") != "1",
        reason="set RECOLLECT_RUN_DOCKER_TESTS=1 for live Docker tests",
    ),
]


@pytest.fixture
def docker_root():
    root = (
        Path(os.environ["LOCALAPPDATA"])
        / "recollect"
        / "sandboxes"
        / f"docker-e2e-{uuid.uuid4().hex}"
    )
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _config(docker_root: Path) -> RecollectConfig:
    return RecollectConfig(
        embedding_model_path=docker_root / "embedding.gguf",
        data_dir=docker_root / "var",
        sandbox_root=docker_root,
        subagent_backend="opencode",
    )


async def _docker(*args: str, check: bool = True) -> tuple[int, str, str]:
    runtime = shutil.which("docker")
    assert runtime is not None
    process = await asyncio.create_subprocess_exec(
        runtime,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    decoded_out = stdout.decode("utf-8", "replace")
    decoded_err = stderr.decode("utf-8", "replace")
    if check and process.returncode:
        pytest.fail(f"docker {' '.join(args)} failed: {decoded_err}")
    return process.returncode or 0, decoded_out, decoded_err


async def _finish_active(manager: SandboxManager) -> None:
    if manager._active is not None:
        await manager.finish_invocation(manager._active)
    await manager.close_all()


async def test_shared_container_lifecycle_resources_and_escape_barriers(
    docker_root,
):
    manager = SandboxManager(_config(docker_root))
    try:
        first = await manager.begin_invocation("chat-a")
        handle = first.handle
        assert handle.container is not None
        assert handle.isolation == "container"
        assert first.process_reused is False
        name = handle.container.name

        _, inspection_text, _ = await _docker("inspect", name)
        inspection = json.loads(inspection_text)[0]
        host = inspection["HostConfig"]
        assert host["Memory"] == 1024 * 1024 * 1024
        assert host["MemorySwap"] == host["Memory"]
        assert host["PidsLimit"] == 256
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert host["Ulimits"] == [
            {"Name": "nofile", "Hard": 1024, "Soft": 1024}
        ]

        _, status, _ = await _docker("exec", name, "cat", "/proc/1/status")
        assert "NoNewPrivs:\t1" in status
        assert "Seccomp:\t2" in status
        assert "CapEff:\t0000000000000000" in status

        code, _, _ = await _docker(
            "exec", name, "touch", "/escape", check=False
        )
        assert code != 0
        code, _, _ = await _docker(
            "exec", name, "touch", "/config/escape", check=False
        )
        assert code != 0
        await _docker("exec", name, "test", "!", "-e", "/var/run/docker.sock")
        await _docker("exec", name, "touch", "/workspace/allowed")
        assert (handle.workdir / "allowed").is_file()

        _, stats_text, _ = await _docker(
            "stats", "--no-stream", "--format", "{{json .}}", name
        )
        stats = json.loads(stats_text)
        assert stats["Name"] == name
        assert "/ 1GiB" in stats["MemUsage"]

        await manager.finish_invocation(first)
        assert list(handle.workdir.iterdir()) == []

        second = await manager.begin_invocation("chat-b")
        assert second.handle is handle
        assert second.process_reused is True
        assert second.oc_session_id != first.oc_session_id
        await manager.finish_invocation(second)
    finally:
        await _finish_active(manager)


async def test_killed_container_restarts_without_poisoning_next_call(
    docker_root,
):
    manager = SandboxManager(_config(docker_root))
    try:
        first = await manager.begin_invocation("chat-a")
        assert first.handle.container is not None
        old_name = first.handle.container.name
        await _docker("kill", old_name)
        if first.handle.process is not None:
            await asyncio.wait_for(first.handle.process.wait(), timeout=10.0)

        await manager.finish_invocation(first)
        second = await manager.begin_invocation("chat-b")
        assert second.handle.container is not None
        assert second.handle.container.name != old_name
        assert second.process_reused is False
        await manager.finish_invocation(second)
    finally:
        await _finish_active(manager)


async def test_continuous_skills_discovered_by_pinned_opencode(docker_root):
    from dataclasses import replace

    config = replace(_config(docker_root), subagent_continuous_enabled=True)
    manager = SandboxManager(config)
    try:
        invocation = await manager.begin_invocation("skill-discovery")
        response = await invocation.handle.client.get("/skill")
        response.raise_for_status()
        skills = {item["name"]: item for item in response.json()}
        assert {"recollect-reporting", "recollect-files", "recollect-research"} <= set(
            skills,
        )
        for name in ("recollect-reporting", "recollect-files", "recollect-research"):
            skill = skills[name]
            assert skill["location"].startswith("/config/skills/")
            assert skill["description"]
        assert "kind=result" in skills["recollect-reporting"]["content"]
        assert "2 MiB" in skills["recollect-files"]["content"]
        # Listing /skill does not exercise the native tool's supporting-file
        # enumeration. Its executable must work with all scratch mounts noexec.
        name = invocation.handle.container.name
        _, version, _ = await _docker("exec", name, "rg", "--version")
        assert "ripgrep 15.1.0" in version
        code, _, _ = await _docker(
            "exec", "--workdir", "/config/skills/recollect-reporting", name,
            "rg", "--no-config", "--files", "--hidden", "--glob=!**/SKILL.md", ".",
            check=False,
        )
        assert code in {0, 1}
        assert list(invocation.handle.workdir.iterdir()) == []
    finally:
        await _finish_active(manager)
