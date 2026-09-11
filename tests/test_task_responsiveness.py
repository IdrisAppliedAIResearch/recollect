"""Active task supervision remains responsive while inference is occupied."""

import asyncio
import json
import time

import pytest

from recollect import tasks as tasks_module
from recollect.engine.sandbox.runner import TaskReport
from recollect.engine.subagent import SubagentStep
from tests import test_task_chat as chat_tests
from tests import test_tasks as task_tests
from tests.test_subagent import _episode_rows
from tests.test_task_chat import chat

environment = task_tests.environment
make_task_state = chat_tests.make_task_state


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_silent_worker_stays_silent_until_it_reports(environment):
    state = environment
    report_now = asyncio.Event()

    async def working(**kwargs):
        yield SubagentStep(1, "web_fetch", {}, "PRIVATE source text", 1)
        await report_now.wait()
        await kwargs["report"](TaskReport(
            "finding", "The source confirms the launch date.", kwargs["revision"],
            call_id="actual-finding",
        ))
        await asyncio.Event().wait()

    state.behavior = working
    await state.coordinator.start()
    task = await task_tests.submit(state)
    await until(lambda: state.store.get(
        state.session_id, task["task_id"],
    )["checkpoint"].get("activity"))
    await asyncio.sleep(7.2)
    assert not state.store.notifications(state.session_id)
    assert not state.generator.calls
    report_now.set()
    await until(lambda: state.store.notifications(state.session_id))
    notices = state.store.notifications(state.session_id)
    assert [n["text"] for n in notices] == ["The source confirms the launch date."]
    assert notices[0]["kind"] == "finding"
    assert len(state.generator.calls) == 1


@pytest.mark.parametrize("status_message", [
    "Any updates?", "You didn't actually start looking.",
])
async def test_status_and_message_do_not_wait_for_model_or_worker(
    make_task_state, monkeypatch, status_message,
):
    state = make_task_state([])
    received = []
    acknowledge = asyncio.Event()

    class Runner:
        def __init__(self, *_):
            pass

        async def run_continuous(self, session_id, task, **kwargs):
            yield SubagentStep(1, "web_search", {}, "PRIVATE evidence", 1)
            command = await kwargs["commands"].get()
            received.append(command)
            await acknowledge.wait()
            await kwargs["report"](TaskReport(
                "accepted", "I will use official sources only.", command.revision,
                related_message_id=command.message_id,
            ))
            await asyncio.Event().wait()

    monkeypatch.setattr(tasks_module, "OpenCodeRunner", Runner)
    session = state.sessions.create_session().session_id
    await state.tasks.start()
    task = await state.tasks.submit(session, "start", "Research batteries", "Research")
    key = (session, task["task_id"])
    await until(lambda: state.task_store.get(*key)["checkpoint"].get("activity"))
    state.task_store.update(*key, findings=["A verified partial finding."])
    started = time.monotonic()
    status = await asyncio.wait_for(chat(state, session, status_message, "status"), 1)
    assert time.monotonic() - started < 1
    assert "searching for sources" in status["token"]["text"]
    assert "A verified partial finding." in status["token"]["text"]
    message = "Tell the subagent to use official sources only."
    sent = await asyncio.wait_for(chat(state, session, message, "steer"), 1)
    await until(lambda: received)
    assert received[0].text == message
    assert "message is saved" in sent["token"]["text"]
    assert state.task_store.get(*key)["accepted_revision"] < 2
    await chat(state, session, message, "steer")
    assert state.task_store.get(*key)["revision"] == 2
    assert len(state.task_store.list(session)) == 1
    assert not state.generator.calls
    assert not _episode_rows(state, session)
    acknowledge.set()
    await until(lambda: state.task_store.get(*key)["accepted_revision"] == 2)
    await until(lambda: state.task_store.notifications(session), 6)
    assert "acknowledged" in state.task_store.notifications(session)[0]["text"]


async def test_stalled_announcement_falls_back_without_losing_finding(
    environment, monkeypatch,
):
    state = environment
    monkeypatch.setattr(tasks_module, "ANNOUNCEMENT_TIMEOUT", 0.03)
    state.generator.release.clear()
    task = await task_tests.submit(state)
    key = (state.session_id, task["task_id"])
    state.coordinator._queue_update(key, "finding", "finding", "Verified detail.", 1)
    await asyncio.wait_for(state.coordinator._announce_one(
        key, state.coordinator._notifications[key],
    ), 0.5)
    assert state.store.notifications(state.session_id)[0]["text"] == "Verified detail."
    assert not state.coordinator._notifications


async def test_routine_progress_cannot_replace_an_undelivered_finding(environment):
    state = environment
    task = await task_tests.submit(state)
    key = (state.session_id, task["task_id"])
    state.coordinator._queue_update(key, "finding", "finding", "Verified detail.", 1)
    state.coordinator._queue_update(key, "progress", "progress", "Still working.", 1)
    await state.coordinator._announce_one(key, state.coordinator._notifications[key])
    assert state.store.notifications(state.session_id)[0]["text"] == "Verified detail."


async def test_explicit_worker_message_disambiguates_multiple_tasks(make_task_state):
    state = make_task_state([])
    session = state.sessions.create_session().session_id
    for request in ("first", "second"):
        await state.tasks.submit(session, request, "Research " + request, request)
    response = await chat(state, session, "Tell the subagent to use NASA.")
    assert "Which active task" in response["token"]["text"]
    assert all(t["revision"] == 1 for t in state.task_store.list(session))
    assert not state.generator.calls


async def test_heartbeats_keep_substantive_update_in_context(environment):
    state = environment
    task = await task_tests.submit(state)
    key = (state.session_id, task["task_id"])
    state.store.notify(*key, "finding", "Options A and B.", "finding")
    for index in range(5):
        state.store.notify(*key, f"heartbeat-{index}", "Still waiting.")
    context, _ = await state.coordinator.context(state.session_id)
    assert json.loads(context)["recent_updates"][-1]["text"] == "Options A and B."


async def test_announcements_receive_latest_scope(environment):
    state = environment
    task = await task_tests.submit(state)
    key = (state.session_id, task["task_id"])
    state.store.steer(*key, "narrow", "Compare ONLY Apollo 11 and 12.")
    state.coordinator._queue_update(key, "finding", "finding", "Verified dates.", 2)
    await state.coordinator._announce_one(key, state.coordinator._notifications[key])
    evidence = json.loads(state.generator.calls[-1]["user_message"])
    assert evidence["later_instructions"] == ["Compare ONLY Apollo 11 and 12."]


async def test_notification_challenge_uses_actual_record(make_task_state):
    state = make_task_state([])
    session = state.sessions.create_session().session_id
    task = await state.tasks.submit(
        session, 'start', 'Research dates', 'Research dates',
    )
    state.task_store.notify(session, task['task_id'], 'finding', 'Verified Tuesday.',
                            'finding')
    answer = await chat(state, session,
                        'Why did you not say anything when you got a partial answer?')
    assert 'Verified Tuesday.' in answer['token']['text']
    assert "can't confirm" in answer['token']['text']
    assert not answer['done']['committed']
    assert not state.generator.calls
