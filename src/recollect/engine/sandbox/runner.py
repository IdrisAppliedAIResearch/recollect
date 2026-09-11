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
killed mid-flight. All delegations serialize through the shared sandbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from ...config import RecollectConfig
from .. import subagent
from ..subagent import SubagentEffort, SubagentResult, SubagentStep
from .configgen import AGENT_NAME, MCP_SERVER
from .evidence import ResearchEvidence
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
_EVENT_QUEUE_SIZE = 32
_RECONCILE_SECONDS = 2.0


@dataclass(frozen=True)
class TaskCommand:
    message_id: str
    kind: Literal["steer", "cancel"]
    text: str = ""
    revision: int = 1


@dataclass(frozen=True)
class TaskReport:
    kind: str
    text: str
    revision: int
    related_message_id: str = ""
    sources: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    native_session_id: str = ""
    call_id: str = ""


ReportCallback = Callable[[TaskReport], Awaitable[None]]
WorkspaceCallback = Callable[[Path], Awaitable[None]]


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

        completed = False
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
            completed = True
        finally:
            cleanup = asyncio.create_task(
                self._cleanup_invocation(invocation, abort=not completed)
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Do not release the global model slot until OpenCode has
                # stopped and ephemeral state has been cleared.
                await cleanup
                raise

    async def _cleanup_invocation(
        self, invocation: SandboxInvocation, *, abort: bool
    ) -> None:
        if abort:
            with contextlib.suppress(httpx.HTTPError):
                response = await invocation.handle.client.post(
                    f"/session/{invocation.oc_session_id}/abort", timeout=10.0
                )
                response.raise_for_status()
        await self._manager.finish_invocation(invocation)

    async def run_continuous(
        self,
        session_id: str,
        task: str,
        *,
        commands: asyncio.Queue[TaskCommand],
        report: ReportCallback,
        revision: int = 1,
        effort: SubagentEffort = "focused",
        restore_workspace: WorkspaceCallback | None = None,
        save_workspace: WorkspaceCallback | None = None,
    ) -> AsyncIterator[SubagentStep | SubagentResult]:
        """Run owned work independently of a foreground response's lifetime.

        Per-request admission belongs to the manager's configured model ingress.
        Native sessions live for this invocation; later invocations restore only
        validated workspace files and the caller's structured checkpoint.
        """
        started = time.perf_counter()
        try:
            invocation = await self._manager.begin_invocation(
                session_id, continuous=True
            )
        except (SandboxStartError, httpx.HTTPError, OSError, ValueError) as error:
            yield self._error_result(task, str(error), [], [], started)
            return
        final: SubagentResult | None = None
        try:
            if restore_workspace is not None:
                await restore_workspace(invocation.handle.workdir)
            async for item in self._continuous_invocation(
                invocation, task, commands, report, revision, effort, started,
                save_workspace,
            ):
                if isinstance(item, SubagentResult):
                    final = item
                else:
                    yield item
        finally:
            # Abort native children too, including background task tools, before
            # reading files or allowing a different conversation to own scratch.
            async def finish() -> None:
                try:
                    await self._manager.quiesce_invocation(invocation)
                    if save_workspace is not None:
                        await save_workspace(invocation.handle.workdir)
                finally:
                    await self._manager.finish_invocation(invocation)

            cleanup = asyncio.create_task(finish())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
        if final is not None:
            final.effort = effort
            final.backend = "opencode"
            final.isolation = invocation.handle.isolation
            final.fresh_context = True
            final.server_reused = invocation.process_reused
            yield final

    async def _continuous_invocation(
        self,
        invocation: SandboxInvocation,
        task: str,
        commands: asyncio.Queue[TaskCommand],
        report: ReportCallback,
        revision: int,
        effort: SubagentEffort,
        started: float,
        save_workspace: WorkspaceCallback | None,
    ) -> AsyncIterator[SubagentStep | SubagentResult]:
        handle, oc_id = invocation.handle, invocation.oc_session_id
        steps: list[SubagentStep] = []
        sources: list[str] = []
        children: set[str] = set()
        seen_calls: set[str] = set()
        seen_reports: set[tuple[str, str]] = set()
        revisions = {revision: ""}
        issued: set[str] = set()
        accepted: set[int] = set()
        result_revisions: set[int] = set()
        final_reports: dict[int, TaskReport] = {}
        assistant_ids: set[str] = set()
        events: asyncio.Queue[dict] = asyncio.Queue(maxsize=_EVENT_QUEUE_SIZE)
        pump = asyncio.create_task(self._pump_events(handle, events))
        messages: list[asyncio.Task[dict]] = []
        cancelled = False
        failures = 0
        next_reconcile = 0.0
        pending_commands: list[TaskCommand] = []
        waiting_for_input = False
        checkpoint_assistants = 0
        checkpoint_evidence = 0
        evidence = ResearchEvidence()
        empty_checkpoints = 0
        recovery_revision = None
        acknowledgment_reminders: set[int] = set()

        async def apply(event: dict) -> SubagentStep | None:
            nonlocal waiting_for_input
            entry = self._report_from_event(event, oc_id, children)
            if entry is not None:
                key = (entry.native_session_id, entry.call_id)
                if key not in seen_reports and entry.revision in revisions:
                    valid = entry.kind != "accepted" or (
                        entry.native_session_id == oc_id
                        and entry.related_message_id == revisions[entry.revision]
                    )
                    if valid and (
                        entry.kind == "accepted" or entry.revision == max(revisions)
                    ):
                        await report(entry)
                        seen_reports.add(key)
                        sources.extend(entry.sources)
                        if entry.kind == "accepted":
                            accepted.add(entry.revision)
                            if entry.revision == max(revisions):
                                waiting_for_input = False
                        elif (
                            entry.kind in {"question", "blocked"}
                            and entry.native_session_id == oc_id
                            and entry.revision == max(revisions)
                        ):
                            waiting_for_input = True
                        elif (
                            entry.kind == "result"
                            and entry.native_session_id == oc_id
                            and entry.revision == max(revisions)
                        ):
                            waiting_for_input = False
                            result_revisions.add(entry.revision)
                            final_reports[entry.revision] = entry
            step = self._apply_event(
                event, oc_id, children, seen_calls, steps, sources,
                scope_calls=True,
            )
            if step is not None and step.tool not in {"report_message", "todowrite"}:
                state = ((event.get("properties") or {}).get("part") or {}).get(
                    "state", {}
                )
                if state.get("status") == "completed":
                    evidence.observe_call(
                        step.tool, step.args, state.get("output", ""),
                    )
            return step

        def submit(text: str) -> None:
            messages.append(asyncio.create_task(
                self._post_message(handle, oc_id, text)
            ))

        submit(self._continuous_prompt(
            subagent.transfer_task(task, effort), revision, ""
        ))
        try:
            while True:
                # Drain control before deciding that the last native response
                # finishes the task. Late commands become another native turn.
                steering = []
                while pending_commands or not commands.empty():
                    command = (
                        pending_commands.pop(0)
                        if pending_commands else commands.get_nowait()
                    )
                    if command.message_id in issued:
                        continue
                    issued.add(command.message_id)
                    if command.kind == "cancel":
                        cancelled = True
                        await self._manager.quiesce_invocation(invocation)
                        await report(TaskReport(
                            "canceled", "Delegated work was canceled.",
                            max(revisions), command.message_id,
                            native_session_id=oc_id,
                        ))
                        break
                    if command.revision <= max(revisions):
                        continue
                    revisions[command.revision] = command.message_id
                    steering.append(command)
                if cancelled:
                    break
                if steering:
                    # A native task tool can block the parent on child research.
                    # Resume its saved conversation instead of queuing the new
                    # direction behind all of that now-superseded work.
                    await self._manager.quiesce_invocation(invocation)
                    for message in messages:
                        if not message.done():
                            message.cancel()
                    await asyncio.gather(*messages, return_exceptions=True)
                    messages.clear()
                    evidence.unchanged_calls = 0
                    submit(self._continuous_prompt(
                        "\n\n".join(command.text for command in steering),
                        steering[-1].revision, steering[-1].message_id,
                    ))
                elif evidence.unchanged_calls >= 6 and not waiting_for_input:
                    if recovery_revision == max(revisions):
                        raise RuntimeError(
                            "Research kept repeating without new evidence after "
                            "a request to synthesize. Verified findings are saved."
                        )
                    await self._manager.quiesce_invocation(invocation)
                    for message in messages:
                        if not message.done():
                            message.cancel()
                    await asyncio.gather(*messages, return_exceptions=True)
                    messages.clear()
                    recovery_revision = max(revisions)
                    evidence.unchanged_calls = 0
                    submit(self._continuous_prompt(
                        "The last six research calls added no evidence. Stop "
                        "retrying those sources. Finish the latest requested "
                        "scope from verified evidence already collected, and "
                        "explicitly identify facts you could not verify. Send "
                        "the substantive answer with kind=result. Do not "
                        "invent missing facts or create unrequested files.",
                        max(revisions), revisions[max(revisions)],
                    ))
                while not events.empty():
                    step = await apply(events.get_nowait())
                    if step is not None:
                        yield step
                messages_done = all(message.done() for message in messages)
                if time.monotonic() >= next_reconcile or (
                    messages_done and not waiting_for_input
                ):
                    try:
                        async for event in self._history_events(
                            handle, oc_id, children, assistant_ids
                        ):
                            step = await apply(event)
                            if step is not None:
                                yield step
                        failures = 0
                    except (httpx.HTTPError, ValueError):
                        failures += 1
                        if failures >= 3:
                            raise RuntimeError(
                                "OpenCode history reconciliation failed three times; "
                                "saved work is preserved for continuation."
                            ) from None
                    next_reconcile = time.monotonic() + _RECONCILE_SECONDS
                    if pump.done():
                        pump = asyncio.create_task(self._pump_events(handle, events))
                if (
                    all(message.done() for message in messages)
                    and not waiting_for_input
                ):
                    # Give a command accepted in the same loop turn precedence
                    # over publishing a result from an earlier instruction.
                    if pending_commands or not commands.empty():
                        continue
                    payload = messages[-1].result()
                    final_text = _last_text(payload.get("parts"))
                    native_error = (payload.get("info") or {}).get("error")
                    if (not native_error
                            and max(revisions) in result_revisions
                            and max(revisions) not in accepted
                            and max(revisions) not in acknowledgment_reminders):
                        acknowledgment_reminders.add(max(revisions))
                        await self._manager.quiesce_invocation(invocation)
                        await asyncio.gather(*messages, return_exceptions=True)
                        messages.clear()
                        submit(self._continuous_prompt(
                            "Your reported result and saved files are retained. "
                            "The required instruction acknowledgment is missing. "
                            "Send kind=accepted with the exact current revision "
                            "and related message ID below. Do not redo the work.",
                            max(revisions), revisions[max(revisions)],
                        ))
                        continue
                    capped = _looks_like_cap_banner(final_text) or (
                        len(assistant_ids) - checkpoint_assistants
                        >= self._config.sandbox_steps
                    )
                    if (
                        capped and not native_error
                        and max(revisions) not in result_revisions
                    ):
                        if evidence.version > checkpoint_evidence:
                            empty_checkpoints = 0
                        else:
                            empty_checkpoints += 1
                        checkpoint_evidence = evidence.version
                        if empty_checkpoints < 2:
                            await self._manager.quiesce_invocation(invocation)
                            if save_workspace is not None:
                                await save_workspace(handle.workdir)
                            await report(TaskReport(
                                "progress", "Saved a native execution checkpoint; "
                                "continuing the same task.", max(revisions),
                                native_session_id=oc_id,
                            ))
                            checkpoint_assistants = len(assistant_ids)
                            await asyncio.gather(*messages, return_exceptions=True)
                            messages.clear()
                            submit(self._continuous_prompt(
                                "Continue the current objective from the saved "
                                "work and native conversation. The previous "
                                "response reached an execution checkpoint. "
                                "Do not repeat unchanged unsuccessful actions.",
                                max(revisions), revisions[max(revisions)],
                            ))
                            continue
                    break
                event_wait = asyncio.create_task(events.get())
                command_wait = asyncio.create_task(commands.get())
                try:
                    await asyncio.wait(
                        {event_wait, command_wait, *(
                            message for message in messages if not message.done()
                        )},
                        timeout=max(0.01, next_reconcile - time.monotonic()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if event_wait.done():
                        step = await apply(event_wait.result())
                        if step is not None:
                            yield step
                    if command_wait.done():
                        pending_commands.append(command_wait.result())
                finally:
                    for pending in (event_wait, command_wait):
                        if not pending.done():
                            pending.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await pending

            if cancelled:
                result = self._error_result(
                    task, "canceled", steps, sources, started
                )
                result.status = "partial"
            else:
                payload = messages[-1].result()
                final_text = _last_text(payload.get("parts"))
                reported = final_reports.get(max(revisions))
                if reported is not None:
                    # Concurrent native message requests can close without prose;
                    # the authenticated parent report is the completed answer.
                    final_text = json.dumps({
                        "summary": reported.text, "findings": [],
                        "sources": reported.sources,
                    }, ensure_ascii=False)
                result = self._finished(
                    task, final_text, steps,
                    sources,
                    (time.perf_counter() - started) * 1000,
                )
                native_error = (payload.get("info") or {}).get("error")
                if native_error:
                    result = self._error_result(
                        task, f"OpenCode execution failed: {native_error}",
                        steps, sources, started,
                    )
                elif max(revisions) not in accepted:
                    result.status = "partial"
                    result.error = "latest instruction revision was not acknowledged"
                elif max(revisions) not in result_revisions and (
                    _looks_like_cap_banner(final_text) or (
                        len(assistant_ids) - checkpoint_assistants
                        >= self._config.sandbox_steps
                    )
                ):
                    result.status = "partial"
                    result.error = (
                        "native checkpoints repeated without new evidence; "
                        "saved work requires a new direction"
                    )
                if result.status != "ok":
                    await report(TaskReport(
                        "blocked", result.error or "Work requires continuation.",
                        max(revisions), native_session_id=oc_id,
                    ))
            yield result
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            await report(TaskReport(
                "blocked", str(error)[:4000], max(revisions),
                native_session_id=oc_id,
            ))
            yield self._error_result(task, str(error), steps, sources, started)
        finally:
            pump.cancel()
            for message in messages:
                if not message.done():
                    message.cancel()
            await asyncio.gather(pump, *messages, return_exceptions=True)

    @staticmethod
    def _continuous_prompt(text: str, revision: int, message_id: str) -> str:
        return (
            f"Instruction revision: {revision}\n"
            f"Related message ID: {message_id}\n"
            "Load the recollect-reporting skill before working. Return findings "
            "in conversation by default. Create files only when the user "
            "requested a file; for creation or revision, load recollect-files.\n\n"
            + text
        )

    @staticmethod
    def _report_from_event(
        event: dict, oc_id: str, children: set[str]
    ) -> TaskReport | None:
        if event.get("type") != "message.part.updated":
            return None
        part = (event.get("properties") or {}).get("part") or {}
        state = part.get("state") or {}
        session_id = part.get("sessionID")
        call_id = part.get("callID")
        if (
            part.get("type") != "tool"
            or _display_tool(str(part.get("tool"))) != "report_message"
            or state.get("status") != "completed"
            or session_id not in {oc_id, *children}
            or not isinstance(call_id, str) or not call_id
        ):
            return None
        try:
            data = json.loads(state.get("output", ""))
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(state.get("input"), dict):
            return None
        args = state["input"]
        if any(data.get(key) != args.get(key) for key in ("kind", "revision")):
            return None
        if data.get("text") != str(args.get("text", "")).strip():
            return None
        if data.get("kind") not in {
            "accepted", "progress", "finding", "question", "blocked", "result"
        }:
            return None
        if (
            type(data.get("revision")) is not int or data["revision"] < 1
            or not isinstance(data.get("text"), str)
            or not 1 <= len(data["text"]) <= 4000
            or not isinstance(data.get("related_message_id", ""), str)
            or len(data.get("related_message_id", "")) > 128
        ):
            return None
        for key in ("sources", "artifacts"):
            values = data.get(key, [])
            if (not isinstance(values, list) or len(values) > 32
                    or any(not isinstance(v, str) or len(v) > 2048 for v in values)):
                return None
        return TaskReport(
            kind=data["kind"], text=data["text"], revision=data["revision"],
            related_message_id=data.get("related_message_id", ""),
            sources=data.get("sources", []), artifacts=data.get("artifacts", []),
            native_session_id=session_id, call_id=call_id,
        )

    async def _history_events(
        self,
        handle: SandboxHandle,
        oc_id: str,
        children: set[str],
        assistant_ids: set[str],
    ) -> AsyncIterator[dict]:
        response = await handle.client.get(f"/session/{oc_id}/children", timeout=10)
        response.raise_for_status()
        for child in response.json():
            if isinstance(child, dict) and child.get("parentID") == oc_id:
                children.add(str(child["id"]))
        for native_id in (*sorted(children), oc_id):
            before = ""
            while True:
                params: dict[str, str | int] = {"limit": 100}
                if before:
                    params["before"] = before
                response = await handle.client.get(
                    f"/session/{native_id}/message", params=params, timeout=10
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError("invalid OpenCode message history")
                for message in payload:
                    info = message.get("info") or {}
                    if native_id == oc_id and info.get("role") == "assistant":
                        assistant_ids.add(str(info.get("id")))
                    for part in message.get("parts", []):
                        # The authenticated history endpoint, not tool input,
                        # establishes ownership when repairing a missed event.
                        if part.get("sessionID") == native_id:
                            yield {
                                "type": "message.part.updated",
                                "properties": {"part": part},
                            }
                cursor = response.headers.get("X-Next-Cursor", "")
                if not cursor or cursor == before:
                    break
                before = cursor

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
        events: asyncio.Queue[dict] = asyncio.Queue(maxsize=_EVENT_QUEUE_SIZE)
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
            if not message.done():
                message.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await message

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
                        # Pause network reads when the UI stops consuming steps.
                        await queue.put(json.loads(payload))
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
        *,
        scope_calls: bool = False,
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
        if key and scope_calls:
            key = f"{session}:{key}"
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
