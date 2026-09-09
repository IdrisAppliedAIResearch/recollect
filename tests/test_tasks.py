"""Server-owned task lifetime, bounded handoffs, and execution boundary races."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from recollect import tasks as tasks_module
from recollect.config import RecollectConfig
from recollect.engine.generator import GeneratorSettings
from recollect.engine.sandbox.runner import TaskReport
from recollect.engine.subagent import SubagentResult
from recollect.session import SessionManager
from recollect.task_store import TaskStore
from recollect.tasks import TaskCoordinator


class UpdateGenerator:
    settings = GeneratorSettings(base_url="http://unused", model="fake")

    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def build_messages(self, **kwargs):
        return kwargs

    async def stream(self, messages, *, trace, **kwargs):
        self.calls.append(messages)
        self.started.set()
        await self.release.wait()
        trace.response_text = json.loads(messages["user_message"])["text"]
        yield None


@pytest.fixture
async def environment(tmp_path, fake_embedder, monkeypatch):
    config = RecollectConfig(
        embedding_model_path=tmp_path / "unused.gguf",
        data_dir=tmp_path / "data",
        sandbox_root=tmp_path / "sandbox",
        subagent_backend="opencode",
        subagent_continuous_enabled=True,
    )
    sessions = SessionManager(config, fake_embedder)
    session_id = sessions.create_session("Task tests").session_id
    store = TaskStore(config)
    generator = UpdateGenerator()
    state = SimpleNamespace(
        calls=[], workspace=tmp_path / "workspace", forced=False, coordinator=None
    )
    state.workspace.mkdir()

    async def force():
        state.forced = True
        state.force_release.set()

    state.force_release = asyncio.Event()
    coordinator = TaskCoordinator(
        config, sessions, store, generator, SimpleNamespace(force_stop_active=force)
    )
    state.coordinator = coordinator
    state.store = store
    state.session_id = session_id
    state.generator = generator

    async def default(**kwargs):
        await kwargs["report"](
            TaskReport(
                "accepted",
                "Accepted",
                kwargs["revision"],
                call_id=f"accept-{len(state.calls)}",
            )
        )
        yield SubagentResult("task", "ok", "{}", "Finished")

    state.behavior = default

    class Runner:
        def __init__(self, *args):
            pass

        async def run_continuous(self, session_id, task, **kwargs):
            state.calls.append((session_id, task))
            async for value in state.behavior(**kwargs):
                yield value

    monkeypatch.setattr(tasks_module, "OpenCodeRunner", Runner)
    yield state
    await coordinator.close()


async def submit(state, request="request", **kwargs):
    return await state.coordinator.submit(
        state.session_id,
        request,
        "Compare batteries",
        "Research batteries",
        "focused",
        **kwargs,
    )


async def wait_state(state, task_id, expected):
    async with asyncio.timeout(3):
        while True:
            task = await asyncio.to_thread(state.store.get, state.session_id, task_id)
            if task["state"] == expected:
                return task
            await asyncio.sleep(0.002)


async def wait_idle(state):
    async with asyncio.timeout(3):
        while True:
            async with state.coordinator._mutation:
                if not state.coordinator._active and not state.coordinator._pending:
                    return
            await asyncio.sleep(0.002)


async def test_start_committed_during_disconnect_is_still_admitted(
    environment, monkeypatch
):
    state = environment
    original = state.store.start
    committed = threading.Event()
    release = threading.Event()

    def paused(*args, **kwargs):
        task = original(*args, **kwargs)
        committed.set()
        assert release.wait(2)
        return task

    monkeypatch.setattr(state.store, "start", paused)
    caller = asyncio.create_task(submit(state))
    assert await asyncio.to_thread(committed.wait, 2)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    async with asyncio.timeout(2):
        while state.coordinator._requests:
            await asyncio.sleep(0.002)
    existing = await submit(state)
    assert len(state.coordinator._pending) == 1
    await state.coordinator.start()
    await wait_state(state, existing["task_id"], "completed")
    assert len(state.calls) == 1
    assert len(state.store.list(state.session_id)) == 1


async def test_queue_cancel_releases_capacity_without_orphaned_request(environment):
    state = environment
    queued = [
        await submit(state, str(index)) for index in range(tasks_module.MAX_QUEUED)
    ]
    with pytest.raises(ValueError, match="queue is full"):
        await submit(state, "overflow")
    assert state.store.request(state.session_id, "overflow") is None
    canceled = await state.coordinator.command(
        state.session_id,
        queued[0]["task_id"],
        "cancel",
        "cancel",
    )
    assert canceled["state"] == "canceled"
    replacement = await submit(state, "replacement")
    assert replacement["state"] == "queued"
    assert len(state.coordinator._pending) == tasks_module.MAX_QUEUED
    await state.coordinator.start()
    await wait_idle(state)
    assert len(state.calls) == tasks_module.MAX_QUEUED
    assert (
        state.store.get(state.session_id, queued[0]["task_id"])["state"] == "canceled"
    )


async def test_failed_worker_does_not_starve_queue_and_continue_creates_child(
    environment,
):
    state = environment

    async def behavior(**kwargs):
        if len(state.calls) == 1:
            raise RuntimeError("search transport failed")
        await kwargs["report"](
            TaskReport(
                "accepted", "Accepted", 1, call_id=f"accepted-{len(state.calls)}"
            )
        )
        yield SubagentResult("task", "ok", "{}", "Finished")

    state.behavior = behavior
    failed = await submit(state, "failed")
    other = await submit(state, "other")
    await state.coordinator.start()
    await wait_state(state, failed["task_id"], "blocked")
    await wait_state(state, other["task_id"], "completed")
    await wait_idle(state)
    followup = await state.coordinator.command(
        state.session_id,
        failed["task_id"],
        "retry",
        "continue",
    )
    assert followup["parent_task_id"] == failed["task_id"]
    assert followup["task_id"] != failed["task_id"]
    await wait_state(state, followup["task_id"], "completed")
    assert not state.coordinator._worker.done()


async def test_owned_question_steers_same_execution_and_deduplicates_reports(
    environment,
):
    state = environment
    questioned = asyncio.Event()

    async def behavior(**kwargs):
        await kwargs["report"](TaskReport("accepted", "Accepted", 1, call_id="accept"))
        question = TaskReport("question", "A or B?", 1, call_id="question")
        await kwargs["report"](question)
        await kwargs["report"](question)
        questioned.set()
        command = await kwargs["commands"].get()
        assert command.text == "Use B"
        await kwargs["report"](
            TaskReport("accepted", "Accepted", command.revision, call_id="accepted-two")
        )
        finding = TaskReport("finding", "B meets the request", 2, call_id="finding")
        await kwargs["report"](finding)
        await kwargs["report"](finding)
        yield SubagentResult("task", "ok", "{}", "B")

    state.behavior = behavior
    task = await submit(state)
    await state.coordinator.start()
    await asyncio.wait_for(questioned.wait(), 2)
    steered = await state.coordinator.command(
        state.session_id,
        task["task_id"],
        "steer",
        "steer",
        "Use B",
        reply_to="question",
    )
    assert steered["task_id"] == task["task_id"]
    final = await wait_state(state, task["task_id"], "completed")
    assert (
        final["revision"] == final["accepted_revision"] == final["result_revision"] == 2
    )
    assert final["findings"] == ["B meets the request"]
    assert len(state.calls) == 1


async def test_late_steering_after_native_completion_creates_one_saved_followup(
    environment,
):
    state = environment
    final_boundary = asyncio.Event()
    finish = asyncio.Event()
    restored = []

    async def behavior(**kwargs):
        await kwargs["restore_workspace"](state.workspace)
        if len(state.calls) > 1:
            restored.append((state.workspace / "report.txt").read_text())
        await kwargs["report"](
            TaskReport(
                "accepted", "Accepted", 1, call_id=f"accepted-{len(state.calls)}"
            )
        )
        if len(state.calls) == 1:
            (state.workspace / "report.txt").write_text("A", encoding="utf-8")
            final_boundary.set()
            await finish.wait()
        await kwargs["save_workspace"](state.workspace)
        (state.workspace / "report.txt").unlink()
        yield SubagentResult("task", "ok", "{}", "Completed earlier work")

    state.behavior = behavior
    task = await submit(state)
    await state.coordinator.start()
    await asyncio.wait_for(final_boundary.wait(), 2)
    await state.coordinator.command(
        state.session_id,
        task["task_id"],
        "late-change",
        "steer",
        "Add warranty",
    )
    finish.set()
    await wait_idle(state)
    old = state.store.get(state.session_id, task["task_id"])
    assert old["revision"] == 2 and old["result_revision"] == 1
    child = state.store.get(state.session_id, old["checkpoint"]["followup_task_id"])
    assert child["parent_task_id"] == old["task_id"]
    assert child["state"] == "completed"
    assert "Add warranty" in state.calls[1][1]
    assert restored == ["A"]
    retried = await state.coordinator.command(
        state.session_id,
        task["task_id"],
        "late-change",
        "steer",
        "Add warranty",
    )
    assert retried["task_id"] == child["task_id"]
    assert len(state.store.list(state.session_id)) == 2


async def test_cancel_wins_when_worker_suppresses_cancellation_and_returns_result(
    environment,
):
    state = environment
    running = asyncio.Event()

    async def behavior(**kwargs):
        await kwargs["report"](TaskReport("accepted", "Accepted", 1, call_id="accept"))
        await kwargs["report"](
            TaskReport("finding", "Saved finding", 1, call_id="found")
        )
        running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await kwargs["report"](TaskReport("finding", "Too late", 1, call_id="late"))
        yield SubagentResult("task", "ok", "{}", "Late completion")

    state.behavior = behavior
    task = await submit(state)
    await state.coordinator.start()
    await asyncio.wait_for(running.wait(), 2)
    await state.coordinator.command(
        state.session_id, task["task_id"], "cancel", "cancel"
    )
    canceled = await wait_state(state, task["task_id"], "canceled")
    assert canceled["findings"] == ["Saved finding"]
    assert canceled["result"] == ""
    assert state.store.get_message(state.session_id, "late") is None


async def test_context_keeps_delivered_ordering_without_episodes(environment):
    state = environment
    task = await submit(state)
    for index in range(5):
        state.store.message(
            state.session_id,
            task["task_id"],
            f"turn-{index}",
            "main",
            "conversation",
            {
                "text": f"Options A and B, update {index}",
                "user_message": "What have you found?",
                "turn_id": str(index),
            },
        )
    state.store.update(state.session_id, task["task_id"], progress="Newer findings C/D")
    context, ids = await state.coordinator.context(state.session_id)
    assert len(context) <= 12_000
    data = json.loads(context)
    assert data["recent_updates"][-1]["text"] == "Options A and B, update 4"
    assert ids == [task["task_id"]]
    assert state.store.notifications(state.session_id) == []
    assert not state.store.config.store_path(state.session_id).exists()
    assert not state.store.config.session_file(state.session_id, "turns.jsonl").exists()


async def test_progress_coalesces_latest_and_quiet_race_suppresses_spoken_update(
    environment,
):
    state = environment
    task = await submit(state)
    key = (state.session_id, task["task_id"])
    coordinator = state.coordinator
    coordinator._queue_update(key, "first", "finding", "First finding", 1)
    await coordinator._announce_one(key, coordinator._notifications[key])
    coordinator._queue_update(key, "second", "finding", "Second finding", 1)
    coordinator._queue_update(key, "third", "finding", "Third finding", 1)
    await coordinator._announce_one(key, coordinator._notifications[key])
    assert len(state.store.notifications(state.session_id)) == 1
    coordinator._last_notification[key] -= tasks_module.UPDATE_INTERVAL
    await coordinator._announce_one(key, coordinator._notifications[key])
    assert [item["text"] for item in state.store.notifications(state.session_id)] == [
        "First finding",
        "Third finding",
    ]
    coordinator._last_notification[key] = (
        time.monotonic() - tasks_module.UPDATE_INTERVAL
    )
    state.generator.started.clear()
    state.generator.release.clear()
    coordinator._queue_update(key, "fourth", "finding", "Fourth finding", 1)
    delivery = asyncio.create_task(
        coordinator._announce_one(key, coordinator._notifications[key])
    )
    await asyncio.wait_for(state.generator.started.wait(), 2)
    await coordinator.command(*key, "quiet", "quiet", quiet=True)
    state.generator.release.set()
    await delivery
    assert len(state.store.notifications(state.session_id)) == 2


async def test_notifications_retry_after_window_without_another_report(
    environment, monkeypatch
):
    state = environment
    monkeypatch.setattr(tasks_module, "UPDATE_INTERVAL", 0.03)
    task = await submit(state)
    key = (state.session_id, task["task_id"])
    state.coordinator._last_notification[key] = time.monotonic()
    state.coordinator._queue_update(key, "finding", "finding", "Queued update", 1)
    worker = asyncio.create_task(state.coordinator._announce())
    state.coordinator._notification_worker = worker
    async with asyncio.timeout(2):
        while not state.store.notifications(state.session_id):
            await asyncio.sleep(0.002)
    assert state.store.notifications(state.session_id)[0]["text"] == "Queued update"


async def test_shutdown_forces_owned_sandbox_after_bounded_cancel(
    environment, monkeypatch
):
    state = environment
    monkeypatch.setattr(tasks_module, "SHUTDOWN_SECONDS", 0.02)
    running = asyncio.Event()

    async def behavior(**kwargs):
        running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await state.force_release.wait()
        yield SubagentResult("task", "partial", "{}", "Interrupted")

    state.behavior = behavior
    task = await submit(state)
    await state.coordinator.start()
    await asyncio.wait_for(running.wait(), 2)
    await asyncio.wait_for(state.coordinator.close(), 2)
    assert state.forced
    assert state.store.get(state.session_id, task["task_id"])["state"] == "interrupted"
    with pytest.raises(ValueError, match="not available"):
        await submit(state, "after-close")


async def test_execution_date_is_not_part_of_display_objective(environment):
    state = environment
    task = await submit(state)
    await state.coordinator.start()
    await wait_state(state, task["task_id"], "completed")
    assert (
        state.store.get(state.session_id, task["task_id"])["objective"]
        == "Compare batteries"
    )
    assert "Current date (UTC):" in state.calls[0][1]
    assert "Original user request:" in state.calls[0][1]


async def test_context_prioritizes_owner_and_includes_panel_steering(environment):
    state = environment
    running = asyncio.Event()

    async def behavior(**kwargs):
        running.set()
        await asyncio.Event().wait()
        yield

    state.behavior = behavior
    owner = await submit(state, "owner")
    await state.coordinator.start()
    await asyncio.wait_for(running.wait(), 2)
    for index in range(tasks_module.MAX_QUEUED):
        await submit(state, f"queued-{index}")
    for index in range(3):
        old = state.store.start(
            state.session_id, f"old-{index}", "Old task", "Old", "focused"
        )
        state.store.update(state.session_id, old["task_id"], state="completed")
    await state.coordinator.command(
        state.session_id,
        owner["task_id"],
        "steer-owner",
        "steer",
        "Only include installed systems",
    )
    context, ids = await state.coordinator.context(state.session_id)
    assert ids[0] == owner["task_id"]
    assert "Only include installed systems" in context
    assert len(context) <= 12_000


async def test_delete_and_reset_refuse_owned_blocked_question(environment):
    state = environment
    question = asyncio.Event()

    async def behavior(**kwargs):
        await kwargs["report"](
            TaskReport("question", "Which option?", 1, call_id="question")
        )
        question.set()
        await asyncio.Event().wait()
        yield

    state.behavior = behavior
    task = await submit(state)
    await state.coordinator.start()
    await asyncio.wait_for(question.wait(), 2)
    with pytest.raises(ValueError, match="Cancel"):
        await state.coordinator.delete(state.session_id, task["task_id"])
    with pytest.raises(ValueError, match="Cancel"):
        await state.coordinator.reset(state.session_id)
    assert state.store.get(state.session_id, task["task_id"])["state"] == "blocked"
    await state.coordinator.command(
        state.session_id, task["task_id"], "cancel", "cancel"
    )
    await wait_state(state, task["task_id"], "canceled")
    await wait_idle(state)
    await state.coordinator.delete(state.session_id, task["task_id"])
    assert state.store.list(state.session_id) == []


async def test_notification_keeps_earlier_result_revision(environment):
    state = environment
    task = await submit(state)
    state.store.accept_revision(state.session_id, task["task_id"], 1)
    state.store.steer(state.session_id, task["task_id"], "later", "Use B")
    key = (state.session_id, task["task_id"])
    state.coordinator._queue_update(key, "result", "result", "Earlier result A", 1)
    await state.coordinator._announce_one(key, state.coordinator._notifications[key])
    assert state.store.notifications(state.session_id)[0]["revision"] == 1
    assert state.store.get(state.session_id, task["task_id"])["revision"] == 2


async def test_quiet_command_retry_does_not_revert_newer_preference(environment):
    state = environment
    task = await submit(state)
    arguments = (state.session_id, task["task_id"])
    await state.coordinator.command(*arguments, "quiet-one", "quiet", quiet=True)
    await state.coordinator.command(*arguments, "quiet-two", "quiet", quiet=False)
    retried = await state.coordinator.command(
        *arguments, "quiet-one", "quiet", quiet=True
    )
    assert retried["quiet"] is False


async def test_startup_reconciles_crash_between_result_and_followup(environment):
    state = environment
    task = state.store.start(state.session_id, "old", "Compare", "Compare", "focused")
    state.store.accept_revision(state.session_id, task["task_id"], 1)
    state.store.steer(state.session_id, task["task_id"], "late", "Add warranty")
    state.store.update(
        state.session_id, task["task_id"], state="completed", result_revision=1
    )
    await state.coordinator.start()
    await wait_idle(state)
    old = state.store.get(state.session_id, task["task_id"])
    child = state.store.get(state.session_id, old["checkpoint"]["followup_task_id"])
    assert child["state"] == "completed"
    assert child["parent_task_id"] == task["task_id"]
    assert "Add warranty" in state.calls[0][1]


async def test_native_child_call_ids_are_scoped_and_keep_parent_association(
    environment,
):
    state = environment

    async def behavior(**kwargs):
        await kwargs["report"](
            TaskReport(
                "accepted", "Accepted", 1, native_session_id="parent", call_id="same"
            )
        )
        await kwargs["report"](
            TaskReport(
                "finding", "Child finding", 1, native_session_id="child", call_id="same"
            )
        )
        yield SubagentResult("task", "ok", "{}", "Finished")

    state.behavior = behavior
    task = await submit(state)
    await state.coordinator.start()
    finished = await wait_state(state, task["task_id"], "completed")
    assert finished["backend_session_id"] == "parent"
    assert finished["findings"] == ["Child finding"]
    native = [
        item
        for item in state.store.messages(state.session_id, task["task_id"])
        if item["payload"].get("call_id") == "same"
    ]
    assert len(native) == 2
    assert len({item["message_id"] for item in native}) == 2


async def test_context_fails_explicitly_when_escaped_referents_cannot_fit(environment):
    state = environment
    task = await submit(state)
    state.store.message(
        state.session_id,
        task["task_id"],
        "escaped-update",
        "main",
        "conversation",
        {
            "text": "A or B?" + "\x01" * 2_000,
            "user_message": "\x02" * 1_000,
        },
    )
    state.store.steer(
        state.session_id, task["task_id"], "escaped-steer", "Use B" + "\x03" * 1_000
    )
    with pytest.raises(ValueError, match="too large to include safely"):
        await asyncio.wait_for(state.coordinator.context(state.session_id), 2)
    assert state.store.get_message(state.session_id, "escaped-update")["payload"][
        "text"
    ].startswith("A or B?")


async def test_context_preserves_entire_source_urls(environment):
    state = environment
    task = await submit(state)
    short = "https://example.org/source"
    long = "https://example.org/" + "a" * 600
    state.store.update(state.session_id, task["task_id"], sources=[short, long])
    payload, ids = await state.coordinator.context(state.session_id)
    assert ids == [task["task_id"]]
    assert json.loads(payload)["tasks"][0]["sources"] == [short]
    assert long[:500] not in payload
    assert state.store.get(state.session_id, task["task_id"])["sources"] == [
        short,
        long,
    ]


@pytest.mark.parametrize("crash_after_result", [False, True])
async def test_instruction_replay_reads_beyond_first_mailbox_page(
    environment,
    crash_after_result,
):
    state = environment
    task = await submit(state)
    with state.store._db(state.session_id) as connection:
        for index in range(1_000):
            state.store._message(
                connection,
                task,
                f"status-{index}",
                "main",
                "conversation",
                {"text": "Progress update"},
                1,
            )
    state.store.accept_revision(state.session_id, task["task_id"], 1)
    state.store.steer(
        state.session_id,
        task["task_id"],
        "last-direction",
        "Include installation and warranty",
    )
    if crash_after_result:
        state.store.update(
            state.session_id, task["task_id"], state="completed", result_revision=1
        )
    await state.coordinator.start()
    await wait_idle(state)
    assert "Include installation and warranty" in state.calls[-1][1]
    if crash_after_result:
        original = state.store.get(state.session_id, task["task_id"])
        followed = state.store.get(
            state.session_id, original["checkpoint"]["followup_task_id"]
        )
        assert followed["state"] == "completed"
        assert followed["parent_task_id"] == task["task_id"]
    else:
        finished = state.store.get(state.session_id, task["task_id"])
        assert finished["state"] == "completed"
        assert finished["accepted_revision"] == 2
