"""Run one research delegation inside the sandboxed opencode server.

This is the opencode-backend twin of ``engine.subagent.run_subagent``:
it yields the same ``SubagentStep``/``SubagentResult`` items in the
same shapes, so the turn pipeline - ``subagent_*`` SSE events, the
one-line ``SubagentTrace``, the phase-two replay - is shared unchanged.
Structured receipts are accepted for compatibility, while ordinary native
OpenCode prose is wrapped deterministically for the shared turn pipeline.
No extra synthesis generation or replacement system prompt is introduced.

What is different, and why it is safe against the single-model-slot
server behind both backends: one delegation is one message to one
opencode session, and the run is bounded by opencode's own step cap
rather than a client-side wallclock, so a long research pass is never
killed mid-flight; delegation attempts on one recollect session still
serialize on the per-session turn lock.
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
from ..subagent import SubagentEffort, SubagentResult, SubagentStep
from .configgen import AGENT_NAME, MCP_SERVER
from .manager import (
    SandboxHandle,
    SandboxInvocation,
    SandboxManager,
    SandboxStartError,
)

#: The delegation request lives as long as opencode's own turn - bounded by
#: opencode's step cap, never by a client-side timer - so the only timeouts
#: kept are on establishing and sending, not on reading.
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=None, pool=None)


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


def _looks_like_cap_banner(text: str) -> bool:
    """Recognize the OpenCode instruction the model may recite at its cap."""
    normalized = text.upper()
    return "MAXIMUM STEPS REACHED" in normalized or (
        "OVERRIDES ALL OTHER INSTRUCTIONS" in normalized
        and "TOOLS ARE DISABLED" in normalized
    )


class OpenCodeRunner:
    """Yield steps as they happen and a single result, like run_subagent."""

    def __init__(
        self,
        manager: SandboxManager,
        config: RecollectConfig,
        *,
        observation_chars: int | None = None,
    ) -> None:
        self._manager = manager
        self._config = config
        self._observation_chars = (
            observation_chars
            if observation_chars is not None
            else config.subagent_observation_chars
        )

    async def run(
        self,
        session_id: str,
        task: str,
        *,
        effort: SubagentEffort = "focused",
    ) -> AsyncIterator[SubagentStep | SubagentResult]:
        started = time.perf_counter()
        try:
            invocation = await self._manager.begin_invocation(session_id)
        except (SandboxStartError, httpx.HTTPError, OSError, ValueError) as error:
            result = self._error_result(task, str(error), [], [], started)
            result.effort = effort
            result.backend = "opencode"
            result.isolation = "unavailable"
            result.fresh_context = False
            yield result
            return

        try:
            async for item in self._run_invocation(
                invocation,
                task,
                subagent.transfer_task(task, effort),
                started,
            ):
                if isinstance(item, SubagentResult):
                    item.effort = effort
                    item.backend = "opencode"
                    item.isolation = invocation.handle.isolation
                    item.fresh_context = True
                    item.server_reused = invocation.process_reused
                yield item
        finally:
            await self._manager.finish_invocation(invocation)

    async def _run_invocation(
        self,
        invocation: SandboxInvocation,
        task: str,
        delegated_task: str,
        started: float,
    ) -> AsyncIterator[SubagentStep | SubagentResult]:
        handle = invocation.handle
        oc_id = invocation.oc_session_id
        steps: list[SubagentStep] = []
        sources: list[str] = []
        seen_calls: set[str] = set()
        children: set[str] = set()
        events: asyncio.Queue[dict] = asyncio.Queue()
        final_text: str | None = None

        pump = asyncio.create_task(self._pump_events(handle, events))
        message = asyncio.create_task(
            self._post_message(handle, oc_id, delegated_task)
        )
        try:
            while not message.done():
                # Both sides wake the loop: a new event, or the message
                # finishing while the stream is quiet. Waiting only on the
                # queue would hold a finished (or failed) request hostage.
                get_task = asyncio.create_task(events.get())
                try:
                    await asyncio.wait(
                        {message, get_task},
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
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

        total_ms = (time.perf_counter() - started) * 1_000.0

        try:
            payload = message.result()
        except Exception as error:  # noqa: BLE001 - surfaced on the result
            yield self._error_result(
                task, f"opencode request failed: {error}", steps, sources, started
            )
            return
        final_text = _last_text(payload.get("parts"))
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

    async def _post_message(
        self, handle: SandboxHandle, oc_session_id: str, task: str
    ) -> dict:
        response = await handle.client.post(
            f"/session/{oc_session_id}/message",
            json={
                "agent": AGENT_NAME,
                "parts": [{"type": "text", "text": task}],
            },
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

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
        if finalized is None and (
            not final_text or _looks_like_cap_banner(final_text)
        ):
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
                        "sources": sources,
                    },
                    ensure_ascii=False,
                ),
                summary="",
                sources=sources,
                steps=steps,
                total_ms=total_ms,
                error="malformed or missing final JSON",
            )
        if finalized is None:
            # Native OpenCode agents answer in prose. Wrapping that prose
            # deterministically preserves their base behavior without a
            # second model pass or a replacement system prompt.
            return subagent._result(
                task=task,
                status="ok",
                result_json=json.dumps(
                    {
                        "summary": final_text,
                        "findings": [],
                        "sources": sources,
                    },
                    ensure_ascii=False,
                ),
                summary=final_text,
                sources=sources,
                steps=steps,
                total_ms=total_ms,
                error=None,
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
