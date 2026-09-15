"""Native OpenCode session prerequisite, deliberately not an execution receipt.

The owner supplies an isolated transport and a host-only journal. This module
does not launch processes, attest containment, capture candidates or authorize
checkpoints. Never pass its responses to a controller as trusted check results.
"""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass

import httpx

from .contracts import ChangePolicy, File, Snapshot
from .journal import EMPTY_SNAPSHOT, MAX_FILE_BYTES, IntegrityError, encode, sha256

VERSION = "1.18.18"
PROVIDER = "recollect"
AUTHORITY_PATH = "/authority/task.json"


@dataclass(frozen=True)
class NativeSettings:
    model: str
    context_limit: int
    output_limit: int
    policy: ChangePolicy
    authority: bytes

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.model):
            raise ValueError("Freeze a simple local model identity")
        if (type(self.context_limit) is not int
                or type(self.output_limit) is not int
                or not 0 < self.output_limit < self.context_limit - 8000):
            raise ValueError("Model capacity must leave room for compaction")
        if (type(self.authority) is not bytes or not self.authority
                or len(self.authority) > MAX_FILE_BYTES):
            raise ValueError("Freeze nonempty controller-owned task context")
        self.authority.decode("utf-8")
        if self.policy.delete:
            raise ValueError("Native prerequisite does not provision deletions")

    @property
    def config(self):
        # OS ownership, not permission globs, must enforce these exact paths.
        edits = {"*": "deny"}
        # The non-repository profile has project root '/', checked at admission.
        # Native edit/write permission patterns are relative to that root.
        edits.update({"work/" + p: "allow" for p in self.policy.modify})
        edits.update({"work/" + p + "/*": "allow"
                      for p in self.policy.create_under})
        permission = {
            "*": "deny", "read": "allow", "glob": "allow", "grep": "allow",
            "list": "allow", "edit": edits, "todowrite": "allow",
            "doom_loop": "allow", "external_directory": "deny",
        }
        return {
            "$schema": "https://opencode.ai/config.json",
            "model": f"{PROVIDER}/{self.model}",
            "small_model": f"{PROVIDER}/{self.model}",
            "enabled_providers": [PROVIDER], "default_agent": "build",
            "subagent_depth": 0, "plugin": [], "autoupdate": False,
            "share": "disabled", "snapshot": False, "formatter": False,
            "lsp": False, "mcp": {}, "instructions": [AUTHORITY_PATH],
            "compaction": {"auto": True, "prune": True, "reserved": 8000},
            "permission": permission,
            "agent": {
                "build": {"options": {}, "permission": permission},
                **{name: {"disable": True, "options": {}, "permission": {}}
                   for name in ("general", "plan", "explore", "scout", "title")},
            },
            "provider": {PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    # Only a container-local broker may listen here. No host URL,
                    # credential or unrestricted research MCP enters this config.
                    "baseURL": "http://127.0.0.1:4097/v1", "apiKey": "unused",
                    "timeout": False, "headerTimeout": False,
                    # Omit chunkTimeout: 1.18.18's schema rejects false/zero,
                    # and its fetch wrapper creates no timer when absent.
                },
                "models": {self.model: {
                    "name": self.model,
                    "limit": {"context": self.context_limit,
                              "output": self.output_limit},
                }},
            }},
        }

    @property
    def identity(self):
        return sha256(encode({
            "version": VERSION, "config": self.config,
            "authority_sha256": sha256(self.authority),
            "policy_sha256": self.policy.sha256,
        }))


async def _durable(function, *args):
    return await _settle(asyncio.create_task(asyncio.to_thread(function, *args)))


async def _settle(task):
    """Retain ownership until completion, then re-raise caller cancellation."""
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


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise IntegrityError("Duplicate native response key")
        result[key] = value
    return result


class NativeSession:
    """One retained conversation; transport errors/cancellation poison reuse.

    No default network transport is provided. A future qualified runtime must
    own the supplied transport, quiescence and candidate capture. HTTP abort or
    an idle response is not proof that processes or upstream inference stopped.
    The journal must be dedicated to this instance and outside worker mounts.
    """

    def __init__(self, settings: NativeSettings, journal, *, transport,
                 history_reader=None, history_journal=None):
        if not isinstance(transport, httpx.AsyncBaseTransport):
            raise ValueError("An explicit isolated native transport is required")
        if (history_reader is None) != (history_journal is None):
            raise ValueError("Native durable history needs both reader and journal")
        if history_journal is journal:
            raise ValueError("Native history requires a separate owned journal")
        self.settings, self.journal = settings, journal
        self._client = httpx.AsyncClient(
            base_url="http://native.invalid", transport=transport,
            trust_env=False, follow_redirects=False, timeout=None,
        )
        self._session = None
        self._busy = self._failed = self._closed = False
        self._close_task = None
        self._history_reader, self._history_journal = history_reader, history_journal
        self.history = None

    @property
    def session_id(self):
        return self._session

    def _claim(self):
        if self._busy or self._failed or self._closed:
            raise IntegrityError("Native session is busy, failed or closed")
        self._busy = True

    async def _record(self, kind, data, files=EMPTY_SNAPSHOT):
        await _durable(self.journal.append, kind, {
            "profile_sha256": self.settings.identity,
            "native_session_id": self._session, **data,
        }, files)

    async def _request(self, method, path, body=None, *, params=None):
        content = encode(body) if body is not None else b""
        if len(content) > MAX_FILE_BYTES:
            raise IntegrityError("Native request exceeds archive byte bound")
        request_id = uuid.uuid4().hex
        await self._record("native_request", {
            "request_id": request_id, "method": method, "path": path,
            "params": params,
        }, Snapshot((File("body.json", content),)))
        raw = bytearray()
        status, cursor, failure = None, "", None
        complete = False
        response = None
        try:
            request = self._client.build_request(
                method, path, content=content, params=params,
                headers={"Content-Type": "application/json",
                         "Accept-Encoding": "identity"},
            )
            response = await self._client.send(request, stream=True)
            status = response.status_code
            cursor = response.headers.get("X-Next-Cursor", "")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise IntegrityError("Encoded native response is forbidden")
            # HTTPX's aiter_raw closes at EOF outside this owned cleanup path.
            async for chunk in response.stream:
                room = MAX_FILE_BYTES - len(raw)
                raw.extend(chunk[:room])
                if len(chunk) > room:
                    raise IntegrityError("Native response exceeds archive byte bound")
            complete = True
            response.raise_for_status()
            value = json.loads(raw, object_pairs_hook=_pairs)
        except BaseException as error:
            failure = type(error).__name__
            raise
        finally:
            # A disconnected transport is not a process-stop receipt. Preserve
            # the prefix and local failure even when no response headers arrived.
            async def finish():
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    await self._record("native_response", {
                        "request_id": request_id, "status": status,
                        "next_cursor": cursor, "complete": complete,
                        "failure": failure,
                    }, Snapshot((File("body.json", bytes(raw)),)))

            await _settle(asyncio.create_task(finish()))
        return value, cursor

    async def start(self):
        self._claim()
        try:
            if self._session is not None:
                raise IntegrityError("Native session has already started")
            await self._record("native_profile", {
                "history_completeness": (
                    "committed_native_events_pending_verification"
                    if self._history_reader is not None else
                    "unqualified_across_automatic_pruning"
                ),
                "execution_receipt": False,
            }, Snapshot((
                File("config.json", encode(self.settings.config)),
                File("authority.json", self.settings.authority),
            )))
            health, _ = await self._request("GET", "/global/health")
            if health != {"healthy": True, "version": VERSION}:
                raise IntegrityError("Native version/health mismatch")
            config, _ = await self._request("GET", "/config")
            for key, value in self.settings.config.items():
                if config.get(key) != value:
                    raise IntegrityError("Native effective config mismatch: " + key)
            project, _ = await self._request("GET", "/project/current")
            if project.get("worktree") != "/":
                raise IntegrityError("Native permission root mismatch")
            session, _ = await self._request("POST", "/session", {})
            native_id = session.get("id")
            if not isinstance(native_id, str) or not re.fullmatch(
                r"ses_[A-Za-z0-9]+", native_id
            ) or session.get("parentID"):
                raise IntegrityError("Invalid native root session")
            self._session = native_id
            if self._history_reader is not None:
                from .native_history import NativeHistory

                self.history = NativeHistory(
                    native_id, self._history_journal, reader=self._history_reader,
                )
                await self.history.capture()
        except BaseException:
            self._failed = True
            raise
        finally:
            self._busy = False

    async def _history(self, *, expected=None):
        cursor, seen = "", set()
        found = False
        messages = []
        while True:
            params = {"limit": 100}
            if cursor:
                params["before"] = cursor
            page, cursor = await self._request(
                "GET", f"/session/{self._session}/message", params=params,
            )
            if type(page) is not list:
                raise IntegrityError("Invalid native history page")
            for message in page:
                self._message(message)
                messages.append(message)
                if expected and message["info"]["id"] == expected["info"]["id"]:
                    if found or message != expected:
                        raise IntegrityError("Native history response mismatch")
                    found = True
            if not cursor:
                if expected and not found:
                    raise IntegrityError("Native response missing from history")
                return messages
            if cursor in seen:
                raise IntegrityError("Native history cursor cycle")
            seen.add(cursor)

    def _message(self, message):
        info = message.get("info", {})
        if (info.get("sessionID") != self._session
                or not isinstance(info.get("id"), str)
                or type(message.get("parts")) is not list):
            raise IntegrityError("Foreign or malformed native message")
        for part in message["parts"]:
            if (part.get("sessionID") != self._session
                    or part.get("messageID") != info["id"]):
                raise IntegrityError("Foreign native message part")

    async def prompt(self, text):
        """Native tools execute within this request; prose is not a receipt."""
        self._claim()
        try:
            if self._session is None or not isinstance(text, str) or not text:
                raise IntegrityError("Start a native session before prompting")
            message_id = "msg_" + uuid.uuid4().hex
            before = None
            if self.history is not None:
                await self.history.capture()
                before = self.history.durable_sequence
            result, _ = await self._request(
                "POST", f"/session/{self._session}/message", {
                    "messageID": message_id, "agent": "build",
                    "model": {"providerID": PROVIDER,
                              "modelID": self.settings.model},
                    "system": self.settings.authority.decode("utf-8"),
                    "parts": [{"type": "text", "text": text}],
                },
            )
            self._message(result)
            info = result["info"]
            if (info.get("role") != "assistant" or info.get("error")
                    or info.get("finish") != "stop"
                    or not info.get("time", {}).get("completed")):
                raise IntegrityError("Incomplete native response")
            if self.history is not None:
                from .native_continuation import verify_reply
                from .native_history import iter_event_rows

                await self.history.capture()
                records = await _durable(self._history_journal.verify)
                await _durable(
                    verify_reply,
                    iter_event_rows(records, session_id=self._session), before,
                    message_id, result, self.settings.authority, text,
                    "build", {"modelID": self.settings.model, "providerID": PROVIDER},
                )
            elif info.get("parentID") != message_id:
                raise IntegrityError("Incomplete native response")
            await self._history(expected=result)
            return result
        except BaseException:
            self._failed = True
            raise
        finally:
            self._busy = False

    async def verify_projection(self):
        """Require the whole native projection to equal committed event state.

        Run only while native work is idle. Returns the durable watermark whose
        state was compared; a later advance must be treated as unverified history.
        """
        self._claim()
        try:
            if self.history is None or self._session is None:
                raise IntegrityError("Projection agreement needs durable history")
            from contextlib import closing

            from .native_continuation import verify_projection
            from .native_history import iter_event_rows

            await self.history.capture()
            watermark = self.history.durable_sequence
            messages = await self._history()

            def compare():
                # Stream verified journal records instead of materializing them.
                with closing(self._history_journal.iter_verify()) as records:
                    return verify_projection(
                        iter_event_rows(records, session_id=self._session), messages,
                    )

            count = await _durable(compare)
            await self.history.capture()
            if self.history.durable_sequence != watermark:
                raise IntegrityError("Native history advanced during projection check")
            await self._record("native_projection_verified", {
                "through_sequence": watermark, "messages": count,
            })
            return watermark
        except BaseException:
            self._failed = True
            raise
        finally:
            self._busy = False

    async def compact(self):
        """Explicit native compaction, with pre/post history archived separately.

        Automatic compaction remains enabled inside prompt(). Neither kind may
        update the frozen authority file, accepted plan or controller findings.
        """
        self._claim()
        try:
            if self._session is None:
                raise IntegrityError("Start a native session before compaction")
            await self._history()
            result, _ = await self._request(
                "POST", f"/session/{self._session}/summarize", {
                    "providerID": PROVIDER, "modelID": self.settings.model,
                    "auto": False,
                },
            )
            if result is not True:
                raise IntegrityError("Native compaction was not acknowledged")
            await self._history()
        except BaseException:
            self._failed = True
            raise
        finally:
            self._busy = False

    async def close(self):
        if self._busy:
            raise IntegrityError("Settle native work before closing transport")
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._client.aclose())
        await _settle(self._close_task)
