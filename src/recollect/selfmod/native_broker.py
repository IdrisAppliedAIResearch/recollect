"""Host-owned native chat forwarding, not production containment attestation.

The caller owns the journal outside worker mounts and supplies request identity
and a controller guard. Use one broker on one event loop. Its local
HTTP cleanup cannot certify that upstream inference has stopped. The journal
must not have concurrent writers; its current readback implementation also has
history-dependent cost, independent of this broker's bounded chunk buffering.
"""

import asyncio
import inspect
import ipaddress
import json
import math
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .contracts import File, Snapshot
from .journal import EMPTY_SNAPSHOT, MAX_FILE_BYTES, IntegrityError, encode

TOKEN_CAP_FIELDS = frozenset({
    "max_tokens", "max_completion_tokens", "max_output_tokens",
})
# Native's openai-compatible chat body only. SDK transport options, llama.cpp
# routing/control extensions and Responses API fields are deliberately absent.
CHAT_FIELDS = TOKEN_CAP_FIELDS | frozenset({
    "model", "messages", "stream", "stream_options", "temperature", "top_p",
    "frequency_penalty", "presence_penalty", "stop", "seed", "tools",
    "tool_choice", "parallel_tool_calls", "response_format", "user", "metadata",
    "reasoning_effort", "verbosity", "logprobs", "top_logprobs", "logit_bias",
    "n", "functions", "function_call",
})


@dataclass(frozen=True)
class BrokerSettings:
    base_url: str
    model: str
    max_request_bytes: int = MAX_FILE_BYTES
    max_frame_bytes: int = 1024 * 1024
    max_header_bytes: int = 64 * 1024
    max_json_depth: int = 64
    # Host-pinned modifier lane slot; None keeps unpinned single-lane forwarding.
    slot: int | None = None

    def __post_init__(self):
        if self.slot is not None and (type(self.slot) is not int
                                      or not 0 <= self.slot < 64):
            raise ValueError("Model slot must be an explicit small integer")
        try:
            url = urlsplit(self.base_url)
            address = ipaddress.ip_address(url.hostname or "")
            valid = (
                url.scheme == "http" and address.is_loopback
                and url.path == "/v1" and not url.query and not url.fragment
                and url.username is None and url.password is None
                and url.port is not None and 0 < url.port < 65536
                and httpx.URL(self.base_url).raw_path == b"/v1"
                and "?" not in self.base_url and "#" not in self.base_url
                and not any(c.isspace() for c in self.base_url)
                and "%" not in (url.hostname or "")
            )
        except (TypeError, ValueError, httpx.InvalidURL) as exc:
            raise ValueError("Freeze a literal loopback HTTP /v1 endpoint") from exc
        if not valid:
            raise ValueError("Freeze a literal loopback HTTP /v1 endpoint")
        if not isinstance(self.model, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+", self.model,
        ):
            raise ValueError("Freeze a simple local model identity")
        for bound in (
            self.max_request_bytes, self.max_frame_bytes, self.max_header_bytes,
        ):
            if type(bound) is not int or not 0 < bound <= MAX_FILE_BYTES:
                raise ValueError("Resource byte bounds must fit one archive file")
        if type(self.max_json_depth) is not int or not 1 <= self.max_json_depth <= 64:
            raise ValueError("JSON depth must be between 1 and 64")


@dataclass(frozen=True)
class BrokerIdentity:
    """Host evidence identity, never parsed from the model payload.

    A history watermark is optional but indivisible. The caller may supply it
    only after NativeHistory.capture plus verification; this module records the
    binding and does not independently certify history completeness or trust.
    """

    run_id: str
    session_id: str
    history_sequence: int | None = None
    history_event_sha256: str | None = None

    def __post_init__(self):
        if any(not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,256}", value,
        ) for value in (self.run_id, self.session_id)):
            raise ValueError("Host run/session identities must be nonempty and bounded")
        if (self.history_sequence is None) != (self.history_event_sha256 is None):
            raise ValueError("Supply history sequence and event SHA-256 together")
        if self.history_sequence is not None and (
            type(self.history_sequence) is not int or self.history_sequence < 0
            or not isinstance(self.history_event_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.history_event_sha256)
        ):
            raise ValueError("Invalid verified history watermark")


@dataclass(frozen=True)
class BrokerResponse:
    request_id: str
    status_code: int
    # Raw provider headers are evidence, not permission to forward hop-by-hop
    # headers or redirect the downstream client. The bridge chooses safe headers.
    headers: tuple[tuple[bytes, bytes], ...]


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise IntegrityError("Duplicate native request key")
        result[key] = value
    return result


def _constant(value):
    raise IntegrityError("Nonfinite native request number")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        _constant(value)
    return result


def _shape(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required) <= value.keys()
            or value.keys() - set(required) - set(optional)):
        raise IntegrityError("Unknown or missing native text/tool fields")


def _name(value):
    if not isinstance(value, str) or not value:
        raise IntegrityError("Native text/tool identity must be a nonempty string")


def _function_call(value):
    _shape(value, {"name", "arguments"})
    _name(value["name"])
    if not isinstance(value["arguments"], str):
        raise IntegrityError("Native tool arguments must be a string")


def _message(message):
    role = message.get("role")
    if not isinstance(role, str) or role not in {
        "system", "developer", "user", "assistant", "tool", "function",
    }:
        raise IntegrityError("Unsupported native text/tool message role")
    optional = {"content", "name"}
    if role == "assistant":
        optional |= {"tool_calls", "function_call", "reasoning_content"}
    elif role == "tool":
        optional.add("tool_call_id")
    _shape(message, {"role"}, optional)
    # Native's Qwen adapter replays assistant reasoning as plain text.
    if "reasoning_content" in message and not isinstance(
        message["reasoning_content"], str,
    ):
        raise IntegrityError("Native assistant reasoning_content must be a string")
    if "name" in message:
        _name(message["name"])
    if role == "tool":
        _name(message.get("tool_call_id"))
    elif role == "function":
        _name(message.get("name"))
    if "tool_calls" in message:
        calls = message["tool_calls"]
        if not isinstance(calls, list) or not calls or "function_call" in message:
            raise IntegrityError("Invalid native assistant tool calls")
        for call in calls:
            _shape(call, {"id", "type", "function"})
            _name(call["id"])
            if call["type"] != "function":
                raise IntegrityError("Only native function tool calls are supported")
            _function_call(call["function"])
    if "function_call" in message:
        _function_call(message["function_call"])
    content = message.get("content")
    if isinstance(content, str):
        return
    if content is None and role == "assistant" and (
        "tool_calls" in message or "function_call" in message
    ):
        return
    if not isinstance(content, list):
        raise IntegrityError("Native message content must be text")
    # A loopback model server can fetch media URLs on behalf of its caller.
    # Exact text-part shapes close that deputy without filtering text or code.
    for part in content:
        _shape(part, {"type", "text"})
        if part["type"] != "text" or not isinstance(part["text"], str):
            raise IntegrityError("Only native text content parts are supported")


def _function_definition(value):
    _shape(value, {"name"}, {"description", "parameters", "strict"})
    _name(value["name"])
    if ("description" in value and not isinstance(value["description"], str)
            or "parameters" in value and not isinstance(value["parameters"], dict)
            or "strict" in value and type(value["strict"]) is not bool):
        raise IntegrityError("Invalid native function definition")


def _tools(value):
    for key in ("tools", "functions"):
        if key not in value:
            continue
        if not isinstance(value[key], list):
            raise IntegrityError("Native tool definitions must be a list")
        for tool in value[key]:
            if key == "tools":
                _shape(tool, {"type", "function"})
                if tool["type"] != "function":
                    raise IntegrityError("Only native function tools are supported")
                tool = tool["function"]
            _function_definition(tool)
    for key in ("tool_choice", "function_call"):
        if key not in value:
            continue
        choice = value[key]
        if isinstance(choice, str) and choice in {"auto", "none", "required"}:
            continue
        if key == "tool_choice":
            _shape(choice, {"type", "function"})
            if choice["type"] != "function":
                raise IntegrityError("Only native function tool choice is supported")
            choice = choice["function"]
        _shape(choice, {"name"})
        _name(choice["name"])


def _payload(raw: bytes, settings: BrokerSettings) -> bytes:
    try:
        text = raw.decode("utf-8")
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > settings.max_json_depth:
                    raise IntegrityError("Native request JSON depth exceeds bound")
            elif char in "]}":
                depth -= 1
        value = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_constant,
            parse_float=_float,
        )
        if not isinstance(value, dict) or value.keys() - CHAT_FIELDS:
            raise IntegrityError("Unknown native chat routing/control fields")
        if value.get("model") != settings.model:
            raise IntegrityError("Native request model mismatch")
        messages = value.get("messages")
        if not isinstance(messages, list) or not messages or any(
            not isinstance(message, dict) for message in messages
        ):
            raise IntegrityError("Native chat requires a nonempty messages list")
        for message in messages:
            _message(message)
        _tools(value)
        if "stream" in value and type(value["stream"]) is not bool:
            raise IntegrityError("Native stream must be boolean")
        forwarded = encode({
            k: v for k, v in value.items() if k not in TOKEN_CAP_FIELDS
        })
        if len(forwarded) > settings.max_request_bytes:
            raise IntegrityError("Forwarded request exceeds resource byte bound")
        return forwarded
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, IntegrityError):
            raise
        raise IntegrityError("Invalid native request JSON") from exc


async def _settle(task):
    """Finish owned cleanup/I/O despite repeated cancellation, then propagate it."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class NativeModelBroker:
    """One active forward, no call/token/time quotas, redirects or retries.

    ``forward`` awaits ``on_response(head)`` once, then ``on_chunk(bytes)`` for
    each durably archived provider chunk. Status is immutable even if streaming
    subsequently fails. Callbacks must not buffer the whole response; they own
    downstream delivery/backpressure. Their return only acknowledges local
    delivery, never remote receipt. Cancel forward on downstream disconnect.

    Guard is a trusted ``guard() -> None | Awaitable[None]`` which raises on
    revoked authority (False is also rejected). Synchronous invocation runs in
    a settled worker thread; async functions/callables run on this event loop.
    It runs immediately before dispatch,
    each callback and successful return. A paused callback must itself respect
    revocation while awaiting downstream I/O. Errors poison broker reuse; create
    a replacement only under the controller's independently established policy.
    ``close`` cancels and settles active work, then closes the owned client. The
    caller keeps ownership of the journal and closes it after broker settlement.
    """

    def __init__(self, settings: BrokerSettings, journal, *, transport=None,
                 settlement=None):
        if not isinstance(settings, BrokerSettings):
            raise ValueError("Frozen broker settings required")
        if transport is not None and not isinstance(
            transport, httpx.AsyncBaseTransport,
        ):
            raise ValueError("Expected a trusted host async transport")
        if (settlement is None) != (settings.slot is None) or (
                settlement is not None and settlement.slot != settings.slot):
            raise ValueError("Pinned slot settlement must match the frozen slot")
        self._settlement, self._unsettled = settlement, False
        self._settings, self._journal = settings, journal
        # An injected transport is host/test authority, never supplied by native.
        self._client = httpx.AsyncClient(
            transport=(transport if transport is not None else
                       httpx.AsyncHTTPTransport(retries=0, trust_env=False)),
            trust_env=False, follow_redirects=False, timeout=None,
        )
        self._active = None
        self._closed = self._failed = False
        self._close_task = None

    @property
    def settings(self):
        return self._settings

    @property
    def busy(self):
        return self._active is not None

    @property
    def settlement(self):
        return self._settlement

    @property
    def upstream_settled(self):
        """True only with pinned-slot settlement and no unconfirmed dispatch."""
        return (self._settlement is not None and self._active is None
                and not self._unsettled)

    async def _record(self, kind, data, files=EMPTY_SNAPSHOT):
        await _settle(asyncio.create_task(asyncio.to_thread(
            self._journal.append, "native_broker_" + kind, data, files,
        )))

    async def forward(
        self, raw: bytes, *, identity: BrokerIdentity,
        guard: Callable[[], None | Awaitable[None]],
        on_response: Callable[[BrokerResponse], Awaitable[None]],
        on_chunk: Callable[[bytes], Awaitable[None]], path: str = "/chat/completions",
    ) -> BrokerResponse:
        if self._active is not None or self._closed or self._failed:
            raise IntegrityError("Native broker is busy, closed or failed")
        if not isinstance(identity, BrokerIdentity) or not all(
            callable(f) for f in (guard, on_response, on_chunk)
        ):
            raise ValueError("Host identity, guard and stream callbacks required")
        self._active = asyncio.current_task()
        self._active_settled = asyncio.Event()
        try:
            return await self._forward(
                raw, identity, guard, on_response, on_chunk, path,
            )
        finally:
            self._active = None
            self._active_settled.set()

    async def _forward(self, raw, identity, guard, on_response, on_chunk, path):
        context = {
            "request_id": uuid.uuid4().hex, "run_id": identity.run_id,
            "session_id": identity.session_id, "base_url": self.settings.base_url,
            "model": self.settings.model,
            "history_sequence": identity.history_sequence,
            "history_event_sha256": identity.history_event_sha256,
        }
        response, head, failure = None, None, None
        complete = dispatched = False
        chunks = delivered = 0

        async def check():
            if self._closed:
                raise IntegrityError("Native broker closed during request")
            if inspect.iscoroutinefunction(guard) or inspect.iscoroutinefunction(
                guard.__call__,
            ):
                result = await guard()
            else:
                def invoke():
                    result = guard()
                    # Do not orphan a coroutine if cancellation arrives while
                    # a synchronous guard is still running in its worker.
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise IntegrityError(
                            "Awaitable guard must be an async callable",
                        )
                    return result

                result = await _settle(asyncio.create_task(asyncio.to_thread(invoke)))
            if self._closed:
                raise IntegrityError("Native broker closed during request")
            if result is not None and result is not True:
                raise IntegrityError("Controller guard rejected native request")

        try:
            if type(raw) is not bytes or not 0 < len(raw) <= (
                self.settings.max_request_bytes
            ):
                raise IntegrityError("Native request exceeds resource byte bound")
            await self._record("original", context, Snapshot((
                File("original.json", raw),
            )))
            if path != "/chat/completions":
                raise IntegrityError("Only chat/completions is supported")
            forwarded = _payload(raw, self.settings)
            if self.settings.slot is not None:
                # Trusted host routing added after validating the native body.
                forwarded = encode({**json.loads(forwarded),
                                    "id_slot": self.settings.slot})
                if len(forwarded) > self.settings.max_request_bytes:
                    raise IntegrityError("Forwarded request exceeds byte bound")
            await self._record("request", {**context, "path": path}, Snapshot((
                File("forwarded.json", forwarded),
            )))
            request = self._client.build_request(
                "POST", self.settings.base_url + path, content=forwarded,
                headers={"Content-Type": "application/json",
                         "Accept-Encoding": "identity"},
            )
            await check()
            if self._settlement is not None:
                # An occupied pinned slot means this lane does not own its capacity.
                await self._settlement.require_idle()
            dispatched = True
            response = await self._client.send(request, stream=True)
            head = BrokerResponse(
                context["request_id"], response.status_code,
                tuple(response.headers.raw),
            )
            header_bytes = sum(len(k) + len(v) + 4 for k, v in head.headers)
            header_prefix = bytearray()
            for key, value in head.headers:
                for part in (key, b": ", value, b"\r\n"):
                    room = self.settings.max_header_bytes - len(header_prefix)
                    header_prefix.extend(part[:room])
            await self._record("response", {
                **context, "status": head.status_code,
                "header_bytes": header_bytes,
                "headers_truncated": header_bytes > self.settings.max_header_bytes,
            }, Snapshot((File(
                "headers.bin", bytes(header_prefix),
            ),)))
            if header_bytes > self.settings.max_header_bytes:
                raise IntegrityError("Provider headers exceed resource byte bound")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise IntegrityError("Encoded provider response is forbidden")
            await check()
            await on_response(head)
            # Iterate the public raw stream directly: aiter_raw() auto-closes
            # at EOF, outside our cancellation-resistant cleanup ownership.
            async for chunk in response.stream:
                prefix = chunk[:self.settings.max_frame_bytes]
                chunks += 1
                await self._record("chunk", {
                    **context, "index": chunks - 1, "received_bytes": len(chunk),
                    "truncated": len(prefix) != len(chunk),
                }, Snapshot((File("chunk.bin", prefix),)))
                if len(prefix) != len(chunk):
                    raise IntegrityError("Provider frame exceeds resource byte bound")
                await check()
                await on_chunk(chunk)
                delivered += 1
            complete = True
            await check()
        except BaseException as error:
            self._failed = True
            failure = type(error).__name__
            raise
        finally:
            async def finish():
                close_failure = None
                quiescence, settlement = "unknown", None
                try:
                    if response is not None:
                        await response.aclose()
                except BaseException as error:
                    close_failure = type(error).__name__
                    raise
                finally:
                    if dispatched and self._settlement is not None:
                        # Closing HTTP asks the server to stop; only the pinned
                        # slot going idle is recorded as upstream settlement.
                        try:
                            settlement = await self._settlement.settle()
                            quiescence = "pinned_slot_idle_confirmed"
                        except BaseException as error:
                            self._unsettled = True
                            settlement = {"error": type(error).__name__}
                    await self._record("end", {
                        **context, "status": head.status_code if head else None,
                        "dispatch_attempted": dispatched,
                        "http_body_complete": complete,
                        "failure": failure, "close_failure": close_failure,
                        "chunks": chunks, "delivery_callbacks_completed": delivered,
                        "broker_close_requested": self._closed,
                        "upstream_quiescence": quiescence,
                        "settlement": settlement,
                    })

            try:
                await _settle(asyncio.create_task(finish()))
            except BaseException as error:
                self._failed = True
                if isinstance(error, asyncio.CancelledError):
                    await self._record("cancelled_during_settlement", context)
                raise
        # Settlement yields; authorization can change while the final record is
        # committing. Successful return has its own delivery fence.
        try:
            await check()
        except BaseException as error:
            self._failed = True
            await self._record("delivery_blocked", {
                **context, "failure": type(error).__name__,
            })
            raise
        return head

    async def close(self):
        if self._active is asyncio.current_task():
            raise IntegrityError("Close broker from outside its delivery callback")
        self._closed = True
        if self._close_task is None:
            active = self._active
            settled = self._active_settled if active is not None else None

            async def finish():
                if active is not None:
                    active.cancel()
                    await settled.wait()
                await self._client.aclose()
                if self._settlement is not None:
                    await self._settlement.aclose()

            self._close_task = asyncio.create_task(finish())
        await _settle(self._close_task)
