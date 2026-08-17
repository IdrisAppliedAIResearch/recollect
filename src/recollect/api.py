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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import RecollectConfig
from .engine._internals import LIBRARY_VERSION
from .engine.embedder import HarnessEmbedder
from .engine.generator import (
    GenerationError,
    Generator,
    GeneratorSettings,
    new_generation_trace,
)
from .session import SessionInfo, SessionManager
from .trace import TurnSummary, TurnTrace


class ChatRequest(BaseModel):
    session_id: str
    message: str


class CreateSession(BaseModel):
    title: str | None = None


class AppState:
    """Everything with a lifetime longer than one request."""

    def __init__(self, config: RecollectConfig) -> None:
        self.config = config
        self.embedder = HarnessEmbedder(
            config.embedding_model_path, n_threads=config.embedding_threads
        )
        self.sessions = SessionManager(config, self.embedder)
        self.generator = Generator(
            GeneratorSettings(
                base_url=config.generator_base_url,
                model=config.generator_model,
                api_key=config.generator_api_key,
                timeout_s=config.generator_timeout_s,
                thinking=config.generator_thinking,
                max_tokens=config.generator_max_tokens,
                temperature=config.generator_temperature,
            )
        )
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
        try:
            yield
        finally:
            await state.generator.aclose()

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
        return StreamingResponse(
            _stream_turn(state(), request.session_id, request.message),
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
            return StreamingResponse(
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


async def _stream_turn(
    state: AppState, session_id: str, message: str
) -> AsyncIterator[str]:
    """Retrieval first, then tokens. The inspector fills in before the model.

    Retrieval is emitted as its own event the moment it completes rather
    than being held until the reply is done. On a multi-second generation
    that turns the inspector from a post-mortem into a live view of what
    the memory system just decided.
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

        generation = new_generation_trace(
            settings=state.generator.settings,
            system_prompt=state.config.system_prompt,
            context_block=prepared.trace.context_block.payload,
            user_message=message,
        )
        messages = state.generator.build_messages(
            system_prompt=state.config.system_prompt,
            context_block=prepared.trace.context_block.payload,
            user_message=message,
        )

        try:
            async for chunk in state.generator.stream(messages, trace=generation):
                yield _sse(chunk.kind, {"text": chunk.text})
        except GenerationError as error:
            generation.error = str(error)
            yield _sse("error", {"message": str(error)})

        prepared.trace.generation = generation
        prepared.trace.total_ms = (time.perf_counter() - started) * 1_000.0

        if generation.response_text and not generation.error:
            await asyncio.to_thread(
                state.sessions.commit_turn, prepared, generation.response_text
            )
        else:
            # Nothing was said, so nothing is remembered - but the trace is
            # still worth keeping, since a failed turn is exactly the kind
            # of thing someone will want to look at.
            await asyncio.to_thread(state.sessions.save_trace, prepared.trace)

        yield _sse(
            "done",
            {
                "turn_id": prepared.trace.turn_id,
                "generation": generation.model_dump(mode="json"),
                "total_ms": prepared.trace.total_ms,
            },
        )


async def _complete(
    state: AppState, session_id: str, message: str
) -> tuple[str, TurnTrace]:
    """Non-streaming turn, for OpenAI clients that ask for one."""
    async with state.lock(session_id):
        started = time.perf_counter()
        prepared = await asyncio.to_thread(
            state.sessions.prepare_turn, session_id, message
        )
        generation = new_generation_trace(
            settings=state.generator.settings,
            system_prompt=state.config.system_prompt,
            context_block=prepared.trace.context_block.payload,
            user_message=message,
        )
        messages = state.generator.build_messages(
            system_prompt=state.config.system_prompt,
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
            await asyncio.to_thread(
                state.sessions.commit_turn, prepared, generation.response_text
            )
        else:
            await asyncio.to_thread(state.sessions.save_trace, prepared.trace)
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

        generation = new_generation_trace(
            settings=state.generator.settings,
            system_prompt=state.config.system_prompt,
            context_block=prepared.trace.context_block.payload,
            user_message=message,
        )
        messages = state.generator.build_messages(
            system_prompt=state.config.system_prompt,
            context_block=prepared.trace.context_block.payload,
            user_message=message,
        )

        yield frame({"role": "assistant", "content": ""})
        try:
            async for chunk in state.generator.stream(messages, trace=generation):
                if chunk.kind == "token":
                    yield frame({"content": chunk.text})
                else:
                    yield frame({"reasoning_content": chunk.text})
        except GenerationError as error:
            generation.error = str(error)
            yield frame({"content": f"\n\n[recollect] {error}"})

        prepared.trace.generation = generation
        if generation.response_text and not generation.error:
            await asyncio.to_thread(
                state.sessions.commit_turn, prepared, generation.response_text
            )
        else:
            await asyncio.to_thread(state.sessions.save_trace, prepared.trace)

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
