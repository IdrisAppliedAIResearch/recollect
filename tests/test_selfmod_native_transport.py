"""Faults at the qualification relay/cleanup seam; no Docker or model needed."""

import asyncio
import base64
import io
import json
import sys
import threading
import urllib.request
from types import SimpleNamespace

import httpx
import pytest

from recollect.selfmod.journal import Journal, encode
from recollect.selfmod.native import NativeSession, _durable, _settle
from tests import test_selfmod_native_docker as live
from tests.test_selfmod_native import settings


class Process:
    def __init__(self):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.returncode = None
        self.finished = asyncio.Event()
        self.killed = self.reaped = False
        self.input = []

        async def ready():
            pass

        self.stdin = SimpleNamespace(write=self.input.append, drain=ready,
                                     close=lambda: None)

    def kill(self):
        self.killed = True
        self.returncode = -9
        if not self.stdout.at_eof():
            self.stdout.feed_eof()
        if not self.stderr.at_eof():
            self.stderr.feed_eof()
        self.finished.set()

    async def wait(self):
        await self.finished.wait()
        self.reaped = True
        return self.returncode


@pytest.mark.parametrize("fault", ["disconnect", "cancel"])
async def test_real_relay_framing_preserves_prefix_and_reaps_cli(
    tmp_path, monkeypatch, fault,
):
    process = Process()
    received = asyncio.Event()
    prefix = b"partial health response"

    async def spawn(*args, **kwargs):
        process.stdout.feed_data(encode({"status": 200, "headers": {}}))
        process.stdout.feed_data(encode({"chunk": base64.b64encode(prefix).decode()}))
        if fault == "disconnect":
            process.stdout.feed_eof()
        return process

    original = live.RelayStream.__aiter__

    async def observe(self):
        async for chunk in original(self):
            yield chunk
            received.set()

    monkeypatch.setattr(live.RelayStream, "__aiter__", observe)
    monkeypatch.setattr(live.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(live.shutil, "which", lambda _: "fixture-docker")
    journal = Journal.create(tmp_path / "archive")
    session = NativeSession(settings(), journal, transport=live.Relay("owned"))
    try:
        work = asyncio.create_task(session.start())
        await received.wait()
        if fault == "cancel":
            work.cancel()
        with pytest.raises((httpx.ReadError, asyncio.CancelledError)):
            await work
        record = journal.verify()[-1]
        assert record.value["data"]["status"] == 200
        assert record.value["data"]["complete"] is False
        assert record.files.files[0].content == prefix
        assert process.killed and process.reaped
    finally:
        await session.close()
        journal.close()


async def test_cancel_during_docker_spawn_retains_and_reaps_process(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    process = Process()

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return process

    monkeypatch.setattr(live.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(live.shutil, "which", lambda _: "fixture-docker")
    command = asyncio.create_task(live.docker("fixture"))
    await entered.wait()
    command.cancel()
    await asyncio.sleep(0)
    assert not command.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await command
    assert process.killed and process.reaped


@pytest.mark.parametrize("fault", ["logs", "close", "remaining", "setup"])
async def test_cleanup_independent_phases_and_confirmed_removal(
    tmp_path, monkeypatch, fault,
):
    name = "recollect-selfmod-native-fixture"
    root = tmp_path / "selfmod-native-qualification-fixture"
    root.mkdir()
    calls = []

    async def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "exec" and fault == "logs":
            return 1, b"", b"capture failed"
        if args[0] == "ps" and fault == "remaining":
            return 0, name.encode(), b""
        return 0, b"", b""

    async def close():
        calls.append(("close",))
        if fault == "close":
            raise ValueError("close failed")

    monkeypatch.setattr(live, "docker", docker)
    session = None if fault == "setup" else SimpleNamespace(close=close)
    journal = None if fault == "setup" else SimpleNamespace(
        close=lambda: calls.append(("journal",))
    )
    if fault == "setup":
        await live.cleanup(name, root, session, journal, tmp_path)
    else:
        with pytest.raises(ExceptionGroup):
            await live.cleanup(name, root, session, journal, tmp_path)
        assert ("close",) in calls and ("journal",) in calls
    assert len([c for c in calls if c[0] == "exec"]) == 2
    assert any(c[0] == "rm" for c in calls)
    assert any(c[0] == "ps" for c in calls)
    assert root.exists() == (fault == "remaining")


async def test_cleanup_survives_repeated_cancellation(tmp_path, monkeypatch):
    root = tmp_path / "selfmod-native-qualification-fixture"
    root.mkdir()
    entered, release = asyncio.Event(), asyncio.Event()

    async def docker(*args, **kwargs):
        if args[0] == "rm":
            entered.set()
            await release.wait()
        return 0, b"", b""

    monkeypatch.setattr(live, "docker", docker)
    work = asyncio.create_task(_settle(asyncio.create_task(live.cleanup(
        "recollect-selfmod-native-fixture", root, None, None, tmp_path,
    ))))
    await entered.wait()
    work.cancel()
    await asyncio.sleep(0)
    work.cancel()
    await asyncio.sleep(0)
    assert root.exists() and not work.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert not root.exists()


async def test_relay_stderr_is_drained_beyond_retained_prefix():
    process = Process()
    process.stderr.feed_data(b"x" * 200000)
    process.stderr.feed_eof()
    stream = live.RelayStream(process)
    assert await stream.stderr == b"x" * 65537
    assert process.stderr.at_eof()
    await stream.aclose()
    assert process.reaped


@pytest.mark.parametrize("truncated", [False, True])
def test_container_relay_emits_end_only_after_complete_content_length(
    monkeypatch, truncated,
):
    class Response:
        status = 200
        headers = {"Content-Length": "8" if truncated else "3"}
        length = 8 if truncated else 3
        chunks = [b"abc", b""]

        def read1(self, size):
            chunk = self.chunks.pop(0)
            self.length -= len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    output = io.StringIO()
    monkeypatch.setattr(urllib.request, "urlopen", lambda _: Response())
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "method": "GET", "path": "/global/health", "body": "",
    })))
    monkeypatch.setattr(sys, "stdout", output)
    if truncated:
        with pytest.raises(ValueError, match="before Content-Length"):
            exec(live.RELAY, {})
    else:
        exec(live.RELAY, {})
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[0]["status"] == 200
    assert base64.b64decode(frames[1]["chunk"]) == b"abc"
    assert ({"end": True} in frames) is not truncated


async def test_fixture_append_settles_before_cancellation_is_delivered():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def append():
        entered.set()
        release.wait()
        finished.set()

    work = asyncio.create_task(_durable(append))
    await asyncio.to_thread(entered.wait)
    work.cancel()
    await asyncio.sleep(0)
    work.cancel()
    await asyncio.sleep(0)
    assert not work.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert finished.is_set()


@pytest.mark.parametrize("fault", ["cancel", "launch_error", "unknown", "wrong_owner"])
async def test_ambiguous_native_launch_preserves_before_removal(
    tmp_path, monkeypatch, fault,
):
    shared = tmp_path / "local" / "recollect" / "sandboxes"
    shared.mkdir(parents=True)
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    entered, release = asyncio.Event(), asyncio.Event()
    calls, owned = [], {}

    async def docker(*args, **kwargs):
        calls.append(args[0])
        if args[0] == "image":
            return 0, b"sha256:fixture", b""
        if args[0] == "run":
            owned["name"] = args[args.index("--name") + 1]
            owned["identity"] = args[args.index("--label") + 1].split("=", 1)[1]
            entered.set()
            await release.wait()
            if fault != "cancel":
                raise RuntimeError("ambiguous launch reply")
            return 0, b"created", b""
        if args[0] == "ps":
            if fault == "unknown":
                raise RuntimeError("engine unreachable")
            return 0, owned["name"].encode(), b""
        if args[0] == "inspect":
            return 0, encode([{
                "Name": "/" + owned["name"], "Image": "sha256:fixture",
                "Config": {"Labels": {"recollect.selfmod": (
                    "other" if fault == "wrong_owner" else owned["identity"]
                )}},
                "Mounts": [{"Type": "bind", "Destination": "/authority",
                            "RW": False, "Source": str(next(shared.iterdir()))}],
            }]), b""
        raise AssertionError(args)

    async def preserve(name, target):
        assert name == owned["name"] and target == archive
        calls.append("preserve")

    async def cleanup(name, root, session, journal, target):
        assert "preserve" in calls
        calls.append("remove")
        await session.close()
        journal.close()
        live.shutil.rmtree(root)

    monkeypatch.setattr(live, "docker", docker)
    monkeypatch.setattr(live, "preserve_native_state", preserve)
    monkeypatch.setattr(live, "cleanup", cleanup)
    work = asyncio.create_task(live.qualify_native(archive, True))
    await entered.wait()
    if fault == "cancel":
        work.cancel()
        await asyncio.sleep(0)
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done() and "ps" not in calls
    release.set()
    with pytest.raises((asyncio.CancelledError, RuntimeError, ExceptionGroup)):
        await work
    if fault in ("unknown", "wrong_owner"):
        assert "preserve" not in calls and "remove" not in calls
        assert list(shared.iterdir())
    else:
        assert calls.index("ps") < calls.index("preserve") < calls.index("remove")
        assert not list(shared.iterdir())
