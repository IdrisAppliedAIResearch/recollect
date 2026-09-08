"""Disconnects must finish owned cleanup before admitting another turn."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import httpx
import pytest
from starlette.requests import ClientDisconnect

import recollect.api as api
from recollect.config import RecollectConfig
from recollect.engine.generator import (
    GenerationError,
    Generator,
    GeneratorSettings,
    new_generation_trace,
)
from recollect.engine.subagent import SubagentResult, SubagentStep
from recollect.trace import TurnTrace
from tests.test_subagent import _episode_rows, _make_state, _parse_sse


@pytest.fixture
def state(tmp_path):
    config = RecollectConfig(
        embedding_model_path=tmp_path / "unused.gguf",
        data_dir=tmp_path / "var",
        subagent_enabled=False,
    )
    return _make_state({"final": ["First answer.", "Second answer."]}, config)


async def run_turn(path, state, session_id, message):
    if path == "complete":
        return await api._complete(state, session_id, message)
    stream = api._stream_turn if path == "chat" else api._stream_openai
    return [chunk async for chunk in stream(state, session_id, message)]


@pytest.mark.parametrize(("answer", "failed", "committed"), [
    ("A complete answer.", False, True),
    ("", False, False),
    ("An unfinished answer.", True, False),
    ("   ", False, True),
])
async def test_done_reports_the_actual_episode_write(
    state, monkeypatch, answer, failed, committed,
):
    session_id = state.sessions.create_session().session_id
    state.generator.scripts["final"][0] = answer
    generate = state.generator.stream

    async def stream(*args, **kwargs):
        async for chunk in generate(*args, **kwargs):
            yield chunk
        if failed:
            raise GenerationError("Model connection failed.")

    monkeypatch.setattr(state.generator, "stream", stream)
    events = dict(_parse_sse("".join(await run_turn(
        "chat", state, session_id, "A question.",
    ))))

    assert events["done"]["committed"] is committed
    assert state.sessions.get_session(session_id).turn_count == int(committed)
    assert len(_episode_rows(state, session_id)) == int(committed)
    trace = state.sessions.find_trace(events["done"]["turn_id"])
    assert trace.verification.trustworthy
    assert trace.generation.response_text == answer
    assert bool(trace.generation.error) is failed


@pytest.mark.parametrize("recovered", [False, True])
async def test_delegation_committed_flag_tracks_final_answer_not_preamble_or_error(
    state, monkeypatch, recovered,
):
    state.config = replace(
        state.config, subagent_enabled=True, subagent_backend="opencode",
    )
    state.sandboxes = object()
    state.generator.scripts["main"] = [{
        "text": "I will investigate this.",
        "tool_calls": [("run_subagent", '{"task":"Find evidence"}')],
    }]
    answer = "I could not complete the research." if recovered else ""
    state.generator.scripts["final"][0] = answer

    class Runner:
        def __init__(self, *_):
            pass

        async def run(self, session_id, task, **kwargs):
            if recovered:
                raise RuntimeError("Research connection failed.")
            yield SubagentResult(
                task=task, status="ok", result_json='{"summary":"Evidence."}',
                summary="Evidence.", backend="opencode",
            )

    monkeypatch.setattr(api, "OpenCodeRunner", Runner)
    session_id = state.sessions.create_session().session_id
    events = dict(_parse_sse("".join(await run_turn(
        "chat", state, session_id, "Research this.",
    ))))

    assert events["done"]["committed"] is recovered
    assert events["done"]["generation"]["response_text"] == answer
    assert ("error" in events) is recovered
    assert state.sessions.get_session(session_id).turn_count == int(recovered)
    rows = _episode_rows(state, session_id)
    assert [row["assistant_message"] for row in rows] == (
        [answer] if recovered else []
    )


async def test_done_cannot_claim_commit_before_disk_write_completes(state, monkeypatch):
    session_id = state.sessions.create_session().session_id
    started = threading.Event()
    release = threading.Event()
    write = state.sessions.commit_turn
    chunks = []

    def delayed_write(*args):
        started.set()
        assert release.wait(5), "test did not release the disk write"
        write(*args)

    async def consume():
        async for chunk in api._stream_turn(state, session_id, "A question."):
            chunks.append(chunk)

    monkeypatch.setattr(state.sessions, "commit_turn", delayed_write)
    task = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(started.wait, 3)
        assert "event: done" not in "".join(chunks)
        assert state.sessions.get_session(session_id).turn_count == 0
        release.set()
        await asyncio.wait_for(task, 3)
        assert dict(_parse_sse("".join(chunks)))["done"]["committed"] is True
        assert state.sessions.get_session(session_id).turn_count == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("path", ["chat", "complete", "openai"])
@pytest.mark.parametrize("write_kind", ["commit_turn", "save_trace"])
async def test_cancelled_write_holds_session_lock_until_disk_work_finishes(
    state, monkeypatch, path, write_kind,
):
    session_id = state.sessions.create_session().session_id
    if write_kind == "save_trace":
        state.generator.scripts["final"][0] = ""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    prepare_indexes = []
    write = getattr(state.sessions, write_kind)
    prepare = state.sessions.prepare_turn

    def delayed_write(*args):
        if not started.is_set():
            started.set()
            assert release.wait(5), "test did not release the disk write"
        try:
            return write(*args)
        finally:
            finished.set()

    def observe_prepare(*args):
        prepared = prepare(*args)
        prepare_indexes.append(prepared.trace.turn_index)
        return prepared

    monkeypatch.setattr(state.sessions, write_kind, delayed_write)
    monkeypatch.setattr(state.sessions, "prepare_turn", observe_prepare)
    first = asyncio.create_task(run_turn(path, state, session_id, "First question."))
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 3)
        first.cancel()
        await asyncio.sleep(0)
        # Repeated aborts must not abandon the same still-running writer.
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        assert state.lock(session_id).locked()
        assert not finished.is_set()

        second = asyncio.create_task(
            run_turn(path, state, session_id, "Second question."),
        )
        await asyncio.sleep(0)
        assert prepare_indexes == [0]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 3)
        await asyncio.wait_for(second, 3)
    finally:
        release.set()
        await asyncio.gather(
            first, *([second] if second else []), return_exceptions=True,
        )

    assert finished.is_set()
    assert not state.lock(session_id).locked()
    assert prepare_indexes == [0, 1 if write_kind == "commit_turn" else 0]
    rows = _episode_rows(state, session_id)
    assert [row["user_message"] for row in rows] == (
        ["First question.", "Second question."]
        if write_kind == "commit_turn" else ["Second question."]
    )
    traces = list(state.config.traces_dir(session_id).glob("*.json"))
    assert len(traces) == 2
    assert all(
        TurnTrace.model_validate_json(path.read_text()).verification.trustworthy
        for path in traces
    )


def research_state(state, monkeypatch, *, waiting=False):
    state.config = replace(
        state.config, subagent_enabled=True, subagent_backend="opencode",
    )
    state.sandboxes = object()
    state.generator.scripts["main"] = [[("run_subagent", '{"task":"Find evidence"}')]]
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_finished = asyncio.Event()
    invocation_started = asyncio.Event()
    retained_streams = []

    class Runner:
        def __init__(self, *_):
            pass

        def run(self, *args, **kwargs):
            async def events():
                try:
                    invocation_started.set()
                    if waiting:
                        await asyncio.Event().wait()
                    yield SubagentStep(1, "web_search", {}, "Evidence found.", 1)
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await cleanup_release.wait()
                    cleanup_finished.set()

            stream = events()
            # Retain it so garbage collection cannot accidentally hide leaks.
            retained_streams.append(stream)
            return stream

    monkeypatch.setattr(api, "OpenCodeRunner", Runner)
    return (
        cleanup_started, cleanup_release, cleanup_finished,
        invocation_started, retained_streams,
    )


@pytest.mark.parametrize("termination", ["aclose", "failed_send", "disconnect"])
async def test_research_abort_finishes_before_outer_stream_releases_session(
    state, monkeypatch, termination,
):
    started, release, finished, _, retained = research_state(state, monkeypatch)
    session_id = state.sessions.create_session().session_id
    stream = api._stream_turn(state, session_id, "Research this.")
    step_sent = asyncio.Event()

    async def send(message):
        if b"event: subagent_step" in message.get("body", b""):
            step_sent.set()
            if termination == "failed_send":
                raise OSError("client went away")

    async def receive():
        await step_sent.wait()
        return {"type": "http.disconnect"}

    if termination == "aclose":
        while "event: subagent_step" not in await anext(stream):
            pass
        close = asyncio.create_task(stream.aclose())
    else:
        response = api._ChatResponse(stream)
        scope = {"type": "http", "asgi": {"spec_version": (
            "2.4" if termination == "failed_send" else "2.3"
        )}}
        close = asyncio.create_task(response(scope, receive, send))
    try:
        await asyncio.wait_for(started.wait(), 3)
        assert not close.done()
        assert not finished.is_set()
        assert state.lock(session_id).locked()
        release.set()
        if termination == "failed_send":
            with pytest.raises(ClientDisconnect):
                await asyncio.wait_for(close, 3)
        else:
            await asyncio.wait_for(close, 3)
    finally:
        release.set()
        await asyncio.gather(close, return_exceptions=True)
        for inner in retained:
            await inner.aclose()
        await stream.aclose()

    assert finished.is_set()
    assert not state.lock(session_id).locked()
    assert _episode_rows(state, session_id) == []
    assert state.sessions.list_turns(session_id) == []


async def test_disconnect_during_pending_research_waits_for_abort_cleanup(
    state, monkeypatch,
):
    started, release, finished, invocation, retained = research_state(
        state, monkeypatch, waiting=True,
    )
    session_id = state.sessions.create_session().session_id
    stream = api._stream_turn(state, session_id, "Research this.")

    async def send(message):
        pass

    async def receive():
        await invocation.wait()
        return {"type": "http.disconnect"}

    response = api._ChatResponse(stream)
    close = asyncio.create_task(response(
        {"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send,
    ))
    try:
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.sleep(0)
        assert not close.done()
        assert state.lock(session_id).locked()
        release.set()
        await asyncio.wait_for(close, 3)
        assert finished.is_set()
        assert not state.lock(session_id).locked()
    finally:
        release.set()
        await asyncio.gather(close, return_exceptions=True)
        for inner in retained:
            await inner.aclose()
        await stream.aclose()


async def test_chat_routes_return_responses_that_own_their_iterators(state):
    app = api.create_app(state.config)
    app.state.recollect = state
    endpoints = {
        route.path: route.endpoint for route in app.routes
        if hasattr(route, "endpoint")
    }
    session_id = state.sessions.create_session().session_id
    response = await endpoints["/api/chat"](api.ChatRequest(
        session_id=session_id, message="Hello", input_mode="voice",
    ))
    assert isinstance(response, api._ChatResponse)
    response = await endpoints["/v1/chat/completions"]({
        "stream": True, "messages": [{"role": "user", "content": "Hello"}],
    })
    assert isinstance(response, api._ChatResponse)


async def test_openai_disconnect_closes_http_stream_before_releasing_model_slot(
    state,
):
    closing = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    slot = asyncio.Lock()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closing.set()
            await release.wait()
            closed.set()

    settings = GeneratorSettings(base_url="http://model.test/v1", model="fake")
    generator = Generator(settings, model_slot=slot)
    await generator.aclose()
    generator._client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Body())),
    )
    state.generator = generator
    session_id = state.sessions.create_session().session_id
    response = api._ChatResponse(api._stream_openai(state, session_id, "Hello"))

    async def send(message):
        if b'"content": "Hello"' in message.get("body", b""):
            raise OSError("client went away")

    close = asyncio.create_task(response.stream_response(send))
    try:
        await asyncio.wait_for(closing.wait(), 3)
        assert not close.done()
        assert slot.locked()
        assert state.lock(session_id).locked()
        release.set()
        with pytest.raises(OSError, match="client went away"):
            await asyncio.wait_for(close, 3)
        assert closed.is_set()
        assert not slot.locked()
        assert not state.lock(session_id).locked()
        assert _episode_rows(state, session_id) == []
    finally:
        release.set()
        await asyncio.gather(close, return_exceptions=True)
        await generator.aclose()


async def test_explicit_generator_close_releases_inner_stream_then_slot():
    slot = asyncio.Lock()
    generator = Generator(
        GeneratorSettings(base_url="http://model.test/v1", model="fake"),
        model_slot=slot,
    )
    retained = []
    closed = []

    def stream(*args, **kwargs):
        async def chunks():
            try:
                yield object()
            finally:
                closed.append(slot.locked())

        inner = chunks()
        retained.append(inner)
        return inner

    generator._stream_unlocked = stream
    trace = new_generation_trace(
        settings=generator.settings, system_prompt="",
        context_block="", user_message="",
    )
    outer = generator.stream([], trace=trace)
    try:
        await anext(outer)
        await outer.aclose()
        assert closed == [True]
        assert not slot.locked()
    finally:
        await outer.aclose()
        for inner in retained:
            await inner.aclose()
        await generator.aclose()
