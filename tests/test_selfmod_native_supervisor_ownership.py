"""Offline fault tests for the live qualification fixture's ownership rules."""

import asyncio
from types import SimpleNamespace

import pytest

from recollect.selfmod.journal import encode
from tests import test_selfmod_native_supervisor_docker as fixture
from tests.test_selfmod_native_containment import spec


@pytest.fixture
def owned(tmp_path):
    config = spec()
    root = tmp_path / ("selfmod-supervisor-qualification-" + config.run_id)
    root.mkdir()
    (root / "input").mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return fixture.RunningSupervisor(["docker"], {}, config, root, evidence)


class DockerState:
    def __init__(self, owned, *, exists=True, fault=None):
        self.owned, self.exists, self.fault = owned, exists, fault
        self.identity = "c" * 64
        self.running, self.paused = True, False
        self.calls = []

    def inspection(self):
        config = self.owned.config
        return {
            "Id": self.identity, "Name": "/" + config.name, "Image": config.image_id,
            "Config": {"Labels": {"recollect.selfmod": config.run_id,
                                  "recollect.spec": config.sha256}},
            "State": {"Running": self.running, "Paused": self.paused,
                      "Pid": 1 if self.running else 0,
                      "Status": "running" if self.running else "exited"},
            "Mounts": [{"Type": "bind", "RW": False,
                        "Source": str(self.owned.root / "input"),
                        "Destination": "/authority"}],
        }

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        kind = args[0]
        if kind == self.fault:
            raise OSError(kind + " failed")
        if kind == "ps":
            raw = (self.identity + "\n").encode() if self.exists else b""
        elif kind == "inspect":
            raw = encode([self.inspection()])
        elif kind == "pause":
            self.paused, raw = True, b""
        elif kind == "cp":
            assert self.paused and args == ("cp", self.identity + ":/evidence", "-")
            raw = b"opaque diagnostic tar, never extracted by fixture"
        elif kind == "unpause":
            self.paused, raw = False, b""
        elif kind == "kill":
            assert not self.paused
            self.running, raw = False, b""
        elif kind == "rm":
            assert not self.running
            self.exists, raw = False, b""
        elif kind == "exec":
            assert self.owned.finished_frame is not None
            raw = b"{}\n"
        else:
            raise AssertionError(args)
        return 0, raw, b""


async def test_ambiguous_create_empty_lookup_retains_inputs(owned, monkeypatch):
    state = DockerState(owned, exists=False)
    monkeypatch.setattr(owned, "docker", state)

    async def failed():
        raise OSError("daemon response lost")

    owned._create_task = asyncio.create_task(failed())
    with pytest.raises(BaseExceptionGroup, match="inputs retained"):
        await owned.cleanup()
    assert owned.root.exists() and not owned._create_resolved
    assert all(args[0] == "ps" for args in state.calls)
    assert (owned.evidence / "create-error.json").is_file()


async def test_cancelled_start_settles_create_before_absence_lookup(owned, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    state = DockerState(owned, exists=False)
    monkeypatch.setattr(owned, "docker", state)

    async def create():
        entered.set()
        await release.wait()
        owned.identity = state.identity
        owned._create_resolved = True

    monkeypatch.setattr(owned, "_create", create)
    starting = asyncio.create_task(owned.start())
    await entered.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert not owned._create_task.done()
    cleanup = asyncio.create_task(owned.cleanup())
    await asyncio.sleep(0)
    assert not state.calls and not cleanup.done()
    release.set()
    await cleanup
    assert owned._create_task.done() and not owned.root.exists()


async def test_cancelled_attach_spawn_is_owned_until_cleanup(owned, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    state = DockerState(owned)
    closed = []

    async def communicate():
        assert not state.exists
        return b"preserved tail", b""

    async def wait():
        return 0

    process = SimpleNamespace(
        stdin=SimpleNamespace(close=lambda: closed.append(True)),
        communicate=communicate, returncode=0, wait=wait,
    )

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return process

    async def create():
        owned.identity = state.identity
        owned._create_resolved = True

    monkeypatch.setattr(owned, "_create", create)
    monkeypatch.setattr(owned, "docker", state)
    monkeypatch.setattr(fixture, "attest", lambda *a: None)
    monkeypatch.setattr(fixture.asyncio, "create_subprocess_exec", spawn)
    starting = asyncio.create_task(owned.start())
    await entered.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    cleanup = asyncio.create_task(owned.cleanup())
    await asyncio.sleep(0)
    assert not cleanup.done() and not owned._attachment_spawn.done()
    release.set()
    await cleanup
    assert owned.attachment is process and closed == [True]
    assert not state.exists and not owned.root.exists()


@pytest.mark.parametrize("fault", [None, "cp"])
async def test_unknown_collector_state_never_adds_root_exec_peers(
    owned, monkeypatch, fault,
):
    state = DockerState(owned, fault=fault)
    monkeypatch.setattr(owned, "docker", state)
    if fault:
        with pytest.raises(BaseExceptionGroup, match="inputs retained"):
            await owned.cleanup()
        assert owned.root.exists()
    else:
        await owned.cleanup()
        assert not owned.root.exists()
        assert (owned.evidence / "emergency-evidence.tar").is_file()
    assert not state.exists
    kinds = [args[0] for args in state.calls]
    assert "exec" not in kinds
    assert kinds.index("pause") < kinds.index("cp") < kinds.index("unpause")
    assert kinds.index("unpause") < kinds.index("kill") < kinds.index("rm")


async def test_ambiguous_create_with_independently_owned_container_can_reconcile(
    owned, monkeypatch,
):
    state = DockerState(owned)
    monkeypatch.setattr(owned, "docker", state)

    async def failed():
        raise OSError("lost create reply")

    owned._create_task = asyncio.create_task(failed())
    await owned.cleanup()
    assert owned._create_resolved and not state.exists and not owned.root.exists()


async def test_evidence_exec_forbidden_until_collection_finishes(owned, monkeypatch):
    state = DockerState(owned)
    monkeypatch.setattr(owned, "docker", state)
    with pytest.raises(AssertionError, match="census"):
        await owned.read("native.json")
    assert not state.calls


@pytest.mark.parametrize("fault", [None, "run", "hash", "status", "extra"])
async def test_only_valid_bound_completion_opens_evidence_read_gate(owned, fault):
    frame = {"kind": "terminal_collection_finished", "run_id": owned.config.run_id,
             "runtime_sha256": owned.config.sha256, "collector_returncode": 0,
             "failed": False}
    if fault == "run":
        frame["run_id"] = "foreign"
    elif fault == "hash":
        frame["runtime_sha256"] = "0" * 64
    elif fault == "status":
        frame["collector_returncode"] = False
    elif fault == "extra":
        frame["extra"] = True
    reader = asyncio.StreamReader()
    reader.feed_data(encode(frame))
    reader.feed_eof()
    owned.attachment = SimpleNamespace(stdout=reader)
    await owned._read_controls()
    assert (owned.finished_frame is not None) == (fault is None)
    assert owned._terminal.is_set()


@pytest.mark.parametrize("fault", ["inspect", "rm"])
async def test_namespace_failure_cannot_skip_owned_attachment_cleanup(
    owned, monkeypatch, fault,
):
    state = DockerState(owned, fault=fault)
    monkeypatch.setattr(owned, "docker", state)
    calls = []

    async def communicate():
        calls.append("reaped")
        return b"tail", b"error"

    async def wait():
        calls.append("waited")
        return 0

    owned.attachment = SimpleNamespace(
        returncode=None, kill=lambda: calls.append("kill_cli"),
        stdin=SimpleNamespace(close=lambda: calls.append("close_stdin")),
        communicate=communicate, wait=wait,
    )
    with pytest.raises(BaseExceptionGroup, match="inputs retained"):
        await owned.cleanup()
    assert calls == ["kill_cli", "close_stdin", "reaped", "waited"]
    assert owned.root.exists()
    assert (owned.evidence / "attachment.stderr").read_bytes() == b"error"


async def test_stdin_close_fault_does_not_skip_reap(owned):
    calls = []

    def close():
        raise OSError("close failed")

    async def communicate():
        calls.append("drained")
        return b"", b""

    async def wait():
        calls.append("reaped")
        return 0

    owned.attachment = SimpleNamespace(
        returncode=0, stdin=SimpleNamespace(close=close),
        communicate=communicate, wait=wait,
    )
    with pytest.raises(BaseExceptionGroup, match="Attachment cleanup failed"):
        await owned._close_attachment()
    assert calls == ["drained", "reaped"]


async def test_stderr_failure_waits_for_other_owned_readers_and_process(owned):
    waiting, release = asyncio.Event(), asyncio.Event()
    drained = []

    async def read():
        await release.wait()
        drained.append(True)

    async def stderr():
        raise OSError("stderr evidence failed")

    async def wait():
        waiting.set()
        await release.wait()
        return 0

    owned.attachment = SimpleNamespace(
        returncode=0, stdin=SimpleNamespace(close=lambda: None), wait=wait,
    )
    owned._reader = asyncio.create_task(read())
    owned._stderr = asyncio.create_task(stderr())
    closing = asyncio.create_task(owned._close_attachment())
    await waiting.wait()
    assert not closing.done() and not drained
    release.set()
    with pytest.raises(BaseExceptionGroup, match="Attachment cleanup failed"):
        await closing
    assert drained == [True] and owned._reader.done() and owned._stderr.done()
