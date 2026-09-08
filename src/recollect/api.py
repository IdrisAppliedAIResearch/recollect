"""The HTTP surface: a trace API for the inspector, and OpenAI compatibility.

Two audiences, one process.

``/api/*`` is the real interface. It streams a turn as it happens and
serves the full ``TurnTrace`` behind it. The inspector is built on this.

``/v1/*`` is the OpenAI chat API, present so that Open WebUI - or any other
client, or a script - works without knowing anything about this project.
It is a lossy view on purpose: the OpenAI shape has nowhere to put a
retrieval trace, so those clients get the reply and nothing else, and the
trace is still recorded and still visible in the inspector afterwards.

**One deliberate incompatibility, and it is the whole point.** OpenAI
clients resend the entire transcript on every request. This system does
not want it: reconstructing the relevant past from an append-only store is
the mechanism being deployed. So the request's history is ignored and only
the final user message is read; everything else the model sees comes from
the store. Honouring the client's history instead would silently replace
the memory system with the client's scrollback and make every number in
the trace meaningless. Sessions are keyed off the request's ``user`` field
when present, so distinct users keep distinct memories.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from anyio import CancelScope
from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import RecollectConfig
from .engine._internals import LIBRARY_VERSION
from .engine.date_context import current_date_context, research_date_context
from .engine.embedder import HarnessEmbedder
from .engine.generator import (
    GenerationError,
    GenerationTrace,
    Generator,
    GeneratorSettings,
    StreamChunk,
    new_generation_trace,
)
from .engine.sandbox import OpenCodeRunner, SandboxManager
from .engine.subagent import (
    SubagentConfig,
    SubagentEffort,
    SubagentResult,
    SubagentStep,
    run_subagent,
    run_subagent_tool,
    transfer_task,
)
from .engine.voice import VoiceService
from .session import SessionInfo, SessionManager
from .trace import SubagentTrace, ToolCallTrace, TurnSummary, TurnTrace
from .voice_api import install_voice_routes

_VOICE_INSTRUCTIONS = (
    "This reply will be spoken aloud in a live voice conversation. "
    "Answer directly in one to three short sentences by default, then let "
    "the user ask a follow-up. Expand when the user explicitly asks for "
    "detail or when essential accuracy requires it. Use natural conversational "
    "prose without Markdown, headings, tables, or long lists. Express amounts, "
    "units, and symbols as spoken words, such as four hundred dollars per month. "
    "Avoid raw links, citation markup, and code; mention source names briefly "
    "when needed. Finish the answer naturally without cutting a sentence short."
)


def _turn_system_prompt(
    config: RecollectConfig, started_at: datetime, input_mode: str = "text",
) -> str:
    prompt = config.system_prompt
    if input_mode == "voice":
        prompt += "\n\n" + _VOICE_INSTRUCTIONS
    # A date stays stable throughout the day; seconds would invalidate the
    # memory prefix cache on every turn. Use one timestamp for all phases.
    return prompt + "\n\n" + current_date_context(started_at.astimezone(UTC).date())


class ChatRequest(BaseModel):
    session_id: str
    message: str
    input_mode: Literal["text", "voice"] = "text"


class CreateSession(BaseModel):
    title: str | None = None


class _ChatResponse(StreamingResponse):
    async def stream_response(self, send) -> None:
        # Starlette's disconnect scope can cancel every subsequent await.
        # Deliver one cancellation to the stream, then wait for its cleanup.
        await _finish_task(
            asyncio.create_task(self._send_and_close(send)), cancel=True,
        )

    async def _send_and_close(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # A failed send leaves the iterator suspended at its last yield.
            # Close it before the request ends so research releases its slot.
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


async def _write_before_unlock(write: Callable, *args) -> None:
    # Cancelling to_thread only abandons its await; the disk write continues.
    # Keep the session lock until that worker finishes, even on disconnect.
    await _finish_task(asyncio.create_task(asyncio.to_thread(write, *args)))


async def _finish_task(worker: asyncio.Task, *, cancel: bool = False) -> None:
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        if cancel:
            worker.cancel()
        with CancelScope(shield=True):
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
        worker.result()
        raise


class AppState:
    """Everything with a lifetime longer than one request."""

    def __init__(self, config: RecollectConfig) -> None:
        self.config = config
        self.embedder = HarnessEmbedder(
            config.embedding_model_path, n_threads=config.embedding_threads
        )
        self.sessions = SessionManager(config, self.embedder)
        self.voice = VoiceService(config)
        # llama.cpp is configured with one model slot. Main turns and the
        # complete OpenCode workflow, including native child agents, queue
        # on this lock rather than competing for the same server context.
        self.model_slot = asyncio.Lock()
        self.generator = Generator(
            GeneratorSettings(
                base_url=config.generator_base_url,
                model=config.generator_model,
                api_key=config.generator_api_key,
                timeout_s=config.generator_timeout_s,
                thinking=config.generator_thinking,
                max_tokens=config.generator_max_tokens,
                temperature=config.generator_temperature,
            ),
            model_slot=self.model_slot,
        )
        # Outbound web traffic for the subagent. Deliberately a
        # separate client from the generator's: different destination,
        # different timeout posture, and it must never inherit the generator
        # base URL. web_fetch validates and follows each redirect itself.
        self.web_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": "recollect-research/1.0 (local research agent)"
            },
        )
        # One globally shared sandbox, spawned lazily. Every call gets a
        # fresh OpenCode conversation and scrubbed scratch directory.
        self.sandboxes = SandboxManager(config, model_slot=self.model_slot)
        self.embedder_health: dict = {}
        # A session is an append-only log with a turn counter; two turns
        # racing on one session would interleave episodes and corrupt the
        # ordering the recency window depends on.
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, session_id: str) -> asyncio.Lock:
        return self._locks[session_id]


def create_app(config: RecollectConfig | None = None) -> FastAPI:
    config = config or RecollectConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = AppState(config)
        app.state.recollect = state
        # Pay the ~750ms model load and prove the embedder's identity now,
        # rather than making the first user wait and then fail.
        state.embedder_health = await asyncio.to_thread(state.embedder.warm_up)
        if config.subagent_enabled and config.subagent_backend == "opencode":
            await state.sandboxes.start_reaper()
        try:
            yield
        finally:
            await state.sandboxes.close_all()
            await state.generator.aclose()
            await state.web_client.aclose()

    app = FastAPI(
        title="Recollect",
        version=__version__,
        description=(
            "A harness for episodic conversational memory, instrumented so "
            "every retrieval decision is visible."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def state() -> AppState:
        return app.state.recollect

    install_voice_routes(app, state)

    # -- inspector API -----------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        current = state()
        return {
            "ok": True,
            "version": __version__,
            "library_version": LIBRARY_VERSION,
            "embedder": {
                **current.embedder_health,
                **current.embedder.stats,
            },
            "generator": await current.generator.health(),
            "budget_chars": current.config.budget_chars,
            "episodic_config": json.loads(current.config.episodic.to_json()),
        }

    @app.get("/api/sessions")
    async def list_sessions() -> list[SessionInfo]:
        return state().sessions.list_sessions()

    @app.post("/api/sessions")
    async def create_session(body: CreateSession = Body(default=CreateSession())):
        return state().sessions.create_session(body.title)

    @app.get("/api/sessions/{session_id}/turns")
    async def list_turns(session_id: str) -> list[TurnSummary]:
        return state().sessions.list_turns(session_id)

    @app.get("/api/turns/{turn_id}")
    async def get_turn(turn_id: str) -> TurnTrace:
        trace = state().sessions.find_trace(turn_id)
        if trace is None:
            raise HTTPException(404, f"No such turn: {turn_id}")
        return trace

    @app.get("/api/sessions/{session_id}/episodes/{episode_id}")
    async def get_episode(session_id: str, episode_id: str) -> dict:
        episode = await asyncio.to_thread(
            state().sessions.episode, session_id, episode_id
        )
        if episode is None:
            raise HTTPException(404, f"No such episode: {episode_id}")
        return episode

    @app.post("/api/chat")
    async def chat(request: ChatRequest) -> StreamingResponse:
        return _ChatResponse(
            _stream_turn(
                state(), request.session_id, request.message,
                input_mode=request.input_mode,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # -- OpenAI compatibility ----------------------------------------------

    @app.get("/v1/models")
    async def models() -> dict:
        return {
            "object": "list",
            "data": [
                {
                    "id": "recollect",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "recollect",
                }
            ],
        }

    @app.post("/v1/chat/completions")
    async def completions(body: dict = Body(...)):
        current = state()
        message = _last_user_message(body)
        if message is None:
            raise HTTPException(400, "No user message in the request")
        session_id = await asyncio.to_thread(
            _resolve_session, current, body.get("user")
        )

        if body.get("stream"):
            return _ChatResponse(
                _stream_openai(current, session_id, message),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        text, trace = await _complete(current, session_id, message)
        return {
            "id": f"chatcmpl-{trace.turn_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "recollect",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": (
                        trace.generation.finish_reason if trace.generation else "stop"
                    ),
                }
            ],
            # Non-standard, and harmless to clients that ignore it: enough to
            # find the full trace in the inspector afterwards.
            "recollect": {
                "turn_id": trace.turn_id,
                "session_id": trace.session_id,
                "episodes_delivered": trace.report.episodes_delivered,
                "chars_delivered": trace.report.chars_delivered,
                "trace_trustworthy": trace.verification.trustworthy,
            },
        }

    # -- static UI ----------------------------------------------------------

    dist = Path(__file__).resolve().parent.parent.parent / "ui" / "dist"
    if dist.is_dir():
        app.mount(
            "/assets", StaticFiles(directory=dist / "assets"), name="assets"
        )

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(dist / "index.html")

    return app


# ---------------------------------------------------------------------------
# Turn execution
# ---------------------------------------------------------------------------


def _sse(event: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data)
    return f"event: {event}\ndata: {payload}\n\n"


_SUBAGENT_HANDOFF = (
    "The subagent returned the internal evidence below. Answer the "
    "user's original question now in clear natural language. Synthesize the "
    "findings; do not reproduce the JSON, tool-call syntax, or internal "
    "workflow. Cite useful source URLs. If the result says it is partial, "
    "state that limitation briefly.\n\nINTERNAL SUBAGENT RESULT:\n"
)

_SUBAGENT_REPAIR = (
    "Your previous draft exposed an internal JSON/tool payload. Rewrite it as "
    "a direct natural-language answer to the user's original question. Do not "
    "output JSON, XML tool syntax, or discuss the internal subagent "
    "process."
)


def _looks_like_internal_payload(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    if any(
        marker in lowered
        for marker in ("<tool_call", "</tool_call>", "function=web_")
    ):
        return True
    if "```json" in lowered:
        return True
    if (
        '"summary"' in lowered
        and '"findings"' in lowered
        and '"sources"' in lowered
    ):
        return True
    if '"tool"' in lowered and '"results"' in lowered:
        return True

    candidate = stripped
    if candidate.startswith("```json") and candidate.endswith("```"):
        candidate = candidate[7:-3].strip()
    try:
        return isinstance(json.loads(candidate), (dict, list))
    except (json.JSONDecodeError, TypeError):
        return False


def _subagent_fallback(result_json: str) -> str:
    """Render a safe answer if two model attempts expose internal payloads."""
    try:
        document = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        document = {}

    lines = []
    summary = document.get("summary") if isinstance(document, dict) else None
    if isinstance(summary, str) and summary.strip():
        lines.append(summary.strip())
    else:
        lines.append("The subagent run did not return a complete synthesis.")

    findings = document.get("findings", []) if isinstance(document, dict) else []
    rendered_findings = []
    for finding in (findings if isinstance(findings, list) else []):
        if not isinstance(finding, dict):
            continue
        claim = str(finding.get("claim") or "").strip()
        url = str(finding.get("source_url") or "").strip()
        if claim:
            citation = f" ([source]({url}))" if url else ""
            rendered_findings.append(f"- {claim}{citation}")
    if rendered_findings:
        lines.append("\n".join(rendered_findings))

    sources = document.get("sources", []) if isinstance(document, dict) else []
    source_lines = [
        f"- {url}"
        for url in sources
        if isinstance(url, str) and url.startswith(("http://", "https://"))
    ]
    if source_lines:
        lines.append("Sources:\n" + "\n".join(source_lines))
    return "\n\n".join(lines)


async def _stream_turn(
    state: AppState, session_id: str, message: str,
    *, input_mode: Literal["text", "voice"] = "text",
) -> AsyncIterator[str]:
    """Retrieval first, then tokens. The inspector fills in before the model.

    Retrieval is emitted as its own event the moment it completes rather
    than being held until the reply is done. On a multi-second generation
    that turns the inspector from a post-mortem into a live view of what
    the memory system just decided.

    A turn may be two phases: if the model answers with a ``run_subagent``
    tool call, an ephemeral research loop runs inside this same lock, its
    steps stream as ``subagent_*`` events, and a second generation produces
    the final answer. A delegated turn commits only that final answer; the
    phase-one preamble and the subagent's arc reach no database.
    """
    async with state.lock(session_id):
        started = time.perf_counter()
        try:
            prepared = await asyncio.to_thread(
                state.sessions.prepare_turn, session_id, message
            )
        except Exception as error:  # noqa: BLE001 - surfaced to the client
            yield _sse("error", {"message": str(error)})
            return

        yield _sse("retrieval", prepared.trace.model_dump(mode="json"))

        system_prompt = _turn_system_prompt(
            state.config, prepared.trace.started_at, input_mode,
        )

        def _trace_for() -> GenerationTrace:
            return new_generation_trace(
                settings=state.generator.settings,
                system_prompt=system_prompt,
                context_block=prepared.trace.context_block.payload,
                user_message=message,
            )

        messages = state.generator.build_messages(
            system_prompt=system_prompt,
            context_block=prepared.trace.context_block.payload,
            user_message=message,
        )

        # -- phase 1: the main model, allowed to delegate -------------------
        generation = _trace_for()
        tools = (
            [run_subagent_tool()] if state.config.subagent_enabled else None
        )
        phase_one_chunks = []
        try:
            async for chunk in state.generator.stream(
                messages, trace=generation, tools=tools
            ):
                # A few servers emit raw tool-call markup in `content` while
                # also returning the structured call. Buffer until the call
                # is complete so delegation scaffolding can never flash in
                # the chat before we know this is a two-phase turn.
                phase_one_chunks.append(chunk)
        except GenerationError as error:
            generation.error = str(error)
            yield _sse("error", {"message": str(error)})

        committed_parts: list[str] = []
        subagent_trace: SubagentTrace | None = None

        delegation = (
            None
            if generation.error
            else _find_run_subagent(generation.tool_calls)
        )
        if delegation is None:
            for chunk in phase_one_chunks:
                yield _sse(chunk.kind, {"text": chunk.text})
            committed_parts.append(generation.response_text)

        if delegation is not None:
            call, run_id = delegation
            request = _subagent_request(call.arguments)
            task = request[0] if request else None
            effort = request[1] if request else "focused"
            date_context = research_date_context(
                prepared.trace.started_at.astimezone(UTC).date(), message,
            )
            if task is not None and state.config.subagent_backend == "opencode":
                task = f"{task}\n\n{date_context}"
            subagent_result: SubagentResult | None = None

            yield _sse(
                "subagent_start",
                {
                    "run_id": run_id,
                    "task": task or "(unparseable task)",
                    "effort": effort,
                },
            )

            if task is None:
                # The model produced a tool call it cannot mean. Feed the
                # failure back as the tool result so phase 2 can still give
                # the user a sane answer instead of a dead stream.
                observation = (
                    "The subagent call was malformed (no 'task' field). "
                    "No subagent work was performed."
                )
            else:
                # Both backends yield SubagentStep / SubagentResult, so
                # everything below - events, trace, phase two - is shared.
                if state.config.subagent_backend == "opencode":
                    stream = OpenCodeRunner(
                        state.sandboxes, state.config
                    ).run(session_id, task, effort=effort)
                else:
                    config = SubagentConfig(
                        max_steps=state.config.subagent_max_steps,
                        max_tool_calls=state.config.subagent_max_tool_calls,
                        observation_chars=(
                            state.config.subagent_observation_chars
                        ),
                        max_tokens=state.config.subagent_max_tokens,
                    )
                    stream = run_subagent(
                        state.web_client,
                        state.generator,
                        transfer_task(task, effort),
                        config=config,
                        runtime_context=date_context,
                    )
                try:
                    async with aclosing(stream):
                        async for item in stream:
                            if isinstance(item, SubagentStep):
                                yield _sse(
                                    "subagent_step",
                                    {
                                        "run_id": run_id,
                                        "step": {
                                            "index": item.index,
                                            "tool": item.tool,
                                            "args": item.args,
                                            "observation": item.observation,
                                            "ms": round(item.ms, 1),
                                        },
                                    },
                                )
                            else:
                                subagent_result = item
                                subagent_result.effort = effort
                except Exception as error:  # noqa: BLE001 - phase fails, turn lives
                    observation = f"the subagent failed: {error}"
                    yield _sse(
                        "subagent_done",
                        {
                            "run_id": run_id,
                            "ok": False,
                            "steps": 0,
                            "sources": [],
                            "returned_chars": 0,
                            "error": str(error),
                        },
                    )
                    yield _sse("error", {"message": observation})

            if subagent_result is not None:
                step_count = len(subagent_result.steps)
                observation = subagent_result.result_json
                subagent_trace = SubagentTrace(
                    task=task,
                    effort=subagent_result.effort,
                    backend=subagent_result.backend,
                    isolation=subagent_result.isolation,
                    fresh_context=subagent_result.fresh_context,
                    server_reused=subagent_result.server_reused,
                    status=subagent_result.status,
                    steps=step_count,
                    tools_used=sorted({s.tool for s in subagent_result.steps}),
                    sources=subagent_result.sources,
                    returned_chars=len(observation),
                    total_ms=subagent_result.total_ms,
                    error=subagent_result.error,
                )
                done_payload: dict = {
                    "run_id": run_id,
                    "ok": subagent_result.ok,
                    "steps": step_count,
                    "sources": subagent_result.sources,
                    "returned_chars": len(observation),
                }
                if subagent_result.error:
                    done_payload["error"] = subagent_result.error
                yield _sse("subagent_done", done_payload)

            # -- phase 2: the main model answers from the subagent ----------
            # Replay the delegation exactly as the server expects: the
            # assistant turn with its tool call, then the tool result.
            messages.append(
                {
                    "role": "assistant",
                    # The structured tool call is the complete phase-one
                    # assistant message. Real servers sometimes duplicate it
                    # as malformed content, which must not be replayed into
                    # the final-answer context.
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": "run_subagent",
                                "arguments": call.arguments,
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": _SUBAGENT_HANDOFF + observation,
                }
            )

            final = _trace_for()
            final_chunks = []
            try:
                async for chunk in state.generator.stream(
                    messages, trace=final
                ):
                    final_chunks.append(chunk)
            except GenerationError as error:
                final.error = str(error)
                yield _sse("error", {"message": str(error)})

            if not final.error and _looks_like_internal_payload(
                final.response_text
            ):
                # The local model occasionally echoes a tool result verbatim.
                # Keep that draft out of SSE and storage, then allow exactly
                # one bounded rewrite before using a deterministic fallback.
                messages.append(
                    {"role": "assistant", "content": final.response_text}
                )
                messages.append({"role": "system", "content": _SUBAGENT_REPAIR})
                repaired = _trace_for()
                repair_chunks = []
                try:
                    async for chunk in state.generator.stream(
                        messages, trace=repaired
                    ):
                        repair_chunks.append(chunk)
                except GenerationError as error:
                    repaired.error = str(error)

                if not repaired.error and not _looks_like_internal_payload(
                    repaired.response_text
                ):
                    final = repaired
                    final_chunks = repair_chunks
                else:
                    fallback = _subagent_fallback(observation)
                    final = repaired
                    final.error = None
                    final.finish_reason = "stop"
                    final.response_text = fallback
                    final.response_chars = len(fallback)
                    final_chunks = [StreamChunk("token", fallback)]

            for chunk in final_chunks:
                yield _sse(chunk.kind, {"text": chunk.text})
            committed_parts.append(final.response_text)
            generation = final

        prepared.trace.generation = generation
        if subagent_trace is not None:
            prepared.trace.subagent = subagent_trace
        prepared.trace.total_ms = (time.perf_counter() - started) * 1_000.0

        committed_text = "".join(committed_parts)
        committed = False
        if committed_text and not generation.error:
            await _write_before_unlock(
                state.sessions.commit_turn, prepared, committed_text
            )
            committed = True
        else:
            # Nothing was said, so nothing is remembered - but the trace is
            # still worth keeping, since a failed turn is exactly the kind
            # of thing someone will want to look at.
            await _write_before_unlock(state.sessions.save_trace, prepared.trace)

        yield _sse(
            "done",
            {
                "turn_id": prepared.trace.turn_id,
                "committed": committed,
                "generation": generation.model_dump(mode="json"),
                "total_ms": prepared.trace.total_ms,
            },
        )


def _find_run_subagent(tool_calls) -> tuple[ToolCallTrace, str] | None:
    """The turn's delegation, if the model made one.

    Only ``run_subagent`` is ever offered in phase 1, so at most one
    delegation can exist; first match wins and a fresh run id is minted so
    the UI can key its workspace events without any shared registry.
    """
    for call in tool_calls or []:
        if call.name == "run_subagent":
            return call, uuid.uuid4().hex
    return None


def _subagent_request(
    arguments: str,
) -> tuple[str, SubagentEffort] | None:
    """Parse the task and effort, accepting old task-only calls as focused."""
    try:
        decoded = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    if isinstance(decoded, dict):
        task = decoded.get("task")
        effort = decoded.get("effort", "focused")
        if (
            isinstance(task, str)
            and task.strip()
            and effort in ("focused", "deep")
        ):
            return task.strip(), effort
    return None


async def _complete(
    state: AppState, session_id: str, message: str
) -> tuple[str, TurnTrace]:
    """Non-streaming turn, for OpenAI clients that ask for one."""
    async with state.lock(session_id):
        started = time.perf_counter()
        prepared = await asyncio.to_thread(
            state.sessions.prepare_turn, session_id, message
        )
        system_prompt = _turn_system_prompt(state.config, prepared.trace.started_at)
        generation = new_generation_trace(
            settings=state.generator.settings,
            system_prompt=system_prompt,
            context_block=prepared.trace.context_block.payload,
            user_message=message,
        )
        messages = state.generator.build_messages(
            system_prompt=system_prompt,
            context_block=prepared.trace.context_block.payload,
            user_message=message,
        )
        try:
            async for _ in state.generator.stream(messages, trace=generation):
                pass
        except GenerationError as error:
            generation.error = str(error)

        prepared.trace.generation = generation
        prepared.trace.total_ms = (time.perf_counter() - started) * 1_000.0

        if generation.response_text and not generation.error:
            await _write_before_unlock(
                state.sessions.commit_turn, prepared, generation.response_text
            )
        else:
            await _write_before_unlock(state.sessions.save_trace, prepared.trace)
        return generation.response_text, prepared.trace


async def _stream_openai(
    state: AppState, session_id: str, message: str
) -> AsyncIterator[str]:
    """The OpenAI streaming shape, for clients that speak only that."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created = int(time.time())

    def frame(delta: dict, finish: str | None = None) -> str:
        return "data: " + json.dumps(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": "recollect",
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish}
                ],
            }
        ) + "\n\n"

    async with state.lock(session_id):
        try:
            prepared = await asyncio.to_thread(
                state.sessions.prepare_turn, session_id, message
            )
        except Exception as error:  # noqa: BLE001
            yield frame({"content": f"[recollect] {error}"}, "stop")
            yield "data: [DONE]\n\n"
            return

        system_prompt = _turn_system_prompt(state.config, prepared.trace.started_at)
        generation = new_generation_trace(
            settings=state.generator.settings,
            system_prompt=system_prompt,
            context_block=prepared.trace.context_block.payload,
            user_message=message,
        )
        messages = state.generator.build_messages(
            system_prompt=system_prompt,
            context_block=prepared.trace.context_block.payload,
            user_message=message,
        )

        yield frame({"role": "assistant", "content": ""})
        try:
            async with aclosing(
                state.generator.stream(messages, trace=generation)
            ) as stream:
                async for chunk in stream:
                    if chunk.kind == "token":
                        yield frame({"content": chunk.text})
                    else:
                        yield frame({"reasoning_content": chunk.text})
        except GenerationError as error:
            generation.error = str(error)
            yield frame({"content": f"\n\n[recollect] {error}"})

        prepared.trace.generation = generation
        if generation.response_text and not generation.error:
            await _write_before_unlock(
                state.sessions.commit_turn, prepared, generation.response_text
            )
        else:
            await _write_before_unlock(state.sessions.save_trace, prepared.trace)

        yield frame({}, generation.finish_reason or "stop")
        yield "data: [DONE]\n\n"


def _last_user_message(body: dict) -> str | None:
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # Content-part form: concatenate the text parts.
                return "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
    return None


def _resolve_session(state: AppState, user: str | None) -> str:
    """Map an OpenAI client to a session, creating one on first sight."""
    title = f"openai:{user}" if user else "openai:default"
    for info in state.sessions.list_sessions():
        if info.title == title:
            return info.session_id
    return state.sessions.create_session(title).session_id
