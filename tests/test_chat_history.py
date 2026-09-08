"""Reloaded conversations retain complete messages, independently of previews."""

import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from recollect.api import _complete, create_app
from recollect.config import RecollectConfig
from recollect.session import SessionManager
from tests.test_subagent import _make_state


@pytest.fixture
async def saved(tmp_path):
    config = RecollectConfig(
        embedding_model_path=tmp_path / "unused.gguf", data_dir=tmp_path / "var",
        subagent_enabled=False,
    )
    user = "Long question café — " * 30 + "\n\nLAST USER LINE"
    answer = "Long answer — £400. " * 40 + "\n\n**LAST ANSWER LINE**"
    state = _make_state({"final": [answer, "Second answer."]}, config)
    session_id = state.sessions.create_session().session_id
    await _complete(state, session_id, user)
    await _complete(state, session_id, "Second question.")
    # A fresh manager mirrors a server/page reload, without an in-memory turn.
    manager = SessionManager(config, state.sessions.embedder)
    return manager, session_id, user, answer


async def test_reload_reads_all_full_messages_without_changing_previews(saved):
    manager, session_id, user, answer = saved
    summaries = manager.list_turns(session_id)
    assert len(summaries[0].query_preview) == 160
    assert len(summaries[0].response_preview) == 160
    history = manager.chat_history(session_id)
    assert [turn.turn_id for turn in history] == [s.turn_id for s in summaries]
    assert [turn.user_message for turn in history] == [user, "Second question."]
    assert [turn.assistant_message for turn in history] == [answer, "Second answer."]
    assert manager.list_turns(session_id) == summaries


async def test_old_trace_messages_remain_readable_without_relaxing_validation(saved):
    manager, session_id, user, answer = saved
    turn_id = manager.list_turns(session_id)[0].turn_id
    path = manager.config.traces_dir(session_id) / f"{turn_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = 1
    document.pop("cc80_detail")
    path.write_text(json.dumps(document), encoding="utf-8")
    assert manager.get_trace(session_id, turn_id) is None
    history = manager.chat_history(session_id)
    assert history[0].user_message == user
    assert history[0].assistant_message == answer


async def test_history_keeps_failed_attempts_in_append_order_with_their_errors(saved):
    manager, session_id, _, answer = saved
    summaries = manager.list_turns(session_id)
    failed = manager.get_trace(session_id, summaries[0].turn_id)
    failed.turn_id = "failed-attempt"
    failed.turn_index = summaries[1].turn_index
    failed.generation.error = "Generation connection failed."
    failed.generation.reasoning_text = "Saved reasoning.\n" * 40
    manager.save_trace(failed)
    empty = failed.model_copy(deep=True)
    empty.turn_id = "empty-attempt"
    empty.generation = None
    manager.save_trace(empty)
    history = manager.chat_history(session_id)
    assert [turn.turn_id for turn in history] == [
        summaries[0].turn_id, summaries[1].turn_id,
        "failed-attempt", "empty-attempt",
    ]
    assert history[2].assistant_message == answer
    assert history[2].error == "Generation connection failed."
    assert history[2].reasoning_text == "Saved reasoning.\n" * 40
    assert history[3].assistant_message == ""


@pytest.mark.parametrize("damage", ["missing", "broken-json", "bad-message-type"])
async def test_unavailable_body_is_explicit_and_other_messages_still_load(
    saved, damage,
):
    manager, session_id, _, _ = saved
    summary = manager.list_turns(session_id)[0]
    path = manager.config.traces_dir(session_id) / f"{summary.turn_id}.json"
    if damage == "missing":
        path.unlink()
    else:
        path.write_text(
            "{" if damage == "broken-json" else '{"query": {"text": []}}',
            encoding="utf-8",
        )
    history = manager.chat_history(session_id)
    assert history[0].assistant_message == summary.response_preview
    assert history[0].error == "Full saved text is unavailable for this turn."
    assert history[1].assistant_message == "Second answer."
    assert history[1].error is None


async def test_history_api_returns_full_text_and_runs_disk_reads_off_event_loop(
    saved, monkeypatch,
):
    manager, session_id, user, answer = saved
    threads = []
    read = manager.chat_history

    def record_thread(session_id):
        threads.append(threading.get_ident())
        return read(session_id)

    monkeypatch.setattr(manager, "chat_history", record_thread)
    app = create_app(manager.config)
    app.state.recollect = SimpleNamespace(sessions=manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.get(f"/api/sessions/{session_id}/history")
        assert response.status_code == 200
        assert response.json()[0]["user_message"] == user
        assert response.json()[0]["assistant_message"] == answer
        assert "candidates" not in response.json()[0]
        missing = await client.get("/api/sessions/missing/history")
        assert missing.status_code == 404
        empty = manager.create_session().session_id
        assert (await client.get(f"/api/sessions/{empty}/history")).json() == []
    assert threads and threading.get_ident() not in threads
