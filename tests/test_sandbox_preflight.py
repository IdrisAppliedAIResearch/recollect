"""Unavailable Docker must fail before creating or launching a research task."""

import asyncio

import pytest

from recollect.config import RecollectConfig
from recollect.engine.sandbox import manager as sandbox
from recollect.engine.sandbox.manager import SandboxManager, SandboxStartError


class Probe:
    def __init__(self, *, code=0, output=b"linux\n", hangs=False):
        self.returncode = None if hangs else code
        self.output = output
        self.hangs = hangs
        self.started = asyncio.Event()
        self.killed = False
        self.waited = False

    async def communicate(self):
        self.started.set()
        if self.hangs:
            await asyncio.Event().wait()
        return self.output, None

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.waited = True
        return self.returncode


def manager(tmp_path):
    return SandboxManager(RecollectConfig(
        embedding_model_path=tmp_path / "unused.gguf",
        data_dir=tmp_path / "var",
        sandbox_root=tmp_path / "sandboxes",
        subagent_backend="opencode",
    ))


def install_probe(monkeypatch, probe):
    calls = []
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/runtime/docker")

    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs))
        return probe

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
    return calls


async def test_linux_engine_probe_only_queries_the_server_and_discards_stderr(
    tmp_path, monkeypatch,
):
    probe = Probe()
    calls = install_probe(monkeypatch, probe)
    instance = manager(tmp_path)

    await instance._check_container_runtime()

    assert calls == [(("/runtime/docker", "info", "--format", "{{.OSType}}"), {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.DEVNULL,
    })]
    assert not instance._root.exists()
    assert not probe.killed


async def test_unavailable_daemon_reports_recovery_without_launch_or_task_files(
    tmp_path, monkeypatch,
):
    calls = install_probe(monkeypatch, Probe(code=1, output=b"private host details"))
    instance = manager(tmp_path)

    with pytest.raises(
        SandboxStartError, match="Cannot reach the Docker engine",
    ) as error:
        await instance.begin_invocation("private conversation")

    assert "Start Docker Desktop or Docker Engine" in str(error.value)
    assert "private" not in str(error.value)
    assert len(calls) == 1
    assert calls[0][0][1] == "info"
    assert not instance._root.exists()
    assert instance._handle is None
    assert not instance._model_slot.locked()
    assert not instance._invocation_lock.locked()


@pytest.mark.parametrize("output", [b"windows\n", b"unexpected private output", b""])
async def test_non_linux_engine_fails_closed(tmp_path, monkeypatch, output):
    install_probe(monkeypatch, Probe(output=output))
    instance = manager(tmp_path)

    with pytest.raises(SandboxStartError, match="requires Linux containers") as error:
        await instance.ensure()

    assert "private" not in str(error.value)
    assert not instance._root.exists()


async def test_timeout_reaps_probe_and_releases_invocation_slots(tmp_path, monkeypatch):
    probe = Probe(hangs=True)
    install_probe(monkeypatch, probe)
    monkeypatch.setattr(sandbox, "_RUNTIME_CHECK_TIMEOUT_S", 0.01)
    instance = manager(tmp_path)

    with pytest.raises(SandboxStartError, match="Docker engine did not respond"):
        await instance.begin_invocation("conversation")

    assert probe.killed and probe.waited
    assert not instance._root.exists()
    assert not instance._model_slot.locked()
    assert not instance._invocation_lock.locked()


async def test_cancelled_preflight_reaps_probe_without_starting_research(
    tmp_path, monkeypatch,
):
    probe = Probe(hangs=True)
    calls = install_probe(monkeypatch, probe)
    instance = manager(tmp_path)
    task = asyncio.create_task(instance.begin_invocation("conversation"))
    await probe.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) == 1
    assert probe.killed and probe.waited
    assert not instance._root.exists()
    assert not instance._model_slot.locked()
    assert not instance._invocation_lock.locked()


async def test_exec_failure_does_not_persist_raw_os_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/runtime/docker")

    async def spawn(*argv, **kwargs):
        raise OSError("private path or diagnostic")

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
    instance = manager(tmp_path)

    with pytest.raises(SandboxStartError, match="Docker could not start") as error:
        await instance.ensure()

    assert "private" not in str(error.value)
    assert not instance._root.exists()
