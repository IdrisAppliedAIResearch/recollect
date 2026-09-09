"""A chat reset removes its memory and work without touching other sessions."""

import asyncio
from dataclasses import replace

import pytest

import recollect.tasks as tasks_module
from recollect.engine.subagent import SubagentResult
from tests import test_task_chat
from tests.test_subagent import _episode_rows
from tests.test_task_chat import client_for, seed_task

make_task_state = test_task_chat.make_task_state


def remember(state, sid):
    prepared = state.sessions.prepare_turn(sid, "I teach biology.")
    state.sessions.commit_turn(prepared, "You teach biology.")
    return prepared.trace.turn_id


async def test_reset_erases_history_memory_and_task_context_but_keeps_downloads(
    make_task_state, tmp_path,
):
    state = make_task_state([])
    sid = state.sessions.create_session().session_id
    other = state.sessions.create_session().session_id
    turn = remember(state, sid)
    remember(state, other)
    task = seed_task(state, sid)
    seed_task(state, other)
    state.task_store.config = replace(
        state.config, downloads_dir=tmp_path / "downloads",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.md").write_text("Saved report.", encoding="utf-8")
    state.task_store.export_workspace(sid, task["task_id"], workspace)
    async with client_for(state) as client:
        response = await client.post(f"/api/sessions/{sid}/reset")
        assert response.status_code == 200
        fresh = response.json()
        new_id = fresh["session_id"]
        assert new_id != sid and fresh["turn_count"] == 0
        assert fresh["title"] == "New conversation"
        assert (await client.get(f"/api/sessions/{new_id}/history")).json() == []
        assert (await client.get(f"/api/sessions/{new_id}/tasks")).json()["tasks"] == []
        assert (await client.get(f"/api/sessions/{sid}/history")).status_code == 404
        assert (await client.get(f"/api/turns/{turn}")).status_code == 404
        retry = await client.post(f"/api/sessions/{sid}/reset")
        assert retry.status_code == 404
    assert not state.config.session_dir(sid).exists()
    assert len(state.sessions.chat_history(other)) == 1
    assert len(_episode_rows(state, other)) == 1
    assert len(state.task_store.list(other)) == 1
    assert (tmp_path / "downloads/report.md").read_text() == "Saved report."
    prepared = state.sessions.prepare_turn(new_id, "What do I teach?")
    assert prepared.trace.store.episode_count == 0


async def test_reset_waits_for_worker_cleanup_and_rejects_new_work(
    make_task_state, monkeypatch,
):
    state = make_task_state([])
    sid = state.sessions.create_session().session_id
    remember(state, sid)
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Runner:
        def __init__(self, *_):
            pass

        async def run_continuous(self, session_id, task, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
                yield SubagentResult(task=task, status="ok", result_json="{}")
            finally:
                cleaning.set()
                await release.wait()

    monkeypatch.setattr(tasks_module, "OpenCodeRunner", Runner)
    await state.tasks.start()
    first = await state.tasks.submit(sid, "first", "Research", "Research")
    await asyncio.wait_for(started.wait(), 3)
    await state.tasks.submit(sid, "second", "Queued research", "Queued research")
    async with client_for(state) as client:
        reset = asyncio.create_task(client.post(f"/api/sessions/{sid}/reset"))
        try:
            await asyncio.wait_for(cleaning.wait(), 3)
            assert not reset.done()
            assert state.config.session_dir(sid).is_dir()
            with pytest.raises(ValueError, match="being reset"):
                await state.tasks.submit(sid, "late", "Late work", "Late work")
            with pytest.raises(ValueError, match="being reset"):
                await state.tasks.command(
                    sid, first["task_id"], "steer", "steer", "Change",
                )
            release.set()
            response = await asyncio.wait_for(reset, 3)
            assert response.status_code == 200
            assert not state.config.session_dir(sid).exists()
            assert not any(key[0] == sid for key in state.tasks._pending)
        finally:
            release.set()
            await reset


async def test_reset_waits_for_foreground_turn_lock(make_task_state):
    state = make_task_state([])
    sid = state.sessions.create_session().session_id
    async with client_for(state) as client:
        async with state.lock(sid):
            request = asyncio.create_task(client.post(f"/api/sessions/{sid}/reset"))
            await asyncio.sleep(0.03)
            assert not request.done()
            remember(state, sid)
        response = await asyncio.wait_for(request, 3)
        assert response.status_code == 200
    assert not state.config.session_dir(sid).exists()


async def test_disconnected_reset_keeps_lock_until_reset_finishes(
    make_task_state, monkeypatch,
):
    state = make_task_state([])
    sid = state.sessions.create_session().session_id
    started, release = asyncio.Event(), asyncio.Event()
    original = state.tasks.reset_conversation

    async def paused(session_id):
        started.set()
        await release.wait()
        return await original(session_id)

    monkeypatch.setattr(state.tasks, "reset_conversation", paused)
    async with client_for(state) as client:
        request = asyncio.create_task(client.post(f"/api/sessions/{sid}/reset"))
        await asyncio.wait_for(started.wait(), 3)
        request.cancel()
        await asyncio.sleep(0)
        assert state.lock(sid).locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 3)
    assert not state.lock(sid).locked()
    assert not state.config.session_dir(sid).exists()
    assert len(state.sessions.list_sessions()) == 1


@pytest.mark.parametrize("sid,status", [("missing", 404), ("bad:id", 409)])
async def test_invalid_reset_does_not_create_or_remove_sessions(
    make_task_state, sid, status,
):
    state = make_task_state([])
    existing = state.sessions.create_session().session_id
    async with client_for(state) as client:
        assert (await client.post(f"/api/sessions/{sid}/reset")).status_code == status
    assert [item.session_id for item in state.sessions.list_sessions()] == [existing]


def test_reset_refuses_linked_session_directory(make_task_state, monkeypatch):
    state = make_task_state([])
    sid = state.sessions.create_session().session_id
    remember(state, sid)
    directory = state.config.session_dir(sid).absolute()
    original = type(directory).is_symlink
    monkeypatch.setattr(type(directory), "is_symlink", lambda path:
                        path == directory or original(path))
    with pytest.raises(ValueError, match="linked"):
        state.sessions.reset_session(sid)
    assert state.sessions.get_session(sid).turn_count == 1
