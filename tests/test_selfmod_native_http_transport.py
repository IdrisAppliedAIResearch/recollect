"""Production native HTTP IPC faults, using no Docker, services or models."""

import asyncio
import base64
import io
import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from recollect.selfmod import native_http_relay as relay
from recollect.selfmod import native_transport as transport
from recollect.selfmod.journal import Journal, encode
from recollect.selfmod.native import NativeSession
from tests.test_selfmod_native import settings

TEST_CLI = TEST_CONFIG = None


@pytest.fixture(autouse=True)
def private_cli_assets(tmp_path, monkeypatch):
    cli = tmp_path / "docker.exe"
    cli.write_bytes(b"mock Docker executable; never launched")
    private = tmp_path / "private-cli-config"
    private.mkdir()
    (private / "config.json").write_bytes(b'{"auths":{}}\n')
    monkeypatch.setitem(globals(), "TEST_CLI", cli)
    monkeypatch.setitem(globals(), "TEST_CONFIG", private)


class Process:
    def __init__(self):
        self.stdout = asyncio.StreamReader(limit=relay.FRAME_BYTES)
        self.stderr = asyncio.StreamReader(limit=relay.FRAME_BYTES)
        self.returncode = None
        self.finished = asyncio.Event()
        self.reap_release = asyncio.Event()
        self.reap_release.set()
        self.waiting = asyncio.Event()
        self.input = []
        self.killed = self.reaped = self.stdin_closed = False

        async def drain():
            await asyncio.sleep(0)

        def close():
            self.stdin_closed = True

        self.stdin = SimpleNamespace(write=self.input.append, drain=drain, close=close)

    def finish(self, code=0):
        self.returncode = code
        if not self.stdout.at_eof():
            self.stdout.feed_eof()
        if not self.stderr.at_eof():
            self.stderr.feed_eof()
        self.finished.set()

    def kill(self):
        self.killed = True
        self.finish(-9)

    async def wait(self):
        self.waiting.set()
        await self.finished.wait()
        await self.reap_release.wait()
        self.reaped = True
        return self.returncode


def config():
    return transport.NativeTransportConfig(
        str(TEST_CLI), "recollect-native-owned",
        "unix:///var/run/docker.sock", str(TEST_CONFIG), (),
    )


def chunk(data):
    return encode({"chunk": base64.b64encode(data).decode()})


def response(process, body=b"{}", *, status=200, headers=None, end=True):
    process.stdout.feed_data(encode({"status": status, "headers": headers or []}))
    if body:
        process.stdout.feed_data(chunk(body))
    if end:
        process.stdout.feed_data(encode({"end": True}))
        process.finish()


def mock_spawn(monkeypatch, process):
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(transport.asyncio, "create_subprocess_exec", spawn)
    return calls


def test_helper_snapshot_is_frozen_standalone_bytes():
    frozen = transport.helper_inputs()
    assert len(frozen.files) == 1
    helper = frozen.files[0]
    assert "/authority/" + helper.path == transport.RELAY_PATH
    assert helper.content == Path(relay.__file__).read_bytes()
    assert b"from recollect" not in helper.content
    assert b"eval(" not in helper.content and b"exec(" not in helper.content
    assert transport.helper_inputs().sha256 == frozen.sha256


@pytest.mark.parametrize("field,value", [
    ("docker_cli", "docker"), ("docker_cli", "--host=remote"),
    ("container_name", "--privileged"), ("container_name", "owned other"),
    ("container_name", "owned;whoami"), ("container_name", "owned/other"),
    ("relay_path", "/work/relay.py"), ("relay_path", "/authority/../work/relay.py"),
])
def test_config_rejects_untrusted_command_shapes(field, value):
    values = {"docker_cli": config().docker_cli, "container_name": "owned",
              "endpoint": config().endpoint, "docker_config": config().docker_config,
              "environment": ()}
    values[field] = value
    with pytest.raises(ValueError):
        transport.NativeTransportConfig(**values)


@pytest.mark.parametrize("endpoint", [
    "tcp://127.0.0.1:2375", "tcp://remote:2376", "ssh://remote", "default", "",
    "npipe:////remote/pipe/docker", "unix://remote/var/run/docker.sock",
    "unix:///var/run/docker.sock\n--context=other",
])
def test_config_requires_explicit_local_endpoint(endpoint):
    with pytest.raises(ValueError, match="local Docker endpoints"):
        replace(config(), endpoint=endpoint)


@pytest.mark.parametrize("endpoint", [
    "unix:///var/run/docker.sock", "npipe:////./pipe/dockerDesktopLinuxEngine",
])
def test_runtime_argv_prefix_roundtrip(endpoint):
    original = replace(config(), endpoint=endpoint)
    frozen = transport.NativeTransportConfig.from_argv(
        [original.docker_cli, *original.docker_args], "a1" * 32, env={"PATH": "frozen"},
    )
    assert frozen.container_name == "a1" * 32
    assert frozen.endpoint == endpoint
    assert frozen.docker_config == original.docker_config
    assert frozen.environment == (("PATH", "frozen"),)


@pytest.mark.parametrize("suffix", [
    (), ("exec", "owned"), ("--context", "default", "--host", "unix:///socket"),
    ("--host", "unix:///one", "--host", "unix:///two"),
    ("--config", "private", "--host", "unix:///socket", "exec"),
])
def test_runtime_prefix_cannot_supply_subcommand_or_context(suffix):
    with pytest.raises(ValueError):
        transport.NativeTransportConfig.from_argv(
            (config().docker_cli, *suffix), "a" * 64, env={},
        )


@pytest.mark.parametrize("environment", [
    (("DOCKER_HOST", "tcp://remote:2375"),), (("DOCKER_CONTEXT", "other"),),
    (("DOCKER_CONFIG", "other"),), (("HTTP_PROXY", "http://remote"),),
    (("DOCKER_TLS_VERIFY", "1"),), (("PYTHONPATH", "other"),),
    (("PATH", "one"), ("Path", "two")), (("PATH", "bad\0value"),),
    {"PATH": "mutable"}, (["PATH", "mutable"],),
])
def test_config_rejects_mutable_or_nonwhitelisted_environment(environment):
    with pytest.raises(ValueError):
        replace(config(), environment=environment)


@pytest.mark.parametrize("fault", [
    "cli_missing", "cli_directory", "cli_basename", "config_missing",
    "config_file_missing", "config_file_directory", "config_file_hardlink",
    "relative_config",
])
def test_config_requires_regular_host_materialization(tmp_path, fault):
    original = config()
    if fault == "cli_missing":
        TEST_CLI.unlink()
    elif fault == "cli_directory":
        TEST_CLI.unlink()
        TEST_CLI.mkdir()
    elif fault == "config_file_missing":
        (TEST_CONFIG / "config.json").unlink()
    elif fault == "config_file_directory":
        (TEST_CONFIG / "config.json").unlink()
        (TEST_CONFIG / "config.json").mkdir()
    elif fault == "config_file_hardlink":
        (tmp_path / "linked-config.json").hardlink_to(TEST_CONFIG / "config.json")
    with pytest.raises((ValueError, OSError)):
        if fault == "cli_basename":
            replace(config(), docker_cli=str(tmp_path / "arbitrary.exe"))
        elif fault == "config_missing":
            replace(config(), docker_config=str(tmp_path / "missing"))
        elif fault == "relative_config":
            replace(original, docker_config="relative")
        else:
            replace(original)


async def test_inherited_docker_overrides_cannot_change_frozen_launch(monkeypatch):
    original = config()
    prefix = [original.docker_cli, *original.docker_args]
    private_env = {"PATH": "host-frozen-path", "SYSTEMROOT": "host-frozen-root"}
    frozen = transport.NativeTransportConfig.from_argv(
        prefix, "ab" * 32, env=private_env,
    )
    prefix[-1] = "tcp://mutated-prefix:2375"
    private_env["PATH"] = "mutated-path"
    private_env["DOCKER_HOST"] = "tcp://mutated-dict:2375"
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                 "DOCKER_TLS_VERIFY", "HTTP_PROXY"):
        monkeypatch.setenv(name, "untrusted-inherited-value")
    process = Process()
    response(process)
    calls = mock_spawn(monkeypatch, process)
    async with httpx.AsyncClient(
        transport=transport.NativeHTTPTransport(frozen), timeout=None,
    ) as client:
        await client.get("http://native.invalid/global/health")
    args, kwargs = calls[0]
    assert args[:8] == (
        original.docker_cli, "--config", original.docker_config,
        "--host", original.endpoint, "exec", "-i", "ab" * 32,
    )
    assert kwargs["env"] == {
        "PATH": "host-frozen-path", "SYSTEMROOT": "host-frozen-root",
    }
    assert process.reaped


@pytest.mark.parametrize("method,path", [
    ("GET", "/global/health"), ("GET", "/config"),
    ("GET", "/project/current"), ("POST", "/session"),
    ("POST", "/session/ses_Ab123/message"),
    ("POST", "/session/ses_Ab123/summarize"),
    ("GET", "/session/ses_Ab123/message?limit=100"),
    ("GET", "/session/ses_Ab123/message?limit=100&before=eyJpZCI6MX0%3D"),
])
def test_native_session_route_allowlist(method, path):
    relay.validate_target(method, path)


@pytest.mark.parametrize("method,path", [
    ("GET", "http://evil/session"), ("GET", "//evil/session"),
    ("GET", "/v1/models"), ("POST", "/scripted"),
    ("PATCH", "/config"), ("GET", "/session"), ("DELETE", "/session/ses_a"),
    ("POST", "/session/ses_a/abort"), ("GET", "/session/ses_a/summarize"),
    ("GET", "/session/ses_a%2F../message"), ("GET", "/session/../config"),
    ("GET", "/global/health?host=evil"), ("GET", "/global/health#fragment"),
    ("GET", "/global/health\r\nHost:evil"), ("GET", "/global\\health"),
    ("GET", "/session/ses_a/message?directory=%2Fwork"),
    ("GET", "/session/ses_a/message?limit=100&limit=100"),
    ("GET", "/session/ses_a/message?limit=100&before="),
    ("GET", "/session/ses_a/message?limit=100&before=%0D%0A"),
    ("GET", "/session/ses_a/message?limit=100&before=%GG"),
    ("POST", "/session/ses_a/message?limit=100"),
])
def test_relay_rejects_injection_and_unused_api(method, path):
    with pytest.raises(ValueError):
        relay.validate_target(method, path)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:4096/global/health", "http://evil/global/health",
    "https://native.invalid/global/health", "http://native.invalid:4097/global/health",
    "http://user:password@native.invalid/global/health",
    "http://native.invalid/global/health#x", "http://native.invalid/v1/models",
])
async def test_host_rejects_wrong_origin_before_spawn(monkeypatch, url):
    calls = mock_spawn(monkeypatch, Process())
    bridge = transport.NativeHTTPTransport(config())
    async with httpx.AsyncClient(transport=bridge) as c:
        with pytest.raises(httpx.UnsupportedProtocol):
            await c.get(url)
    assert calls == []


async def test_fixed_command_incremental_input_and_native_response(monkeypatch):
    process = Process()
    response(process, b'{"healthy":true}', headers=[["Content-Length", "16"]])
    calls = mock_spawn(monkeypatch, process)
    async with httpx.AsyncClient(transport=transport.NativeHTTPTransport(config()),
                                 timeout=None) as client:
        result = await client.post(
            "http://native.invalid/session", content=b"x" * 24000,
            headers={"Host": "evil", "Authorization": "secret"},
        )
    assert result.json() == {"healthy": True}
    args, kwargs = calls[0]
    assert args == (config().docker_cli, *config().docker_args,
                    "exec", "-i", config().container_name,
                    "/usr/local/bin/python", "-I", "-S", "-u", "-B",
                    transport.RELAY_PATH)
    assert "-c" not in args and kwargs["limit"] == relay.FRAME_BYTES
    assert kwargs["env"] == {}
    assert len(process.input) > 1
    assert max(map(len, process.input)) <= relay.CHUNK_BYTES
    value = json.loads(b"".join(process.input))
    assert value == {"method": "POST", "path": "/session",
                     "body": base64.b64encode(b"x" * 24000).decode()}
    assert process.stdin_closed and process.reaped


@pytest.mark.parametrize("fault", ["disconnect", "cancel", "content_length"])
async def test_native_session_archives_partial_response_and_reaps(
    tmp_path, monkeypatch, fault,
):
    process = Process()
    prefix = b"partial native health"
    response(process, prefix, headers=[["Content-Length", "100"]], end=False)
    if fault == "disconnect":
        process.stdout.feed_eof()
    elif fault == "content_length":
        process.stdout.feed_data(encode({"end": True}))
        process.finish()
    mock_spawn(monkeypatch, process)
    received = asyncio.Event()
    original = transport.NativeHTTPStream.__aiter__

    async def observe(self):
        async for data in original(self):
            yield data
            received.set()

    monkeypatch.setattr(transport.NativeHTTPStream, "__aiter__", observe)
    journal = Journal.create(tmp_path / "archive")
    session = NativeSession(settings(), journal,
                            transport=transport.NativeHTTPTransport(config()))
    try:
        work = asyncio.create_task(session.start())
        await received.wait()
        if fault == "cancel":
            work.cancel()
        with pytest.raises((httpx.ReadError, asyncio.CancelledError)):
            await work
        record = journal.verify()[-1]
        assert record.value["data"]["complete"] is False
        assert record.files.files[0].content == prefix
        assert process.reaped
        if fault != "content_length":
            assert process.killed
    finally:
        await session.close()
        journal.close()


async def test_cancelled_spawn_and_repeated_close_retain_cli_ownership(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    process = Process()
    process.reap_release.clear()

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return process

    monkeypatch.setattr(transport.asyncio, "create_subprocess_exec", spawn)
    bridge = transport.NativeHTTPTransport(config())
    task = asyncio.create_task(bridge.handle_async_request(
        httpx.Request("GET", "http://native.invalid/global/health"),
    ))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    await process.waiting.wait()
    assert process.killed and not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    process.reap_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.reaped
    await bridge.aclose()
    await bridge.aclose()


async def test_transport_close_cancels_pending_request_and_is_shielded(monkeypatch):
    process = Process()
    process.reap_release.clear()
    calls = mock_spawn(monkeypatch, process)
    bridge = transport.NativeHTTPTransport(config())
    pending = asyncio.create_task(bridge.handle_async_request(
        httpx.Request("GET", "http://native.invalid/global/health"),
    ))
    while not calls:
        await asyncio.sleep(0)
    closing = asyncio.create_task(bridge.aclose())
    await process.waiting.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    process.reap_release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert process.reaped
    await bridge.aclose()
    with pytest.raises(httpx.TransportError, match="closed"):
        await bridge.handle_async_request(
            httpx.Request("GET", "http://native.invalid/global/health"),
        )


async def test_stream_close_interrupts_read_and_waits_for_reap(monkeypatch):
    process = Process()
    response(process, b"", end=False)
    mock_spawn(monkeypatch, process)
    bridge = transport.NativeHTTPTransport(config())
    result = await bridge.handle_async_request(
        httpx.Request("GET", "http://native.invalid/global/health"),
    )
    reading = asyncio.create_task(result.aread())
    await asyncio.sleep(0)
    await bridge.aclose()
    with pytest.raises((asyncio.CancelledError, httpx.ReadError)):
        await reading
    assert process.reaped and not bridge._streams


async def test_stderr_drained_continuously_with_bounded_prefix():
    process = Process()
    stream = transport.NativeHTTPStream(process)
    process.stderr.feed_data(b"x" * 200000)
    process.stderr.feed_eof()
    await stream.stderr
    assert stream.stderr_prefix == b"x" * transport.STDERR_BYTES
    assert process.stderr.at_eof()
    await stream.aclose()
    assert process.reaped


@pytest.mark.parametrize("suffix", [
    b"", b'{"chunk":"eA=="}', b'[]\n', b'{"chunk":"!"}\n',
    b'{"end":1}\n', b'{"chunk":""}\n', b'{"chunk":"eA==","chunk":"eQ=="}\n',
    chunk(b"x" * (relay.CHUNK_BYTES + 1)), b"x" * (relay.FRAME_BYTES + 2) + b"\n",
    encode({"end": True}) + b"trailing",
], ids=["eof", "no-newline", "array", "base64", "bool", "empty", "duplicate",
        "large-chunk", "large-frame", "trailing"])
async def test_bad_frames_fail_closed_and_reap(suffix):
    process = Process()
    process.stdout.feed_data(suffix)
    process.finish()
    stream = transport.NativeHTTPStream(process)
    with pytest.raises(httpx.ReadError):
        async for _ in stream:
            pass
    assert process.reaped


@pytest.mark.parametrize("headers", [
    [["Content-Length", "3"], ["content-length", "3"]],
    [["Content-Length", "-1"]], [["Content-Length", "NaN"]],
    [["Content-Length", "3"], ["Transfer-Encoding", "chunked"]],
    [["Content-Encoding", "gzip"]], [["X-Test", "bad\r\nvalue"]],
])
async def test_invalid_headers_reap_before_return(monkeypatch, headers):
    process = Process()
    response(process, headers=headers)
    mock_spawn(monkeypatch, process)
    bridge = transport.NativeHTTPTransport(config())
    with pytest.raises(httpx.ReadError):
        await bridge.handle_async_request(
            httpx.Request("GET", "http://native.invalid/global/health"),
        )
    assert process.reaped
    await bridge.aclose()


async def test_stdout_backpressure_reads_only_requested_frames():
    process = Process()
    first, second = chunk(b"one"), chunk(b"two")
    process.stdout.feed_data(first + second)
    stream = transport.NativeHTTPStream(process)
    iterator = stream.__aiter__()
    assert await anext(iterator) == b"one"
    await asyncio.sleep(0)
    assert bytes(process.stdout._buffer) == second
    await iterator.aclose()
    assert process.reaped


async def test_host_enforces_existing_response_bound_with_prefix(monkeypatch):
    # Shrinking only the test limit exercises the same overflow boundary.
    monkeypatch.setattr(transport, "MAX_FILE_BYTES", 3)
    process = Process()
    process.stdout.feed_data(chunk(b"abcd"))
    process.finish()
    stream = transport.NativeHTTPStream(process)
    received = bytearray()
    with pytest.raises(httpx.ReadError, match="exceeds bound"):
        async for data in stream:
            received.extend(data)
    assert received == b"abc" and process.reaped
    assert relay.BODY_BYTES == 16 * 1024 * 1024


@pytest.mark.parametrize("fault", ["stderr", "exit"])
async def test_end_frame_requires_clean_cli_exit(fault):
    process = Process()
    process.stdout.feed_data(encode({"end": True}))
    if fault == "stderr":
        process.stderr.feed_data(b"native failure")
    process.finish(1 if fault == "exit" else 0)
    stream = transport.NativeHTTPStream(process)
    with pytest.raises(httpx.ReadError, match="cleanly"):
        async for _ in stream:
            pass
    assert process.reaped


async def test_spawn_failure_is_observed_without_orphaned_tasks(monkeypatch):
    async def spawn(*args, **kwargs):
        raise OSError("fixture spawn failure")

    monkeypatch.setattr(transport.asyncio, "create_subprocess_exec", spawn)
    bridge = transport.NativeHTTPTransport(config())
    with pytest.raises(OSError, match="fixture spawn failure"):
        await bridge.handle_async_request(
            httpx.Request("GET", "http://native.invalid/global/health"),
        )
    await bridge.aclose()
    assert not bridge._streams and not bridge._requests


async def test_stdin_write_failure_closes_and_reaps(monkeypatch):
    process = Process()

    async def broken():
        raise BrokenPipeError("fixture closed stdin")

    process.stdin.drain = broken
    mock_spawn(monkeypatch, process)
    bridge = transport.NativeHTTPTransport(config())
    with pytest.raises(BrokenPipeError, match="fixture closed stdin"):
        await bridge.handle_async_request(
            httpx.Request("POST", "http://native.invalid/session", content=b"{}"),
        )
    assert process.killed and process.reaped and process.stdin_closed
    await bridge.aclose()


@pytest.mark.parametrize("method,body", [("GET", b"x"), ("POST", b"long")])
async def test_invalid_request_body_rejected_before_spawn(monkeypatch, method, body):
    monkeypatch.setattr(transport, "MAX_FILE_BYTES", 3)
    calls = mock_spawn(monkeypatch, Process())
    bridge = transport.NativeHTTPTransport(config())
    route = "/global/health" if method == "GET" else "/session"
    with pytest.raises(httpx.WriteError):
        await bridge.handle_async_request(
            httpx.Request(method, "http://native.invalid" + route, content=body),
        )
    assert not calls
    await bridge.aclose()


async def test_response_length_overflow_is_not_accepted():
    process = Process()
    process.stdout.feed_data(chunk(b"long"))
    process.finish()
    stream = transport.NativeHTTPStream(process)
    stream.set_header({"status": 200, "headers": [["Content-Length", "3"]]})
    received = bytearray()
    with pytest.raises(httpx.ReadError, match="exceeds Content-Length"):
        async for data in stream:
            received.extend(data)
    assert received == b"long" and process.reaped


def test_standalone_relay_preserves_bounded_prefix_on_overflow(monkeypatch):
    monkeypatch.setattr(relay, "BODY_BYTES", 3)
    closed = []

    class Response:
        status = 200
        length = None

        def getheaders(self):
            return []

        def read1(self, size):
            return b"long"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    connection = SimpleNamespace(
        request=lambda *args, **kwargs: None, getresponse=Response,
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(relay.http.client, "HTTPConnection",
                        lambda *args, **kwargs: connection)
    output = io.BytesIO()
    with pytest.raises(ValueError, match="exceeds bound"):
        relay.relay(io.BytesIO(encode({"method": "GET", "path": "/global/health",
                                       "body": ""})), output)
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames == [{"status": 200, "headers": []},
                      {"chunk": base64.b64encode(b"lon").decode()}]
    assert closed == [True]


@pytest.mark.parametrize("kind", ["request", "body", "frame"])
def test_standalone_relay_enforces_wire_bounds_without_connect(monkeypatch, kind):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not connect")

    monkeypatch.setattr(relay.http.client, "HTTPConnection", forbidden)
    if kind == "request":
        monkeypatch.setattr(relay, "REQUEST_BYTES", 3)
    if kind == "body":
        monkeypatch.setattr(relay, "BODY_BYTES", 3)
    with pytest.raises(ValueError, match="bound|body"):
        if kind == "frame":
            relay.emit(io.BytesIO(), {"headers": "x" * relay.FRAME_BYTES})
        else:
            relay.relay(io.BytesIO(encode({
                "method": "POST", "path": "/session",
                "body": base64.b64encode(b"long").decode(),
            })), io.BytesIO())


@pytest.mark.parametrize("truncated,status", [(False, 200), (True, 200), (False, 302),
                                              (False, 500)])
def test_standalone_relay_fixed_loopback_and_content_length(
    monkeypatch, truncated, status,
):
    calls = []

    class Response:
        def __init__(self):
            self.status = status
            self.length = 8 if truncated else 3
            self.chunks = [b"abc", b""]

        def getheaders(self):
            return [("Content-Length", str(self.length)), ("Location", "http://evil")]

        def read1(self, size):
            assert size == relay.CHUNK_BYTES
            data = self.chunks.pop(0)
            self.length -= len(data)
            return data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Connection:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def request(self, *args, **kwargs):
            calls.append((args, kwargs))

        def getresponse(self):
            return Response()

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(relay.http.client, "HTTPConnection", Connection)
    monkeypatch.setenv("HTTP_PROXY", "http://evil")
    source = io.BytesIO(encode({"method": "GET", "path": "/global/health", "body": ""}))
    output = io.BytesIO()
    if truncated:
        with pytest.raises(ValueError, match="before Content-Length"):
            relay.relay(source, output)
    else:
        relay.relay(source, output)
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[0]["status"] == status
    assert base64.b64decode(frames[1]["chunk"]) == b"abc"
    assert ({"end": True} in frames) is not truncated
    assert calls[0] == (("127.0.0.1", 4096), {"timeout": None})
    assert calls[1] == (("GET", "/global/health"), {
        "body": None, "headers": {"Content-Type": "application/json",
                                   "Accept-Encoding": "identity"},
    })
    assert calls[2:] == ["closed"]


@pytest.mark.parametrize("payload", [
    {"method": "GET", "path": "//evil", "body": ""},
    {"method": "GET", "path": "/global/health", "body": "", "host": "evil"},
    {"method": "GET", "path": "/global/health", "body": "eA=="},
    {"method": "POST", "path": "/session", "body": "!"},
])
def test_standalone_relay_rejects_before_connect(monkeypatch, payload):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not connect")

    monkeypatch.setattr(relay.http.client, "HTTPConnection", forbidden)
    with pytest.raises(ValueError):
        relay.relay(io.BytesIO(encode(payload)), io.BytesIO())


@pytest.mark.parametrize("raw", [
    b"", b"\n", b"{}\n", b"{broken\n",
    b'{"body":"","method":"GET","path":"/global/health"}',
    b'{"body":"","method":"GET","path":"/global/health"}\r\n',
    b'{"path":"/global/health","body":"","method":"GET"}\n',
    b'{"body": "", "method": "GET", "path": "/global/health"}\n',
    b'{"body":"","method":"GET","path":"/global/health","body":""}\n',
    b'{"body":"","method":"GET","path":"/global/health"} {}\n',
    b'{"body":"","method":"GET","path":"/global/\\u0068ealth"}\n',
    b'{"body":NaN,"method":"GET","path":"/global/health"}\n',
    b'\xef\xbb\xbf{"body":"","method":"GET","path":"/global/health"}\n',
], ids=["empty", "blank", "fields", "json", "unterminated", "crlf", "order",
        "whitespace", "duplicate", "trailing-json", "escaped", "nan", "bom"])
def test_relay_rejects_malformed_or_noncanonical_request_line(monkeypatch, raw):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid frame must never reach the native API")

    monkeypatch.setattr(relay.http.client, "HTTPConnection", forbidden)
    with pytest.raises(ValueError):
        relay.relay(io.BytesIO(raw), io.BytesIO())


@pytest.mark.parametrize("at_bound", [False, True])
def test_relay_completes_canonical_line_while_real_pipe_writer_remains_open(
    monkeypatch, at_bound,
):
    payload = encode({"method": "GET", "path": "/global/health", "body": ""})
    if at_bound:
        monkeypatch.setattr(relay, "REQUEST_BYTES", len(payload))
    connected, closed = threading.Event(), threading.Event()
    finished = threading.Event()
    failures = []

    class Response:
        status = 200
        length = 2

        def getheaders(self):
            return [("Content-Length", "2")]

        def read1(self, size):
            if self.length:
                self.length = 0
                return b"{}"
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def connection(host, port, *, timeout):
        assert (host, port, timeout) == ("127.0.0.1", 4096, None)
        connected.set()
        return SimpleNamespace(request=lambda *args, **kwargs: None,
                               getresponse=Response, close=closed.set)

    monkeypatch.setattr(relay.http.client, "HTTPConnection", connection)
    reader_fd, writer_fd = os.pipe()
    with os.fdopen(reader_fd, "rb") as source, os.fdopen(writer_fd, "wb", 0) as writer:
        output = io.BytesIO()

        def run():
            try:
                relay.relay(source, output)
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            writer.write(payload)
            assert finished.wait(3), "Relay waited for EOF after the request newline"
            assert not writer.closed and connected.is_set() and closed.is_set()
            assert not failures
            assert [json.loads(line) for line in output.getvalue().splitlines()] == [
                {"status": 200, "headers": [["Content-Length", "2"]]},
                {"chunk": base64.b64encode(b"{}").decode()}, {"end": True},
            ]
        finally:
            writer.close()
            worker.join(3)
            assert not worker.is_alive()


def test_relay_request_line_overflow_rejected_before_connection(monkeypatch):
    payload = encode({"method": "GET", "path": "/global/health", "body": ""})
    monkeypatch.setattr(relay, "REQUEST_BYTES", len(payload) - 1)

    def forbidden(*args, **kwargs):
        raise AssertionError("Oversized frame must never reach the native API")

    monkeypatch.setattr(relay.http.client, "HTTPConnection", forbidden)
    with pytest.raises(ValueError, match="exceeds bound"):
        relay.relay(io.BytesIO(payload), io.BytesIO())
