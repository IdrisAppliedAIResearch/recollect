"""Voice changes response style while preserving the verified turn path."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from recollect.api import _VOICE_INSTRUCTIONS, AppState, create_app
from recollect.config import RecollectConfig
from recollect.engine.date_context import current_date_context
from recollect.engine.generator import Generator, GeneratorSettings
from recollect.session import SessionManager
from tests.conftest import FakeEmbedder


class TurnEmbedder(FakeEmbedder):
    last_cache_hit = False
    last_latency_ms = 0.0


@pytest.fixture
def chat_app(tmp_path):
    config = RecollectConfig(
        embedding_model_path=tmp_path / "unused.gguf",
        data_dir=tmp_path / "var", subagent_enabled=False,
        system_prompt="Use this configured memory instruction unchanged.",
        generator_max_tokens=4096,
    )
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        chunks = [
            {"choices": [{"delta": {"content": state.model_response}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        return httpx.Response(200, content="".join(
            f"data: {json.dumps(chunk)}\n\n" for chunk in chunks
        ) + "data: [DONE]\n\n")

    state = AppState.__new__(AppState)
    state.model_response = "A complete spoken answer."
    state.config = config
    state.sessions = SessionManager(config, TurnEmbedder())
    state.turn_date = datetime(2026, 9, 8, 16, tzinfo=UTC)
    prepare = state.sessions.prepare_turn

    def prepare_turn(*args):
        prepared = prepare(*args)
        prepared.trace.started_at = state.turn_date
        return prepared

    state.sessions.prepare_turn = prepare_turn
    state._locks = defaultdict(asyncio.Lock)
    state.generator = Generator(GeneratorSettings(
        base_url="http://model.test/v1", model="fake",
        max_tokens=config.generator_max_tokens,
    ))
    asyncio.run(state.generator.aclose())
    state.generator._client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(respond),
    )
    app = create_app(config)
    app.state.recollect = state
    client = TestClient(app)
    try:
        yield client, state, payloads
    finally:
        client.close()
        asyncio.run(state.generator.aclose())


def send(client, session_id, message, **kwargs):
    response = client.post("/api/chat", json={
        "session_id": session_id, "message": message, **kwargs,
    })
    assert response.status_code == 200
    events = {}
    for block in response.text.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        events[name.removeprefix("event: ")] = json.loads(data.removeprefix("data: "))
    assert "error" not in events
    return events


def test_voice_guidance_is_before_memory_and_accounted_for_in_persisted_trace(chat_app):
    client, state, payloads = chat_app
    session = state.sessions.create_session()

    events = send(
        client, session.session_id, "What is the monthly price?", input_mode="voice",
    )

    prompt = payloads[0]["messages"]
    effective_system = (
        state.config.system_prompt + "\n\n" + _VOICE_INSTRUCTIONS
        + "\n\n" + current_date_context(state.turn_date.date())
    )
    assert prompt[0] == {"role": "system", "content": effective_system}
    assert prompt[1]["content"] == (
        "Your memory of this conversation so far:\n\n"
        + events["retrieval"]["context_block"]["payload"]
    )
    assert prompt[-1] == {"role": "user", "content": "What is the monthly price?"}
    trace = state.sessions.find_trace(events["done"]["turn_id"])
    assert trace.verification.trustworthy
    assert trace.generation.system_prompt_chars == len(effective_system)
    assert trace.generation.total_prompt_chars == (
        len(effective_system) + trace.context_block.chars + len(trace.query.text)
    )
    assert trace.generation.response_text == "A complete spoken answer."
    assert payloads[0]["max_tokens"] == state.config.generator_max_tokens


def test_typed_chat_default_and_explicit_text_keep_original_prompt_unchanged(chat_app):
    client, state, payloads = chat_app
    expected_prompt = (
        state.config.system_prompt + "\n\n"
        + current_date_context(state.turn_date.date())
    )
    for kwargs in ({}, {"input_mode": "text"}):
        session = state.sessions.create_session()
        events = send(client, session.session_id, "Show a detailed table.", **kwargs)
        assert events["done"]["generation"]["system_prompt_chars"] == len(
            expected_prompt,
        )

    assert payloads[0] == payloads[1]
    assert payloads[0]["messages"][0]["content"] == expected_prompt
    assert _VOICE_INSTRUCTIONS not in json.dumps(payloads[0])


def test_explicit_voice_detail_request_preserves_full_answer_and_generation_budget(
    chat_app,
):
    client, state, payloads = chat_app
    session = state.sessions.create_session()
    state.model_response = "A necessary detailed explanation. " * 20
    request = "Please explain this fully and walk me through all the steps."

    events = send(client, session.session_id, request, input_mode="voice")

    assert payloads[0]["messages"][-1]["content"] == request
    assert payloads[0]["max_tokens"] == state.config.generator_max_tokens
    assert events["done"]["generation"]["response_text"] == state.model_response
    trace = state.sessions.find_trace(events["done"]["turn_id"])
    assert trace.generation.response_text == state.model_response
    assert trace.generation.response_chars == len(state.model_response)


def test_voice_prefix_is_stable_across_followups_and_typed_turn_does_not_inherit_it(
    chat_app,
):
    client, state, payloads = chat_app
    session = state.sessions.create_session()
    first = send(
        client, session.session_id, "Remember the blue door.", input_mode="voice",
    )
    second = send(client, session.session_id, "Which door was it?", input_mode="voice")
    send(client, session.session_id, "Now show a detailed table.")

    assert payloads[0]["messages"][0] == payloads[1]["messages"][0]
    assert payloads[0]["messages"][1] != payloads[1]["messages"][1]
    assert first["retrieval"]["context_block"]["payload"] != (
        second["retrieval"]["context_block"]["payload"]
    )
    assert payloads[2]["messages"][0]["content"] == (
        state.config.system_prompt + "\n\n"
        + current_date_context(state.turn_date.date())
    )
    assert _VOICE_INSTRUCTIONS not in json.dumps(payloads[2])


@pytest.mark.parametrize("input_mode", ["phone", True, None])
def test_invalid_input_mode_is_refused_before_generation(chat_app, input_mode):
    client, state, payloads = chat_app
    session = state.sessions.create_session()

    response = client.post("/api/chat", json={
        "session_id": session.session_id, "message": "Hello", "input_mode": input_mode,
    })

    assert response.status_code == 422
    assert payloads == []
    assert state.sessions.list_turns(session.session_id) == []
