"""Run one research delegation inside the sandboxed opencode server.

This is the opencode-backend twin of ``engine.subagent.run_subagent``:
it yields the same ``SubagentStep``/``SubagentResult`` items in the
same shapes, so the turn pipeline - ``subagent_*`` SSE events, the
one-line ``SubagentTrace``, the phase-two replay - is shared unchanged.
The final-answer contract (one fenced JSON block) and the source
extraction are the legacy helpers themselves; nothing here re-implements
them.

What is different, and why it is safe against the single-model-slot
server behind both backends: one delegation is one message to one
opencode session, the client enforces the wallclock and aborts the
session when it is exceeded, and delegation attempts on one recollect
session still serialize on the per-session turn lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ...config import RecollectConfig
from .. import subagent
from ..subagent import SubagentResult, SubagentStep
from .configgen import AGENT_NAME, FINALIZER_NAME, MCP_SERVER
from .manager import SandboxHandle, SandboxManager, SandboxStartError

#: After a wallclock abort, how long the in-flight message request may
#: still take to unwind before we give up on it.
_GRACE_S = 10.0


def _display_tool(name: str) -> str:
    """MCP tools arrive prefixed with the server name; the UI and the
    trace expect the canonical tool names."""
    prefix = f"{MCP_SERVER}_"
    return name[len(prefix):] if name.startswith(prefix) else name


def _last_text(parts: Any) -> str:
    """The assistant's text from a message's parts, synthetic aside."""
    if not isinstance(parts, list):
        return ""
    texts = [
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict)
        and part.get("type") == "text"
        and not part.get("synthetic")
        and not part.get("ignored")
        and str(part.get("text", "")).strip()
    ]
    return "\n".join(texts).strip()


class OpenCodeRunner:
    """Yield steps as they happen and a single result, like run_subagent."""

    def __init__(
        self,
        manager: SandboxManager,
        config: RecollectConfig,
        *,
        observation_chars: int | None = None,
        wallclock_s: float | None = None,
    ) -> None:
        self._manager = manager
        self._config = config
        self._observation_chars = (
            observation_chars
            if observation_chars is not None
            else config.subagent_observation_chars
        )
        self._wallclock_s = (
            wallclock_s if wallclock_s is not None else config.sandbox_wallclock_s
        )

    async def run(
        self, session_id: str, task: str
    ) -> AsyncIterator[SubagentStep | SubagentResult]:
        started = time.perf_counter()
        try:
            handle = await self._manager.ensure(session_id)
        except SandboxStartError as error:
            yield self._error_result(task, str(error), [], [], started)
            return

        oc_id = handle.oc_session_id or ""
        handle.busy = True
        handle.last_used = time.monotonic()
        steps: list[SubagentStep] = []
        sources: list[str] = []
        seen_calls: set[str] = set()
        children: set[str] = set()
        events: asyncio.Queue[dict] = asyncio.Queue()
        deadline = started + self._wallclock_s
        final_text: str | None = None
        aborted = False

        pump = asyncio.create_task(self._pump_events(handle, events))
        message = asyncio.create_task(self._post_message(handle, task))
        try:
            while not message.done():
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                # Both sides wake the loop: a new event, or the message
                # finishing while the stream is quiet. Waiting only on the
                # queue would hold a finished (or failed) request hostage
                # until wallclock.
                get_task = asyncio.create_task(events.get())
                try:
                    await asyncio.wait(
                        {message, get_task},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not get_task.done():
                        get_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await get_task
                if get_task.cancelled() or not get_task.done():
                    continue
                event = get_task.result()
                handle.last_used = time.monotonic()
                step = self._apply_event(
                    event, oc_id, children, seen_calls, steps, sources
                )
                if step is not None:
                    yield step
            aborted = not message.done()
            if aborted:
                with contextlib.suppress(httpx.HTTPError):
                    await handle.client.post(f"/session/{oc_id}/abort", timeout=5.0)
                # The abort has already told opencode to stop the session;
                # this only waits for the HTTP request to unwind.
                with contextlib.suppress(Exception):  # noqa: BLE001
                    await asyncio.wait_for(message, timeout=_GRACE_S)
        finally:
            handle.busy = False
            handle.last_used = time.monotonic()
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

        total_ms = (time.perf_counter() - started) * 1_000.0

        if aborted:
            # A cap ends browsing, not synthesis: if the aborted session
            # left usable text, a capped run reads like the legacy's
            # capped run, not like nothing.
            final_text = await self._fetch_final_text(handle)
            yield self._partial(
                task, "sandbox wallclock reached", final_text, steps, sources, total_ms
            )
            return

        try:
            payload = message.result()
        except Exception as error:  # noqa: BLE001 - surfaced on the result
            yield self._error_result(
                task, f"opencode request failed: {error}", steps, sources, started
            )
            return
        final_text = _last_text(payload.get("parts"))
        if subagent._parse_final(final_text) is None:
            # A cap ends browsing, not synthesis. opencode enforces its own
            # step cap by forcing a text-only wrap-up, and the local model
            # answers that in prose rather than the JSON receipt - so a run
            # that did the research lands here with its evidence about to be
            # thrown away. One more pass, over the same session, gets the
            # receipt out of what it already gathered.
            #
            # Only on this path: the aborted branch above returns before it,
            # and it is reached exactly when the wallclock is already spent.
            recovered = await self._finalize_partial(handle, deadline)
            if recovered and subagent._parse_final(recovered) is not None:
                # Partial, not ok, and for the same reason the legacy
                # backend calls a finalized run partial: a receipt that
                # had to be asked for twice is not evidence of a clean
                # finish. The findings and sources are real and are
                # handed over in full; only the "treat this as partial"
                # note is added on top.
                yield self._partial(
                    task,
                    "the sandbox's own wrap-up returned no JSON receipt",
                    recovered,
                    steps,
                    sources,
                    (time.perf_counter() - started) * 1_000.0,
                )
                return
            total_ms = (time.perf_counter() - started) * 1_000.0
        yield self._finished(task, final_text, steps, sources, total_ms)

    # -- event flow -----------------------------------------------------------

    async def _pump_events(
        self, handle: SandboxHandle, queue: asyncio.Queue[dict]
    ) -> None:
        """Forward the server's event stream. A dead stream must not kill
        the delegation: the message request still completes on its own."""
        try:
            async with handle.client.stream("GET", "/event", timeout=None) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    with contextlib.suppress(json.JSONDecodeError):
                        queue.put_nowait(json.loads(payload))
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError:
            pass

    def _apply_event(
        self,
        event: Any,
        oc_id: str,
        children: set[str],
        seen_calls: set[str],
        steps: list[SubagentStep],
        sources: list[str],
    ) -> SubagentStep | None:
        """One event from the stream. Returns a step when a tool call in
        this delegation's session tree finished."""
        if not isinstance(event, dict):
            return None
        etype = event.get("type")
        properties = event.get("properties") or {}
        if oc_id and etype in ("session.created", "session.updated"):
            info = properties.get("info") or properties
            if (
                isinstance(info, dict)
                and info.get("parentID") == oc_id
                and info.get("id")
            ):
                children.add(str(info["id"]))
                return None
        if etype != "message.part.updated":
            return None
        part = properties.get("part") or {}
        if part.get("type") != "tool":
            return None
        # Work in the subagent's child sessions belongs to this
        # delegation; everything else on the stream does not.
        session = part.get("sessionID", "")
        if session != oc_id and session not in children:
            return None
        state = part.get("state") or {}
        status = state.get("status")
        if status not in ("completed", "error"):
            return None
        key = part.get("callID") or part.get("id") or ""
        if not key or key in seen_calls:
            return None
        seen_calls.add(key)
        tool = _display_tool(str(part.get("tool", "unknown")))
        args = state.get("input") or {}
        if not isinstance(args, dict):
            args = {"raw": args}
        stamps = state.get("time") or {}
        ms = max(0.0, float(stamps.get("end", 0)) - float(stamps.get("start", 0)))
        if status == "completed":
            observation = str(state.get("output", ""))
        else:
            observation = str(state.get("error", ""))
        if observation.startswith("{"):
            sources.extend(subagent._source_urls(observation))
        step = SubagentStep(
            index=len(steps) + 1,
            tool=tool,
            args=args,
            observation=observation[: self._observation_chars],
            ms=round(ms, 1),
        )
        steps.append(step)
        return step

    # -- request plumbing ---------------------------------------------------

    async def _post_message(self, handle: SandboxHandle, task: str) -> dict:
        response = await handle.client.post(
            f"/session/{handle.oc_session_id}/message",
            json={
                "agent": AGENT_NAME,
                "parts": [{"type": "text", "text": task}],
            },
            timeout=httpx.Timeout(self._wallclock_s + 30.0, connect=10.0),
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def _finalize_partial(
        self, handle: SandboxHandle, deadline: float
    ) -> str:
        """One tools-disabled pass at the final receipt, budget permitting.

        The legacy backend re-streams with ``tools=None``; opencode has no
        such flag on a message, so the tools come off the *agent* instead -
        ``FINALIZER_NAME`` is the same researcher with an empty tool
        surface. The message goes to the same opencode session, so the
        model is finalizing over the evidence it gathered rather than
        starting the task again.

        This is a synthesis pass, so it costs nothing the caller can see:
        it cannot call tools, the event pump is already cancelled, and no
        ``SubagentStep`` can come out of it. It gets only the wallclock
        that was left over; with none left, or on any failure, it returns
        ``""`` and the caller keeps today's partial result.
        """
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return ""
        try:
            response = await handle.client.post(
                f"/session/{handle.oc_session_id}/message",
                json={
                    "agent": FINALIZER_NAME,
                    "parts": [
                        {"type": "text", "text": subagent._FINALIZE_PARTIAL}
                    ],
                },
                timeout=httpx.Timeout(remaining, connect=10.0),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return _last_text(payload.get("parts"))

    async def _fetch_final_text(self, handle: SandboxHandle) -> str:
        """The last assistant text in the session, for capped runs."""
        try:
            response = await handle.client.get(
                f"/session/{handle.oc_session_id}/message", timeout=10.0
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return ""
        messages = response.json()
        if not isinstance(messages, list):
            return ""
        for entry in reversed(messages):
            if not isinstance(entry, dict):
                continue
            info = entry.get("info") or {}
            if info.get("role") != "assistant":
                continue
            text = _last_text(entry.get("parts"))
            if text:
                return text
        return ""

    # -- result assembly (shapes mirror run_subagent) -------------------------

    def _finished(
        self,
        task: str,
        final_text: str,
        steps: list[SubagentStep],
        sources: list[str],
        total_ms: float,
    ) -> SubagentResult:
        finalized = subagent._parse_final(final_text)
        if finalized is None:
            return subagent._result(
                task=task,
                status="partial",
                result_json=json.dumps(
                    {
                        "summary": (
                            "The subagent stopped without returning a "
                            "well-formed result."
                        ),
                        "note": subagent._PARTIAL_NOTE,
                        "partial_text": final_text[:1_000],
                    },
                    ensure_ascii=False,
                ),
                summary="",
                sources=sources,
                steps=steps,
                total_ms=total_ms,
                error="malformed or missing final JSON",
            )
        return subagent._result(
            task=task,
            status="ok",
            result_json=finalized["json"],
            summary=finalized["summary"],
            sources=finalized["sources"],
            steps=steps,
            total_ms=total_ms,
            error=None,
        )

    def _partial(
        self,
        task: str,
        reason: str,
        final_text: str,
        steps: list[SubagentStep],
        sources: list[str],
        total_ms: float,
    ) -> SubagentResult:
        finalized = subagent._parse_final(final_text)
        if finalized is not None:
            document = json.loads(finalized["json"])
            document["note"] = subagent._PARTIAL_NOTE
            document["stop_reason"] = reason
            return subagent._result(
                task=task,
                status="partial",
                result_json=json.dumps(document, ensure_ascii=False),
                summary=finalized["summary"],
                sources=[*sources, *finalized["sources"]],
                steps=steps,
                total_ms=total_ms,
                error=reason,
            )
        return subagent._result(
            task=task,
            status="partial",
            result_json=json.dumps(
                {
                    "summary": f"The subagent stopped: {reason}.",
                    "note": subagent._PARTIAL_NOTE,
                    "sources": sources,
                },
                ensure_ascii=False,
            ),
            summary="",
            sources=sources,
            steps=steps,
            total_ms=total_ms,
            error=reason,
        )

    def _error_result(
        self,
        task: str,
        error: str,
        steps: list[SubagentStep],
        sources: list[str],
        started: float,
    ) -> SubagentResult:
        return subagent._result(
            task=task,
            status="error",
            result_json=json.dumps({"error": error}, ensure_ascii=False),
            summary="",
            sources=sources,
            steps=steps,
            total_ms=(time.perf_counter() - started) * 1_000.0,
            error=error,
        )
