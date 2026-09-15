"""Host-owned HTTPX transport into an already attested network-none container.

The owner freezes/mounts helper_inputs() under /authority before launch. This
module does not attest that mount, launch a container, grant execution authority,
or produce receipts. Reaping Docker's CLI does not prove that the in-container
exec child, native worker or upstream inference stopped; the supervisor owns
that reconciliation. There are no work deadlines or call/token quotas here.
"""

import asyncio
import base64
import contextlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import native_http_relay as wire
from .contracts import File, Snapshot
from .journal import MAX_FILE_BYTES, encode, regular, root_path
from .native import _settle

RELAY_PATH = "/authority/native_http_relay.py"
STDERR_BYTES = 65536


def helper_inputs() -> Snapshot:
    """Freeze trusted helper bytes; snapshot paths are relative to /authority.

    Call during host preparation, off the event loop. Mount this snapshot read
    only and bind its digest through the owning supervisor's admission inputs.
    """
    path = Path(__file__).with_name("native_http_relay.py")
    regular(path)
    return Snapshot((File("native_http_relay.py", path.read_bytes()),))


@dataclass(frozen=True)
class NativeTransportConfig:
    """Trusted CLI selection, private config and environment frozen by the owner.

    container_name also accepts a full 64-hex container ID; use the attested ID
    in production to avoid name reuse. from_argv accepts the runtime's complete
    prefix: (absolute_cli, '--config', private_directory, '--host', endpoint).
    Construction checks regular paths; the owner guards their contents and
    continued identity before dispatch. No global Docker context is consulted.
    """

    docker_cli: str
    container_name: str
    endpoint: str
    docker_config: str
    environment: tuple[tuple[str, str], ...]
    relay_path: str = RELAY_PATH

    @classmethod
    def from_argv(cls, argv_prefix, container_id, *, env):
        argv = tuple(argv_prefix)
        if (len(argv) != 5 or any(type(v) is not str for v in argv)
                or set(argv[1::2]) != {"--config", "--host"}):
            raise ValueError("Supply the trusted Docker argv prefix")
        options = dict(zip(argv[1::2], argv[2::2], strict=True))
        return cls(argv[0], container_id, options["--host"], options["--config"],
                   tuple(env.items()))

    @property
    def docker_args(self):
        return ("--config", self.docker_config, "--host", self.endpoint)

    def __post_init__(self):
        if (type(self.docker_cli) is not str
                or not Path(self.docker_cli).is_absolute()
                or Path(self.docker_cli).name.lower() not in {"docker", "docker.exe"}
                or any(ord(c) < 32 for c in self.docker_cli)):
            raise ValueError("Supply the trusted absolute Docker CLI path")
        if (type(self.container_name) is not str
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*",
                                    self.container_name)):
            raise ValueError("Supply an exact Docker container name")
        if self.relay_path != RELAY_PATH:
            raise ValueError("Native relay must use its fixed authority path")
        if (type(self.docker_config) is not str
                or not Path(self.docker_config).is_absolute()
                or any(ord(c) < 32 for c in self.docker_config)):
            raise ValueError("Supply the absolute private Docker config directory")
        if type(self.endpoint) is not str or not (
            re.fullmatch(r"npipe:////\./pipe/[A-Za-z0-9_-]+", self.endpoint)
            or re.fullmatch(r"unix:///[A-Za-z0-9_./-]+", self.endpoint)
        ):
            raise ValueError("Only explicit local Docker endpoints are permitted")
        if type(self.environment) is not tuple:
            raise ValueError("Freeze the Docker subprocess environment")
        allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT"}
        names = set()
        for pair in self.environment:
            if (type(pair) is not tuple or len(pair) != 2
                    or any(type(v) is not str for v in pair)):
                raise ValueError("Invalid Docker environment entry")
            key, value = pair
            if key.upper() not in allowed or key.upper() in names or "\0" in value:
                raise ValueError("Docker environment must exclude ambient overrides")
            names.add(key.upper())
        root_path(Path(self.docker_cli).parent)
        regular(Path(self.docker_cli))
        private_config = root_path(Path(self.docker_config))
        regular(private_config / "config.json")


class NativeHTTPTransport(httpx.AsyncBaseTransport):
    """Use with NativeSession, whose logical origin is http://native.invalid."""

    def __init__(self, config: NativeTransportConfig):
        if type(config) is not NativeTransportConfig:
            raise ValueError("Freeze a NativeTransportConfig")
        self.config = config
        self._streams = set()
        self._requests = set()
        self._closed = False
        self._closing = None

    async def handle_async_request(self, request):
        if self._closed:
            raise httpx.TransportError("Native transport is closed", request=request)
        url = request.url
        if (url.scheme != "http" or url.raw_host != b"native.invalid"
                or url.port not in (None, 80) or url.userinfo or url.fragment):
            raise httpx.UnsupportedProtocol("Forbidden native origin", request=request)
        try:
            path = url.raw_path.decode("ascii")
            wire.validate_target(request.method, path)
        except (ValueError, UnicodeError) as error:
            raise httpx.UnsupportedProtocol(str(error), request=request) from error
        owner = asyncio.current_task()
        self._requests.add(owner)
        spawning = stream = None
        try:
            body = bytearray()
            async for chunk in request.stream:
                if len(body) + len(chunk) > MAX_FILE_BYTES:
                    raise httpx.WriteError("Native request exceeds bound")
                body.extend(chunk)
            if request.method == "GET" and body:
                raise httpx.WriteError("Native GET body is forbidden")
            payload = encode({"method": request.method, "path": path,
                              "body": base64.b64encode(body).decode()})
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                self.config.docker_cli, *self.config.docker_args,
                "exec", "-i", self.config.container_name,
                "/usr/local/bin/python", "-I", "-S", "-u", "-B",
                self.config.relay_path,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=wire.FRAME_BYTES,
                env=dict(self.config.environment),
                **({"creationflags": subprocess.CREATE_NO_WINDOW}
                   if sys.platform == "win32" else {}),
            ))
            process = await asyncio.shield(spawning)
            stream = NativeHTTPStream(process)
            self._streams.add(stream)
            stream.on_close = self._streams.discard
            for offset in range(0, len(payload), wire.CHUNK_BYTES):
                process.stdin.write(payload[offset:offset + wire.CHUNK_BYTES])
                await process.stdin.drain()
            process.stdin.close()
            header = await stream.frame()
            stream.set_header(header)
            return httpx.Response(header["status"], headers=header["headers"],
                                  stream=stream)
        except BaseException:
            async def finish():
                nonlocal stream
                if spawning is not None:
                    # A cancelled spawn may already have created a process.
                    # Never relinquish it before the spawn result is known.
                    if stream is None:
                        try:
                            process = await spawning
                        except Exception:
                            return
                        stream = NativeHTTPStream(process)
                    await stream.aclose()

            await _settle(asyncio.create_task(finish()))
            raise
        finally:
            self._requests.discard(owner)

    async def aclose(self):
        self._closed = True

        async def finish():
            pending = tuple(self._requests)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*(s.aclose() for s in tuple(self._streams)))

        if self._closing is None:
            self._closing = asyncio.create_task(finish())
        await _settle(self._closing)


class NativeHTTPStream(httpx.AsyncByteStream):
    """Pull framed stdout; continuously drain stderr while retaining its prefix."""

    def __init__(self, process):
        self.process = process
        self.stderr_prefix = bytearray()
        self.stderr = asyncio.create_task(self._stderr())
        self._closing = None
        self._read_lock = asyncio.Lock()
        self._reader = None
        self._length = None
        self.on_close = None

    async def _stderr(self):
        while chunk := await self.process.stderr.read(wire.CHUNK_BYTES):
            self.stderr_prefix.extend(chunk[:max(0, STDERR_BYTES
                                                - len(self.stderr_prefix))])

    async def _read(self, *, eof=False):
        async with self._read_lock:
            self._reader = asyncio.current_task()
            try:
                if eof:
                    return await self.process.stdout.read(1)
                return await self.process.stdout.readline()
            finally:
                self._reader = None

    async def frame(self):
        try:
            line = await self._read()
            if len(line) > wire.FRAME_BYTES or not line.endswith(b"\n"):
                raise ValueError("Unterminated or oversized frame")
            value = json.loads(line, object_pairs_hook=wire.pairs)
            if type(value) is not dict:
                raise ValueError("Not an object")
            return value
        except (ValueError, OSError) as error:
            raise httpx.ReadError("Incomplete or invalid native relay frame") from error

    def set_header(self, header):
        if (set(header) != {"status", "headers"}
                or type(header["status"]) is not int
                or not 200 <= header["status"] <= 599
                or type(header["headers"]) is not list):
            raise httpx.ReadError("Invalid native relay headers")
        lengths, encodings, transfers = [], [], []
        for pair in header["headers"]:
            if (type(pair) is not list or len(pair) != 2
                    or any(type(v) is not str for v in pair)):
                raise httpx.ReadError("Invalid native relay header pair")
            key, value = pair
            if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                    or any(ord(c) < 32 or ord(c) > 126 for c in value)):
                raise httpx.ReadError("Invalid native relay header bytes")
            if key.lower() == "content-length":
                lengths.append(value)
            if key.lower() == "content-encoding":
                encodings.append(value)
            if key.lower() == "transfer-encoding":
                transfers.append(value)
        if (len(lengths) > 1 or (lengths and (
                not re.fullmatch(r"[0-9]+", lengths[0]) or len(lengths[0]) > 20))
                or (encodings and encodings != ["identity"])
                or (transfers and (transfers != ["chunked"] or lengths))):
            raise httpx.ReadError("Invalid native response framing headers")
        self._length = int(lengths[0]) if lengths else None

    async def __aiter__(self):
        total = 0
        try:
            while True:
                frame = await self.frame()
                if set(frame) == {"end"} and frame["end"] is True:
                    if self._length is not None and total != self._length:
                        raise httpx.ReadError(
                            "Native response ended before Content-Length",
                        )
                    if await self._read(eof=True):
                        raise httpx.ReadError("Trailing native relay output")
                    code = await self.process.wait()
                    await asyncio.shield(self.stderr)
                    if code != 0 or self.stderr_prefix:
                        raise httpx.ReadError("Native relay did not finish cleanly")
                    return
                if set(frame) != {"chunk"} or type(frame["chunk"]) is not str:
                    raise httpx.ReadError("Invalid native relay chunk")
                try:
                    chunk = base64.b64decode(frame["chunk"], validate=True)
                    if not 0 < len(chunk) <= wire.CHUNK_BYTES:
                        raise ValueError("Invalid frame size")
                except ValueError as error:
                    raise httpx.ReadError("Invalid native relay bytes") from error
                room = MAX_FILE_BYTES - total
                if room:
                    yield chunk[:room]
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise httpx.ReadError("Native response exceeds bound")
                if self._length is not None and total > self._length:
                    raise httpx.ReadError("Native response exceeds Content-Length")
        finally:
            await self.aclose()

    async def aclose(self):
        async def finish():
            # No claim of native/GPU quiescence follows from this CLI cleanup.
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
            self.process.stdin.close()
            if self._reader is not None:
                self._reader.cancel()

            async def drain():
                async with self._read_lock:
                    while await self.process.stdout.read(wire.CHUNK_BYTES):
                        pass

            try:
                results = await asyncio.gather(
                    drain(), self.stderr, self.process.wait(), return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
            finally:
                if self.on_close is not None:
                    self.on_close(self)

        if self._closing is None:
            self._closing = asyncio.create_task(finish())
        await _settle(self._closing)
