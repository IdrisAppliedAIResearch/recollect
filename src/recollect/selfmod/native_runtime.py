"""Owned native executor: attested PID 1, supervisor IPC and candidate capture.

Construct through NativeAdmission.start(); the constructor is inert and release()
only schedules the owned launch on the supplied event loop. Native HTTP requests,
live history reads and model exchanges all cross the supervisor control pipe, so
the host issues no Docker exec until the bound terminal collection has finished.
Later evidence reads are root execs gated on that frame and serialized by a lock.

There is no work, idle, token or call deadline. Unknown namespace ownership is
retained rather than inferred. Without a pinned modifier slot, completed provider
HTTP bodies are the only upstream settlement claimed. With one, the broker's
pinned slot going idle is required; neither is a whole-GPU quiescence measurement.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from . import native_http_relay as wire
from .contracts import File, Snapshot
from .files import materialize
from .journal import IntegrityError, Journal, decode, encode, root_path, sha256
from .model_settlement import SlotSettlement
from .native import NativeSession, _durable, _settle
from .native_admission import NativeCandidate, NativeStop
from .native_broker import BrokerIdentity, NativeModelBroker
from .native_capture import (
    NativeCaptureSpec,
    capture_request,
    verify_capture,
    verify_stop,
)
from .native_containment import NativeRuntimeSpec, attest, create_arguments
from .native_history_reader import MAX_PAGE_BYTES, HistoryReadError, strict_json
from .native_transport import NativeTransportConfig

PYTHON = ("/usr/local/bin/python", "-I", "-S", "-u", "-B")
IPC_CHUNK_BYTES = 32768
CONTROL_BYTES = 4 * 1024 * 1024
CLI_BYTES = 64 * 1024 * 1024
STDERR_BYTES = 65536
MODEL_REQUEST_BYTES = 16 * 1024 * 1024
STATE_CHUNK_BYTES = 8 * 1024 * 1024
STATE_FILES = ("opencode.db", "opencode.db-wal", "opencode.db-shm")
CONTROLS = {"ready", "native_started", "native_listening", "fenced",
            "terminal_collection_finished"}


@dataclass(frozen=True)
class NativeDocker:
    """Pinned CLI prefix and minimal environment; no ambient Docker context."""

    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]

    def __post_init__(self):
        # Reuse the transport's CLI, private-config and environment checks.
        NativeTransportConfig.from_argv(self.argv, "validation",
                                        env=dict(self.environment))

    async def spawn(self, *args, limit=65536):
        return await asyncio.create_subprocess_exec(
            *self.argv, *args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=limit, env=dict(self.environment),
            **({"creationflags": subprocess.CREATE_NO_WINDOW}
               if sys.platform == "win32" else {}),
        )

    async def run(self, *args, data=None, limit=CLI_BYTES):
        """Complete one CLI command; caller cancellation waits for its settlement."""
        return await _settle(asyncio.create_task(self._run(args, data, limit)))

    async def _run(self, args, data, limit):
        process = await self.spawn(*args)

        async def read(stream, bound):
            result = bytearray()
            while chunk := await stream.read(65536):
                if len(result) + len(chunk) > bound:
                    raise IntegrityError("Docker CLI output exceeds bound")
                result.extend(chunk)
            return bytes(result)

        async def send():
            try:
                if data:
                    process.stdin.write(data)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        tasks = [asyncio.create_task(read(process.stdout, limit)),
                 asyncio.create_task(read(process.stderr, STDERR_BYTES)),
                 asyncio.create_task(send())]
        try:
            out, err, _ = await asyncio.gather(*tasks)
            return await process.wait(), out, err
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await process.wait()


def _response_length(status, headers):
    if (type(status) is not int or not 200 <= status <= 599
            or type(headers) is not list):
        raise httpx.ReadError("Invalid native relay headers")
    lengths, encodings, transfers = [], [], []
    for pair in headers:
        if (type(pair) is not list or len(pair) != 2
                or any(type(v) is not str for v in pair)):
            raise httpx.ReadError("Invalid native relay header pair")
        key, value = pair
        if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                or any(ord(c) < 32 or ord(c) > 126 for c in value)):
            raise httpx.ReadError("Invalid native relay header bytes")
        {"content-length": lengths, "content-encoding": encodings,
         "transfer-encoding": transfers}.get(key.lower(), []).append(value)
    if (len(lengths) > 1 or (lengths and (
            not re.fullmatch(r"[0-9]+", lengths[0]) or len(lengths[0]) > 20))
            or (encodings and encodings != ["identity"])
            or (transfers and (transfers != ["chunked"] or lengths))):
        raise httpx.ReadError("Invalid native response framing headers")
    return int(lengths[0]) if lengths else None


def _history_result(code, raw):
    if code == 0:
        return raw
    try:
        value = strict_json(raw)
        message = str(value["error"])[:1024]
        prefix = base64.b64decode(value["raw_prefix"], validate=True)
    except Exception:
        message, prefix = "Native history reader failed", raw[:MAX_PAGE_BYTES]
    raise HistoryReadError(message, raw_prefix=prefix)


class SupervisorChannel:
    """Demultiplex one attested supervisor pipe without cross-lane blocking.

    The reader never awaits a lane consumer. Helper lanes may hold at most one
    unacknowledged frame, so their queues are bounded by the credit protocol.
    Model request chunks are bounded by the proxy's request size. Any protocol
    violation latches failure; later waits raise instead of hanging silently.
    """

    def __init__(self, write, binding):
        self._write, self.binding = write, dict(binding)
        self.controls, self._events = {}, {k: asyncio.Event() for k in CONTROLS}
        self.trace = []
        self.model = asyncio.Queue()
        self._http, self._history = asyncio.Queue(), asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self.http_lock, self.history_lock = asyncio.Lock(), asyncio.Lock()
        self._http_next = self._history_next = 0
        self._writes = set()
        self.model_assembling = False
        self.failure = None
        self.failed = asyncio.Event()
        self.closing = asyncio.Event()
        self.reader_done = asyncio.Event()

    def fail(self, error):
        if self.failure is None:
            self.failure = error
        self.failed.set()

    async def until(self, awaitable, *, terminal=True, failure=True, closing=True):
        """Await work, but wake on failure, closing or supervisor terminal collection.

        PID 1 may reach terminal collection on its own (native exit, rejected
        frame); no further lane output can follow, so waiting would never end.
        The closing fence send disables failure/closing wakes so it is delivered.
        """
        work = asyncio.ensure_future(awaitable)
        wakes = []
        if failure:
            wakes.append(self.failed)
        if closing:
            wakes.append(self.closing)
        if terminal:
            wakes.append(self._events["terminal_collection_finished"])
        waiters = [asyncio.create_task(event.wait()) for event in wakes]
        try:
            await asyncio.wait((work, *waiters), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
        if not work.cancelled():
            return work.result()
        if self.failure is None and self.closing.is_set():
            raise IntegrityError("Native runtime closing")
        if self.failure is None:
            raise IntegrityError("Native supervisor reached terminal collection")
        raise IntegrityError("Native supervisor channel failed") from self.failure

    def _collected(self):
        if "terminal_collection_finished" in self.controls:
            raise IntegrityError("Native supervisor already reached collection")

    async def read(self, stream):
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    raise EOFError("Supervisor control output closed")
                if not raw.endswith(b"\n") or len(raw) > CONTROL_BYTES:
                    raise IntegrityError("Unterminated supervisor frame")
                frame = decode(raw)
                kind = frame.get("kind")
                if kind in CONTROLS:
                    self._control(kind, frame, raw)
                elif type(kind) is str and kind.startswith("model_"):
                    self._put(self.model, frame, 2048)
                elif type(kind) is str and kind.startswith("http_"):
                    self._put(self._http, frame, 1)
                elif type(kind) is str and kind.startswith("history_"):
                    self._put(self._history, frame, 1)
                else:
                    raise IntegrityError("Unexpected supervisor output frame")
        except BaseException as error:
            self.fail(error)
        finally:
            self.reader_done.set()

    @staticmethod
    def _put(lane, frame, bound):
        if lane.qsize() >= bound:
            raise IntegrityError("Supervisor exceeded granted lane credit")
        lane.put_nowait(frame)

    def _control(self, kind, frame, raw):
        expected = {"kind", *self.binding}
        if kind == "native_started":
            expected.add("native")
        elif kind == "terminal_collection_finished":
            expected |= {"collector_returncode", "failed"}
        if (kind in self.controls or set(frame) != expected
                or any(frame[k] != v for k, v in self.binding.items())):
            raise IntegrityError("Unbound, duplicate or malformed supervisor control")
        if kind == "native_started":
            native = frame["native"]
            if (type(native) is not dict or set(native) != {"pid", "start"}
                    or any(type(v) is not int or v < 1 for v in native.values())):
                raise IntegrityError("Invalid native process identity")
        if kind == "terminal_collection_finished" and (
                type(frame["failed"]) is not bool
                or frame["collector_returncode"] is not None
                and type(frame["collector_returncode"]) is not int):
            raise IntegrityError("Invalid terminal collection frame")
        self.controls[kind] = frame
        self.trace.append(raw)
        self._events[kind].set()

    async def control(self, kind, *, terminal=True):
        if kind not in self.controls:
            await self.until(self._events[kind].wait(), terminal=terminal)
        return self.controls[kind]

    async def terminal(self):
        """Wait for bound collection or loss of the control stream, never a timer."""
        waits = [asyncio.create_task(e.wait()) for e in (
            self._events["terminal_collection_finished"], self.reader_done)]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()
        return self.controls.get("terminal_collection_finished")

    async def send(self, frame, *, urgent=False):
        """Write one frame without letting a stopped PID 1 reader block the host.

        The write stays owned by a tracked task even when this wait is abandoned;
        a possibly partial frame means the pipe is failed and never reused.
        """
        raw = encode(frame)

        async def write():
            async with self._send_lock:
                await self._write(raw)

        task = asyncio.create_task(write())
        self._writes.add(task)
        task.add_done_callback(self._write_done)
        try:
            # An urgent terminal fence is still delivered after host failure.
            await self.until(asyncio.shield(task), failure=not urgent,
                             closing=not urgent)
        except BaseException as error:
            self.fail(error)
            raise

    def _write_done(self, task):
        self._writes.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.fail(task.exception())

    async def settle_writes(self):
        """After the attachment is closed, collect every abandoned write task."""
        await asyncio.gather(*tuple(self._writes), return_exceptions=True)

    async def http(self, method, path, body):
        stream = None
        acquired = False
        try:
            self._collected()
            acquired = await self.until(self.http_lock.acquire())
            identity, self._http_next = self._http_next, self._http_next + 1
            await self.send({"kind": "http_request", "id": identity, "method": method,
                             "path": path, "bytes": len(body)})
            chunks = 0
            for offset in range(0, len(body), IPC_CHUNK_BYTES):
                await self.send({"kind": "http_request_chunk", "id": identity,
                                 "sequence": chunks, "base64": base64.b64encode(
                                     body[offset:offset + IPC_CHUNK_BYTES]).decode()})
                chunks += 1
            await self.send({"kind": "http_request_end", "id": identity,
                             "chunks": chunks})
            head = await self.http_frame(identity, 0)
            if head["kind"] != "http_head":
                raise IntegrityError("Native HTTP response lacks headers")
            length = _response_length(head["status"], head["headers"])
            await self.send({"kind": "http_ack", "id": identity, "sequence": 0})
            stream = _IPCStream(self, identity, length)
            return httpx.Response(head["status"],
                                  headers=[tuple(p) for p in head["headers"]],
                                  stream=stream)
        except BaseException as error:
            # An unfinished operation fences the run; the failed lane is not reused.
            self.fail(error)
            if acquired and stream is None:
                self.http_lock.release()
            raise

    async def http_frame(self, identity, sequence):
        frame = await self.until(self._http.get())
        fields = {"http_head": {"status", "headers"}, "http_chunk": {"base64"},
                  "http_end": set()}.get(frame.get("kind"))
        if (fields is None or set(frame) != {"kind", "id", "sequence", *fields}
                or frame["id"] != identity or frame["sequence"] != sequence):
            raise IntegrityError("Stale or malformed native HTTP response frame")
        return frame

    async def history(self, request):
        acquired = False
        try:
            self._collected()
            acquired = await self.until(self.history_lock.acquire())
            identity = self._history_next
            self._history_next += 1
            await self.send({"kind": "history_request", "id": identity,
                             "request": request})
            data, sequence = bytearray(), 0
            while True:
                frame = await self.until(self._history.get())
                kind = frame.get("kind")
                fields = {"history_chunk": {"base64"},
                          "history_end": {"returncode", "stderr"}}.get(kind)
                if (fields is None
                        or set(frame) != {"kind", "id", "sequence", *fields}
                        or frame["id"] != identity
                        or frame["sequence"] != sequence):
                    raise IntegrityError("Stale or malformed history frame")
                if kind == "history_chunk":
                    chunk = base64.b64decode(frame["base64"], validate=True)
                    if not chunk or len(data) + len(chunk) > MAX_PAGE_BYTES + 4096:
                        raise IntegrityError("Native history page exceeds bound")
                    data.extend(chunk)
                elif (type(frame["returncode"]) is not int
                      or type(frame["stderr"]) is not str):
                    raise IntegrityError("Invalid native history terminator")
                await self.send({"kind": "history_ack", "id": identity,
                                 "sequence": sequence})
                if kind == "history_end":
                    return _history_result(frame["returncode"], bytes(data))
                sequence += 1
        except BaseException as error:
            if not isinstance(error, HistoryReadError):
                self.fail(error)
            raise
        finally:
            if acquired:
                self.history_lock.release()

    async def model_request(self):
        """Assemble the proxy's next request; the proxy admits one at a time."""
        start = await self.model.get()
        if (start.get("kind") != "model_request"
                or set(start) != {"kind", "id", "bytes"}
                or type(start["id"]) is not str
                or not re.fullmatch(r"[0-9a-f]{32}", start["id"])
                or type(start["bytes"]) is not int
                or not 0 < start["bytes"] <= MODEL_REQUEST_BYTES):
            raise IntegrityError("Invalid native model request")
        identity, body, sequence = start["id"], bytearray(), 0
        # Visible to the terminal idle check while chunks are still arriving.
        self.model_assembling = True
        while True:
            frame = await self.model.get()
            kind = frame.get("kind")
            if (kind == "model_request_chunk"
                    and set(frame) == {"kind", "id", "sequence", "base64"}
                    and frame["id"] == identity and frame["sequence"] == sequence
                    and type(frame["base64"]) is str):
                data = base64.b64decode(frame["base64"], validate=True)
                if not data or len(body) + len(data) > start["bytes"]:
                    raise IntegrityError("Native model request exceeds its size")
                body.extend(data)
                sequence += 1
            elif (kind == "model_request_end" and set(frame) == {"kind", "id", "chunks"}
                  and frame["id"] == identity and frame["chunks"] == sequence
                  and len(body) == start["bytes"]):
                self.model_assembling = False
                return identity, bytes(body)
            else:
                raise IntegrityError("Invalid native model request stream")

    async def model_data(self, frame):
        await self.send(frame)
        acknowledgement = await self.model.get()
        if acknowledgement != {"kind": "model_ack", "id": frame["id"],
                               "sequence": frame["sequence"]}:
            raise IntegrityError("Invalid native model acknowledgement")

    async def model_closed(self, identity):
        if await self.model.get() != {"kind": "model_closed", "id": identity,
                                      "response_delivered": True}:
            raise IntegrityError("Native model response delivery unconfirmed")


class _IPCStream(httpx.AsyncByteStream):
    def __init__(self, channel, identity, length):
        self._channel, self._identity, self._length = channel, identity, length
        self._complete = self._closed = False

    async def __aiter__(self):
        total, sequence = 0, 1
        try:
            while True:
                frame = await self._channel.http_frame(self._identity, sequence)
                if frame["kind"] == "http_end":
                    if self._length is not None and total != self._length:
                        raise httpx.ReadError("Native response ended before length")
                    await self._channel.send({"kind": "http_ack",
                                              "id": self._identity,
                                              "sequence": sequence})
                    self._complete = True
                    return
                if frame["kind"] != "http_chunk" or type(frame["base64"]) is not str:
                    raise httpx.ReadError("Invalid native response chunk")
                data = base64.b64decode(frame["base64"], validate=True)
                total += len(data)
                if (not 0 < len(data) <= wire.CHUNK_BYTES or total > wire.BODY_BYTES
                        or self._length is not None and total > self._length):
                    raise httpx.ReadError("Native response exceeds bound")
                await self._channel.send({"kind": "http_ack", "id": self._identity,
                                          "sequence": sequence})
                sequence += 1
                yield data
        except BaseException as error:
            self._channel.fail(error)
            raise
        finally:
            await self.aclose()

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        if not self._complete:
            # Abandoning a native response leaves its effects unknown: no replay.
            self._channel.fail(IntegrityError("Incomplete native HTTP operation"))
        else:
            self._channel.http_lock.release()


class NativeIPCTransport(httpx.AsyncBaseTransport):
    """NativeSession transport over PID 1; validation mirrors the relay's routes."""

    def __init__(self, channel):
        if type(channel) is not SupervisorChannel:
            raise ValueError("An owned supervisor channel is required")
        self._channel = channel

    async def handle_async_request(self, request):
        url = request.url
        if (url.scheme != "http" or url.raw_host != b"native.invalid"
                or url.port not in (None, 80) or url.userinfo or url.fragment):
            raise httpx.UnsupportedProtocol("Forbidden native origin", request=request)
        try:
            path = url.raw_path.decode("ascii")
            wire.validate_target(request.method, path)
        except (ValueError, UnicodeError) as error:
            raise httpx.UnsupportedProtocol(str(error), request=request) from error
        body = bytearray()
        async for chunk in request.stream:
            if len(body) + len(chunk) > wire.BODY_BYTES:
                raise httpx.WriteError("Native request exceeds bound", request=request)
            body.extend(chunk)
        if request.method == "GET" and body:
            raise httpx.WriteError("Native GET body is forbidden", request=request)
        return await self._channel.http(request.method, path, bytes(body))


class NativeRuntime:
    """One owned native namespace for one admission.

    The trusted host factory must construct this inertly inside admission.start.
    Drive work through started()/prompt()/compact(), hand off through
    admission.handoff(), and always settle with admission.close().
    """

    def __init__(self, admission, *, docker, sandbox_root, archive_root, image_id,
                 image_environment, native_binary_sha256, baseline, loop):
        if type(docker) is not NativeDocker:
            raise ValueError("Freeze the native Docker CLI")
        self._admission, self.run = admission, admission.run
        self.capture_spec = NativeCaptureSpec(admission.run, admission.settings,
                                              baseline)
        self.spec = NativeRuntimeSpec(self.capture_spec, image_id,
                                      tuple(image_environment), native_binary_sha256)
        self._docker, self._loop = docker, loop
        self._sandbox_root, self._archive_root = Path(sandbox_root), Path(archive_root)
        self._input_dir = self._sandbox_root / ("selfmod-native-" + self.run.run_id)
        self._archive = self._archive_root / ("native-" + self.run.run_id)
        self.channel = SupervisorChannel(self._write, {
            "run_id": self.run.run_id, "runtime_sha256": self.spec.sha256})
        self._journals = {}
        self._main = self._close_task = None
        self._container = self._attachment = self._native = None
        self._reader = self._stderr = self._pump = None
        self.session = self._broker = None
        self._create_attempted = self._released = self._captured = False
        self._finish_started = self._closing = False
        self._upstream_active = self._exchange_active = False
        self._exec_lock = asyncio.Lock()
        self._forwards = 0
        self._namespace_stopped = False
        self._retained = None
        self._summary = None

    async def _write(self, raw):
        self._attachment.stdin.write(raw)
        await self._attachment.stdin.drain()

    async def _record(self, kind, data, files=()):
        await _durable(self._journals["runtime"].append, "native_runtime_" + kind,
                       {"run_id": self.run.run_id, **data}, Snapshot(tuple(files)))

    def release(self):
        """Schedule the owned launch once, beneath the admission release fence."""
        if self._main is not None:
            raise IntegrityError("Native runtime release is one-use")
        self._main = asyncio.run_coroutine_threadsafe(self._launch(), self._loop)

    async def started(self):
        if self._main is None:
            raise IntegrityError("Native runtime was not released")
        await asyncio.shield(asyncio.wrap_future(self._main))

    def _prepare(self):
        root_path(self._sandbox_root)
        root_path(self._archive_root)
        self._archive.mkdir(mode=0o700)
        for name in ("runtime", "session", "history", "broker"):
            self._journals[name] = Journal.create(self._archive / name)
        materialize(self._input_dir, self.spec.inputs)

    async def _inspect(self):
        code, raw, err = await self._docker.run("inspect", self._container)
        value = json.loads(raw) if code == 0 else None
        if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
            raise IntegrityError("Ambiguous native namespace inspection")
        return raw, value[0]

    def _open(self):
        if self.channel.failure is not None:
            raise IntegrityError("Native runtime is closing or failed") from (
                self.channel.failure)

    async def _exec(self, *args, data=None, limit=CLI_BYTES):
        """Post-collection root exec; one at a time so no two join a census."""
        await self._evidence_gate()
        async with self._exec_lock:
            return await self._docker.run("exec", *args, data=data, limit=limit)

    async def _launch(self):
        try:
            await _durable(self._prepare)
            self._open()
            self._create_attempted = True
            code, out, err = await self._docker.run(
                *create_arguments(self.spec, self._input_dir))
            await self._record("create", {"returncode": code}, (
                File("stdout.txt", out), File("stderr.txt", err)))
            identity = out.decode("ascii", "replace").strip()
            if code or not re.fullmatch(r"[0-9a-f]{64}", identity):
                raise IntegrityError("Native namespace creation failed")
            self._container = identity
            raw, value = await self._inspect()
            await _durable(attest, value, self.spec, self._input_dir, identity)
            await self._record("attested", {"container_id": identity},
                               (File("inspection.json", raw),))
            self._open()
            self._attachment = await _settle(asyncio.create_task(self._docker.spawn(
                "start", "--attach", "--interactive", identity,
                limit=CONTROL_BYTES + 1)))
            self._reader = asyncio.create_task(
                self.channel.read(self._attachment.stdout))
            self._stderr = asyncio.create_task(self._drain_stderr())
            await self.channel.control("ready")
            self._open()
            # Mark possible side effects before the release bytes are written.
            self._released = True
            await self.channel.send(decode(self.spec.control("release")))
            self._native = (await self.channel.control("native_started"))["native"]
            await self.channel.control("native_listening")
            settings = self._admission.broker_settings
            self._broker = NativeModelBroker(
                settings, self._journals["broker"],
                settlement=(SlotSettlement(settings.base_url, settings.slot)
                            if settings.slot is not None else None),
            )
            self._pump = asyncio.create_task(self._model_pump())
            self.session = NativeSession(
                self._admission.settings, self._journals["session"],
                transport=NativeIPCTransport(self.channel),
                history_reader=self._read_history,
                history_journal=self._journals["history"],
            )
            await self.channel.until(self.session.start())
        except BaseException as error:
            self.channel.fail(error)
            raise

    async def _drain_stderr(self):
        prefix = bytearray()
        while chunk := await self._attachment.stderr.read(65536):
            prefix.extend(chunk[:max(0, STDERR_BYTES - len(prefix))])
        self._attachment_stderr = bytes(prefix)

    async def prompt(self, text):
        await self.started()
        return await self.channel.until(self.session.prompt(text))

    async def compact(self):
        await self.started()
        return await self.channel.until(self.session.compact())

    async def _read_history(self, request):
        if "terminal_collection_finished" not in self.channel.controls:
            return await self.channel.history(request)
        code, out, _ = await self._exec(
            "-i", "--user", "0:0", self._container, *PYTHON,
            "/authority/capture_history.py", data=encode(request),
            limit=MAX_PAGE_BYTES + 65536)
        return _history_result(code, out)

    async def _model_pump(self):
        try:
            while True:
                identity, body = await self.channel.model_request()
                # No await between assembly and claiming the exchange.
                self._exchange_active = True
                await self._exchange(identity, body)
                self._exchange_active = False
        except asyncio.CancelledError:
            if not self._closing:
                self.channel.fail(IntegrityError("Native model pump cancelled"))
            raise
        except BaseException as error:
            self.channel.fail(error)
            raise

    async def _exchange(self, identity, body):
        session = self.session
        if session is None or session.session_id is None:
            raise IntegrityError("Native model request preceded its session")
        history = session.history
        broker_identity = BrokerIdentity(self.run.run_id, session.session_id)
        # Bind the last verified durable head. Starting a capture here could race
        # the native HTTP operation's own capture, which rejects concurrent use.
        if history is not None and not history.poisoned:
            head = history.durable_head
            if head["seq"] >= 0:
                broker_identity = BrokerIdentity(self.run.run_id, session.session_id,
                                                 head["seq"], head["sha256"])
        sequence = 0

        async def on_response(head):
            nonlocal sequence
            content_type = next((v for k, v in head.headers
                                 if k.lower() == b"content-type"), b"application/json")
            text = content_type.decode("ascii", "replace")
            if not re.fullmatch(r"[\x20-\x7e]{1,256}", text):
                text = "application/octet-stream"
            await self.channel.model_data({
                "kind": "model_head", "id": identity, "sequence": 0,
                "status": head.status_code, "content_type": text})
            sequence = 1

        async def on_chunk(data):
            nonlocal sequence
            for offset in range(0, len(data), IPC_CHUNK_BYTES):
                await self.channel.model_data({
                    "kind": "model_chunk", "id": identity, "sequence": sequence,
                    "base64": base64.b64encode(
                        data[offset:offset + IPC_CHUNK_BYTES]).decode()})
                sequence += 1

        self._upstream_active = True
        await self._broker.forward(body, identity=broker_identity,
                                   guard=self._admission.guard,
                                   on_response=on_response, on_chunk=on_chunk)
        # A completed provider body settles this request; delivery comes next.
        self._upstream_active = False
        self._forwards += 1
        await self.channel.model_data({"kind": "model_end", "id": identity,
                                       "sequence": sequence})
        await self.channel.model_closed(identity)

    async def _evidence_gate(self):
        frame = self.channel.controls.get("terminal_collection_finished")
        if frame is None or frame["collector_returncode"] != 0:
            raise IntegrityError("Evidence reads wait for successful collection")

    async def _evidence(self, name):
        await self._evidence_gate()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
            raise ValueError("Invalid evidence name")
        code, out, _ = await self._exec(self._container, "/bin/cat",
                                        "/evidence/" + name)
        if code:
            raise IntegrityError("Native evidence read failed: " + name)
        return out

    async def finish(self):
        """Fence an idle run, then collect, finalize history and capture source.

        Returns host-verified bytes and evidence; the admission owns acceptance.
        """
        await self.started()
        channel = self.channel
        if (channel.failure is not None or self._finish_started
                or "terminal_collection_finished" in channel.controls
                or channel.http_lock.locked() or channel.history_lock.locked()
                or channel.model_assembling or self._exchange_active
                or self._upstream_active or not channel.model.empty()
                or self.session._busy):
            raise IntegrityError("Native work is failed or not idle at the fence")
        self._finish_started = True
        try:
            # Full-session projection/event agreement while native is alive and idle.
            projected = await channel.until(self.session.verify_projection())
            await channel.send(decode(self.spec.control("fence")))
            # Only the fence ends collection without failure, and PID 1 always
            # emits fenced for it; any other terminal cause is reported failed.
            collected = await channel.control("terminal_collection_finished",
                                              terminal=False)
            if collected["failed"] or collected["collector_returncode"] != 0:
                raise IntegrityError("Native terminal collection failed")
            await channel.control("fenced", terminal=False)
            stop_raw = await self._evidence("disconnect-stop.json")
            await _durable(verify_stop, stop_raw, self.capture_spec, self._native)
            await self._record("stopped", {"native": self._native},
                               (File("stop.json", stop_raw),))
            history = self.session.history
            await history.finalize()
            if history.final_metadata["durable_sequence"] != projected:
                raise IntegrityError("Native history advanced after projection check")
            request = await _durable(capture_request, self.capture_spec, stop_raw,
                                     self._native, history)
            code, out, err = await self._exec(
                "-i", "--user", "0:0", self._container, *PYTHON,
                "/authority/capture_worker.py", data=request)
            await self._record("capture", {"returncode": code}, (
                File("capture.json", out), File("capture.stderr", err)))
            if code:
                raise IntegrityError("Native source capture failed")
            snapshot = await _durable(verify_capture, out, self.capture_spec,
                                      stop_raw, self._native, history)
            if channel.failure is not None:
                raise IntegrityError("Host channel failed during capture") from (
                    channel.failure)
            self._captured = True
            evidence = Snapshot((
                File("native/stop.json", stop_raw), File("native/capture.json", out),
                File("native/history-final.json", encode(history.final_metadata)),
                File("native/runtime.json", encode(self._state())),
                File("native/control.jsonl", b"".join(channel.trace)),
            ))
            return NativeCandidate(self.run, snapshot, evidence,
                                   upstream_settled=self._upstream_settled())
        except BaseException as error:
            channel.fail(error)
            raise

    def _upstream_settled(self):
        """Only pinned-slot settlement is upstream stop; HTTP completion never is.

        A run that never reached a broker dispatched no model request.
        """
        if self._broker is None:
            return not self._upstream_active
        if self._broker.settlement is None:
            return False
        return self._broker.upstream_settled

    def _state(self):
        return {
            "run_id": self.run.run_id, "runtime_sha256": self.spec.sha256,
            "container_id": self._container, "native": self._native,
            "released": self._released, "captured": self._captured,
            "collection": self.channel.controls.get("terminal_collection_finished"),
            "forwards_completed": self._forwards,
            "upstream_request_incomplete": self._upstream_active,
            "upstream_settlement_scope": (
                "pinned_slot_idle" if self._broker is not None
                and self._broker.settlement is not None
                else "completed_provider_http_bodies"),
            "upstream_settled": self._upstream_settled(),
            "failure": (type(self.channel.failure).__name__
                        if self.channel.failure is not None else None),
            "namespace_stopped": self._namespace_stopped,
            "retained": self._retained,
            "journals": {name: asdict(journal.head) if journal.head else None
                         for name, journal in self._journals.items()},
        }

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await _settle(self._close_task)

    async def _close(self):
        self._closing = True
        errors = []
        # Wake launch, prompt and lane waiters: no further native work starts.
        self.channel.closing.set()
        if self._main is not None and not self._main.done():
            with contextlib.suppress(BaseException):
                await self.started()
        while self.session is not None and self.session._busy:
            # A woken native operation first settles its owned journal writes.
            await asyncio.sleep(0.01)
        for task in (self._pump,):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for closer in ((self.session.close if self.session else None),
                       (self._broker.close if self._broker else None)):
            try:
                if closer is not None:
                    await closer()
            except BaseException as error:
                errors.append(error)
        try:
            await self._settle_namespace()
        except BaseException as error:
            errors.append(error)
        try:
            await self._close_attachment()
        except BaseException as error:
            errors.append(error)
        try:
            if self._journals:
                await self._record("closed", {"state": self._state()})
            self._summary = self._state()
        except BaseException as error:
            errors.append(error)
        for journal in self._journals.values():
            try:
                journal.close()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise IntegrityError("Native runtime cleanup incomplete") from errors[0]

    async def _settle_namespace(self):
        if self._container is None:
            if self._create_attempted:
                await self._reconcile_create()
            else:
                self._namespace_stopped = True
            return
        collected = None
        if self._attachment is not None:
            if not self._released:
                # PID 1 rejects anything but release, so EOF ends it before work.
                with contextlib.suppress(OSError):
                    self._attachment.stdin.close()
            elif ("terminal_collection_finished" not in self.channel.controls
                  and not self.channel.reader_done.is_set()
                  and not self._finish_started):
                self._finish_started = True
                with contextlib.suppress(BaseException):
                    await self.channel.send(decode(self.spec.control("fence")),
                                            urgent=True)
            if self._released:
                collected = await self.channel.terminal()
        if self._released and (collected is None
                               or collected["collector_returncode"] != 0):
            self._retained = "terminal collection unconfirmed; namespace retained"
            await self._record("retained", {"reason": self._retained})
            return
        if self._released:
            await self._archive_evidence()
        await self._remove()

    async def _archive_evidence(self):
        code, out, err = await self._exec(
            self._container, *PYTHON, "-c",
            "import sys,tarfile\n"
            "with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as t:\n"
            " t.add('/evidence',arcname='evidence')")
        if code:
            raise IntegrityError("Native evidence archive failed")
        for index, offset in enumerate(range(0, max(len(out), 1), STATE_CHUNK_BYTES)):
            await self._record("evidence_archive", {
                "index": index, "offset": offset, "total_bytes": len(out),
                "sha256": sha256(out)},
                (File("evidence.tar.part", out[offset:offset + STATE_CHUNK_BYTES]),))
        if not self._captured:
            await self._preserve_state()

    async def _preserve_state(self):
        """Retain raw database/WAL bytes after collector-confirmed writer stop."""
        for filename in STATE_FILES:
            path = "/state/data/opencode/" + filename
            # Workers are collector-stopped; still refuse linked parents or files,
            # so another in-container file is never labelled native state.
            code, raw, _ = await self._exec(
                self._container, *PYTHON, "-c",
                "import json,os,stat\np=" + repr(path) + "\ncur=''\nresult=None\n"
                "for part in p.split('/')[1:-1]:\n"
                " cur+='/'+part\n"
                " if not os.path.lexists(cur): break\n"
                " s=os.lstat(cur)\n"
                " assert stat.S_ISDIR(s.st_mode) and not stat.S_ISLNK(s.st_mode)\n"
                "else:\n"
                " if os.path.lexists(p):\n"
                "  s=os.lstat(p)\n"
                "  assert stat.S_ISREG(s.st_mode) and s.st_nlink==1\n"
                "  result=[s.st_size,s.st_ino]\n"
                "print(json.dumps({'file':result}))")
            found = json.loads(raw)["file"] if code == 0 else "unreadable"
            if found is not None and (
                    type(found) is not list or len(found) != 2
                    or any(type(v) is not int or v < 0 for v in found)):
                raise IntegrityError("Native state file cannot be preserved")
            size, inode = found if found is not None else (None, None)
            digest = hashlib.sha256()
            for offset in range(0, size or 0, STATE_CHUNK_BYTES):
                length = min(STATE_CHUNK_BYTES, size - offset)
                code, data, _ = await self._exec(
                    self._container, *PYTHON, "-c",
                    "import os,sys\n"
                    f"fd=os.open({path!r},os.O_RDONLY|os.O_NOFOLLOW)\n"
                    f"s=os.fstat(fd); assert s.st_ino=={inode} and s.st_size=={size}\n"
                    f"os.lseek(fd,{offset},0); data=b''\n"
                    f"while len(data)<{length}:\n"
                    f" chunk=os.read(fd,{length}-len(data))\n"
                    " assert chunk\n data+=chunk\n"
                    "sys.stdout.buffer.write(data)")
                if code or len(data) != length:
                    raise IntegrityError("Native state chunk read failed")
                digest.update(data)
                await self._record("state_chunk", {
                    "file": filename, "offset": offset, "bytes": length,
                    "sha256": sha256(data)}, (File("chunk.bin", data),))
            await self._record("state_file", {
                "file": filename, "bytes": size, "sha256": digest.hexdigest(),
                "semantic_completeness": "raw_failure_preservation"})

    def _owned(self, value, identity):
        labels = (value.get("Config") or {}).get("Labels") or {}
        if (value.get("Id") != identity or value.get("Name") != "/" + self.spec.name
                or value.get("Image") != self.spec.image_id
                or labels.get("recollect.selfmod") != self.run.run_id
                or labels.get("recollect.spec") != self.spec.sha256):
            raise IntegrityError("Refusing to mutate an unowned container")

    async def _ids(self):
        ids = set()
        for selector in ("label=recollect.selfmod=" + self.run.run_id,
                         "name=^/" + self.spec.name + "$"):
            code, raw, _ = await self._docker.run(
                "ps", "-a", "--no-trunc", "--filter", selector, "--format", "{{.ID}}")
            if code:
                raise IntegrityError("Native namespace lookup failed")
            for line in raw.decode("ascii").splitlines():
                if not re.fullmatch(r"[0-9a-f]{64}", line):
                    raise IntegrityError("Invalid native namespace identity")
                ids.add(line)
        return ids

    async def _reconcile_create(self):
        # The awaited create CLI has settled; a lookup now reflects its result.
        ids = await self._ids()
        if len(ids) > 1:
            raise IntegrityError("Ambiguous native namespace reconciliation")
        if ids:
            self._container = ids.pop()
            _, value = await self._inspect()
            self._owned(value, self._container)
            await self._remove()
        else:
            self._namespace_stopped = True
            await _durable(self._remove_inputs)

    async def _remove(self):
        raw, value = await self._inspect()
        self._owned(value, self._container)
        if value.get("State", {}).get("Running"):
            code, _, _ = await self._docker.run("kill", "--signal=KILL",
                                                self._container)
            if code:
                raise IntegrityError("Native namespace kill failed")
        raw, value = await self._inspect()
        self._owned(value, self._container)
        state = value.get("State", {})
        if (state.get("Running") is not False or state.get("Pid") != 0
                or state.get("Status") not in {"created", "exited"}):
            raise IntegrityError("Native namespace has not stopped")
        code, _, _ = await self._docker.run("rm", self._container)
        if code or await self._ids():
            raise IntegrityError("Native namespace removal unconfirmed")
        await self._record("removed", {"container_id": self._container},
                           (File("final-inspection.json", raw),))
        self._namespace_stopped = True
        await _durable(self._remove_inputs)

    def _remove_inputs(self):
        if self._input_dir.exists():
            if (root_path(self._input_dir) != self._input_dir.absolute()
                    or not self._input_dir.name.startswith("selfmod-native-")):
                raise IntegrityError("Refusing to remove unexpected native inputs")
            shutil.rmtree(self._input_dir)

    async def _close_attachment(self):
        process = self._attachment
        if process is None:
            return
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        with contextlib.suppress(OSError):
            process.stdin.close()
        tasks = [t for t in (self._reader, self._stderr) if t is not None]
        results = await asyncio.gather(*tasks, process.wait(), return_exceptions=True)
        # Closing the pipe completes any write abandoned after a terminal wake.
        await self.channel.settle_writes()
        for result in results:
            if isinstance(result, BaseException):
                raise result

    def verify_stop(self):
        """Report owned namespace removal and completed upstream bodies only."""
        state = self._summary or self._state()
        # An unclosed journal cannot be copied exactly; evidence stays incomplete,
        # so stop is unconfirmed and ownership is retained.
        unclosed = [name for name, journal in self._journals.items()
                    if journal._owner is not None]
        return NativeStop(
            self.run, sha256(self._admission.settings.authority),
            self._namespace_stopped and not unclosed, self._upstream_settled(),
            Snapshot((File("native/stop-summary.json", encode(state)),)),
            # Every host journal, even an empty one, is bound as an exact sidecar.
            tuple((name, journal.root, journal.head)
                  for name, journal in self._journals.items()
                  if journal._owner is None),
        )
