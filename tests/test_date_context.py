"""Current-date grounding follows one turn into both research backends."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from recollect.api import _VOICE_INSTRUCTIONS, _stream_turn, _turn_system_prompt
from recollect.config import RecollectConfig
from recollect.engine.date_context import current_date_context, research_date_context
from recollect.engine.subagent import RESEARCHER_PROMPT, SubagentResult, transfer_task
from tests.test_subagent import FINAL_JSON, _make_state, _parse_sse
from tests.test_voice_chat import chat_app as chat_app


def test_prompt_date_uses_utc_and_remains_stable_throughout_the_day(tmp_path):
    config = RecollectConfig(embedding_model_path=tmp_path / "unused.gguf")
    early = datetime(2026, 9, 7, 20, tzinfo=timezone(timedelta(hours=-5)))
    late = datetime(2026, 9, 8, 23, 59, 59, tzinfo=UTC)

    first = _turn_system_prompt(config, early, "voice")
    assert first == _turn_system_prompt(config, late, "voice")
    assert first.startswith(config.system_prompt + "\n\n" + _VOICE_INSTRUCTIONS)
    assert "Current date (UTC): 2026-09-08." in first
    assert "Day of week (UTC): Tuesday." in first
    assert "23:59" not in first
    next_day = _turn_system_prompt(config, late + timedelta(seconds=1), "voice")
    assert next_day == (
        first.replace("2026-09-08", "2026-09-09").replace("Tuesday", "Wednesday")
    )


@pytest.mark.parametrize(("day", "weekday"), [
    (date(2024, 2, 28), "Wednesday"),
    (date(2024, 2, 29), "Thursday"),
    (date(2024, 3, 1), "Friday"),
    (date(2025, 12, 31), "Wednesday"),
    (date(2026, 1, 1), "Thursday"),
    (date(2026, 9, 6), "Sunday"),
    (date(2026, 9, 7), "Monday"),
    (date(2026, 9, 8), "Tuesday"),
    (date(2026, 9, 9), "Wednesday"),
])
def test_date_and_weekday_stay_consistent_at_calendar_boundaries(day, weekday):
    context = current_date_context(day)
    assert context.startswith(
        f"Current date (UTC): {day.isoformat()}. Day of week (UTC): {weekday}. "
    )
    delegated = research_date_context(day, "What are today's opening hours?")
    assert delegated.startswith(context)


def test_weekday_follows_utc_date_when_local_year_differs(tmp_path):
    config = RecollectConfig(embedding_model_path=tmp_path / "unused.gguf")
    local = datetime(2026, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=14)))
    context = _turn_system_prompt(config, local, "voice")
    assert "Current date (UTC): 2025-12-31. Day of week (UTC): Wednesday." in context
    assert "Thursday" not in context


@pytest.mark.parametrize("mode", ["text", "voice", "openai", "openai-stream"])
def test_every_chat_route_sends_and_accounts_for_the_current_date(chat_app, mode):
    client, state, payloads = chat_app
    question = "What were Austin rents during 2025?"
    if mode in ("text", "voice"):
        session = state.sessions.create_session()
        response = client.post("/api/chat", json={
            "session_id": session.session_id, "message": question, "input_mode": mode,
        })
    else:
        response = client.post("/v1/chat/completions", json={
            "user": "calendar-test", "stream": mode == "openai-stream",
            "messages": [{"role": "user", "content": question}],
        })
        session = state.sessions.list_sessions()[0]

    assert response.status_code == 200
    messages = payloads[0]["messages"]
    system = messages[0]["content"]
    assert "Current date (UTC): 2026-09-08." in system
    assert "Day of week (UTC): Tuesday." in system
    assert "Honor historical periods explicitly requested by the user." in system
    assert (_VOICE_INSTRUCTIONS in system) == (mode == "voice")
    assert messages[-1]["content"] == question
    summary = state.sessions.list_turns(session.session_id)[0]
    trace = state.sessions.find_trace(summary.turn_id)
    assert trace.verification.trustworthy
    assert trace.generation.system_prompt_chars == len(system)
    assert trace.generation.total_prompt_chars == (
        len(system) + trace.context_block.chars + len(question)
    )


@pytest.mark.parametrize("backend", ["legacy", "opencode"])
@pytest.mark.parametrize("question", [
    "Find current rental prices in Austin.",
    "Find Austin rental prices for the historical year 2025.",
])
async def test_research_receives_same_turn_date_and_original_requested_period(
    tmp_path, monkeypatch, backend, question,
):
    proposed = "Find the latest (2025) Austin rental prices."
    config = RecollectConfig(
        embedding_model_path=tmp_path / "unused.gguf", data_dir=tmp_path / "var",
        subagent_backend=backend,
    )
    state = _make_state({
        "main": [[("run_subagent", json.dumps({
            "task": proposed, "effort": "focused",
        }))]],
        "sub": [FINAL_JSON],
        "final": ["The source identifies its reporting period."],
    }, config)
    state.sandboxes = object()
    prepare = state.sessions.prepare_turn
    captured = []
    delegated = []

    def prepare_turn(*args):
        prepared = prepare(*args)
        prepared.trace.started_at = datetime(2026, 9, 8, 23, 59, 59, tzinfo=UTC)
        return prepared

    state.sessions.prepare_turn = prepare_turn
    stream = state.generator.stream

    async def capture(messages, *, trace, **kwargs):
        captured.append((messages[0]["content"], trace))
        async for chunk in stream(messages, trace=trace, **kwargs):
            yield chunk

    state.generator.stream = capture

    class Runner:
        def __init__(self, *args):
            pass

        async def run(self, session_id, task, *, effort):
            delegated.append(task)
            yield SubagentResult(
                task=task, status="ok", result_json='{"summary":"Dated findings."}',
                summary="Dated findings.", backend="opencode",
            )

    monkeypatch.setattr("recollect.api.OpenCodeRunner", Runner)
    session = state.sessions.create_session()
    raw = "".join([chunk async for chunk in _stream_turn(
        state, session.session_id, question, input_mode="voice",
    )])
    events = dict(_parse_sse(raw))
    trace = state.sessions.find_trace(events["done"]["turn_id"])
    context = research_date_context(date(2026, 9, 8), question)

    if backend == "opencode":
        assert delegated == [proposed + "\n\n" + context]
        assert trace.subagent.task == delegated[0]
        assert events["subagent_start"]["task"] == delegated[0]
    else:
        sub_call = next(call for call in state.generator.calls if call["key"] == "sub")
        assert sub_call["messages"][0]["content"] == (
            RESEARCHER_PROMPT + "\n\n" + context
        )
        assert sub_call["messages"][1]["content"] == transfer_task(proposed, "focused")
        sub_system, sub_trace = next(
            item for item in captured if item[0].startswith(RESEARCHER_PROMPT)
        )
        assert sub_trace.system_prompt_chars == len(sub_system)
        assert sub_trace.total_prompt_chars == (
            len(sub_system) + len(transfer_task(proposed, "focused"))
        )

    main = next(call for call in state.generator.calls if call["key"] == "main")
    final = next(call for call in state.generator.calls if call["key"] == "final")
    assert main["messages"][0] == final["messages"][0]
    assert current_date_context(date(2026, 9, 8)) in main["messages"][0]["content"]
    assert trace.generation.system_prompt_chars == len(final["messages"][0]["content"])
    assert trace.query.text == question
    assert trace.verification.trustworthy
