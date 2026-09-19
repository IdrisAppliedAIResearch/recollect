"""Continuous conversation keeps owned work and task evidence out of episodes."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

import recollect.api as api
import recollect.tasks as tasks_module
from recollect.config import RecollectConfig
from recollect.engine.generator import GenerationError
from recollect.engine.subagent import SubagentResult, SubagentStep
from recollect.task_chat import _task_handoff
from recollect.task_store import TaskStore
from recollect.tasks import TaskCoordinator
from tests.test_subagent import _episode_rows, _make_state, _parse_sse


@pytest.fixture
async def make_task_state(tmp_path):
    states = []

    def create(main, final=()):
        config = RecollectConfig(
            embedding_model_path=tmp_path / "unused.gguf",
            data_dir=tmp_path / f"state-{len(states)}",
            sandbox_root=tmp_path / f"scratch-{len(states)}",
            subagent_enabled=True,
            subagent_backend="opencode",
            subagent_continuous_enabled=True,
        )
        state = _make_state({"main": main, "final": list(final)}, config)
        state.generator.key_fn = lambda tools: (
            "main" if tools and len(tools) > 1 else "final"
        )
        state.sandboxes = SimpleNamespace()
        state.task_store = TaskStore(config)
        state.tasks = TaskCoordinator(
            config, state.sessions, state.task_store, state.generator,
            state.sandboxes,
        )
        states.append(state)
        return state

    yield create
    for state in states:
        await state.tasks.close()


def tool(name, **arguments):
    return [(name, json.dumps(arguments))]


async def chat(state, session_id, message="Research this.", request_id="request-1"):
    events = [chunk async for chunk in api._stream_turn(
        state, session_id, message, request_id=request_id,
    )]
    return dict(_parse_sse("".join(events)))


def seed_task(state, session_id, request_id="seed"):
    return state.task_store.start(
        session_id, request_id, "Compare the warranties.", "Research warranties.",
        "focused",
    )


def client_for(state):
    app = api.create_app(state.config, serve_ui=False)
    app.state.recollect = state
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )


async def test_acknowledgment_and_unrelated_chat_do_not_wait_for_owned_worker(
    make_task_state, monkeypatch,
):
    state = make_task_state([
        tool("run_subagent", task="Compare warranties", effort="focused"),
        "The unrelated question can be answered now.",
    ], ["I have queued the warranty research."])
    started = asyncio.Event()
    stopped = asyncio.Event()

    class Runner:
        def __init__(self, *_):
            pass

        async def run_continuous(self, session_id, task, **kwargs):
            started.set()
            try:
                yield SubagentStep(1, "web_fetch", {}, "PRIVATE RAW TOOL OUTPUT", 1)
                await asyncio.Event().wait()
                yield SubagentResult(task=task, status="ok", result_json="{}")
            finally:
                stopped.set()

    monkeypatch.setattr(tasks_module, "OpenCodeRunner", Runner)
    session_id = state.sessions.create_session().session_id
    await state.tasks.start()
    first = await asyncio.wait_for(chat(state, session_id), 3)
    await asyncio.wait_for(started.wait(), 3)
    assert first["done"]["committed"] is False
    assert first["done"]["generation"]["response_text"] == (
        "I have queued the warranty research."
    )
    assert not stopped.is_set()
    assert not state.lock(session_id).locked()
    second = await asyncio.wait_for(chat(
        state, session_id, "What is a warranty?", "request-2",
    ), 3)
    assert second["done"]["committed"] is True
    assert not stopped.is_set()
    assert len(state.task_store.list(session_id)) == 1
    rows = _episode_rows(state, session_id)
    assert len(rows) == 1
    assert all("PRIVATE RAW TOOL OUTPUT" not in (
        row["user_message"] + row["assistant_message"]
    ) for row in rows)
    await state.tasks.close()
    assert stopped.is_set()


async def test_retry_uses_the_durable_task_without_duplicating_work_or_episode(
    make_task_state,
):
    state = make_task_state([
        tool("run_subagent", task="Compare warranties", effort="focused"),
    ], ["The research request is saved."])
    session_id = state.sessions.create_session().session_id
    first = await chat(state, session_id)
    task_id = state.task_store.list(session_id)[0]["task_id"]
    calls = len(state.generator.calls)
    retry = await chat(state, session_id)
    assert first["done"]["committed"] is False
    assert retry["done"]["committed"] is False
    assert len(state.generator.calls) == calls
    assert [task["task_id"] for task in state.task_store.list(session_id)] == [task_id]
    assert len(_episode_rows(state, session_id)) == 0
    assert len(state.sessions.chat_history(session_id)) == 2
    assert "existing task" in retry["done"]["generation"]["response_text"]


async def test_disconnect_during_acknowledgment_preserves_task_and_retry_identity(
    make_task_state, monkeypatch,
):
    state = make_task_state([
        tool("run_subagent", task="Compare warranties", effort="focused"),
    ], ["The research request is saved."])
    acknowledgment_started = asyncio.Event()
    worker_started = asyncio.Event()
    worker_stopped = asyncio.Event()
    original = state.generator.stream

    async def paused_acknowledgment(messages, *, trace, tools=None, **kwargs):
        async for chunk in original(messages, trace=trace, tools=tools, **kwargs):
            yield chunk
        if tools and len(tools) == 1:
            acknowledgment_started.set()
            await asyncio.Event().wait()

    class Runner:
        def __init__(self, *_):
            pass

        async def run_continuous(self, session_id, task, **kwargs):
            worker_started.set()
            try:
                await asyncio.Event().wait()
                yield SubagentResult(task=task, status="ok", result_json="{}")
            finally:
                worker_stopped.set()

    monkeypatch.setattr(state.generator, "stream", paused_acknowledgment)
    monkeypatch.setattr(tasks_module, "OpenCodeRunner", Runner)
    session_id = state.sessions.create_session().session_id
    await state.tasks.start()
    pending = asyncio.create_task(chat(state, session_id))
    try:
        await asyncio.wait_for(acknowledgment_started.wait(), 3)
        await asyncio.wait_for(worker_started.wait(), 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        assert not worker_stopped.is_set()
        assert not state.lock(session_id).locked()
        assert _episode_rows(state, session_id) == []
        assert state.sessions.chat_history(session_id) == []
        saved = state.task_store.request(session_id, "request-1")
        assert saved["original_message"] == "Research this."
        calls = len(state.generator.calls)
        retry = await asyncio.wait_for(chat(state, session_id), 3)
        assert retry["done"]["committed"] is False
        assert len(state.generator.calls) == calls
        assert len(state.task_store.list(session_id)) == 1
        assert not worker_stopped.is_set()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_request_id_cannot_silently_change_the_originating_request(
    make_task_state,
):
    state = make_task_state([
        tool("run_subagent", task="Compare warranties", effort="focused"),
    ], ["The research request is saved."])
    session_id = state.sessions.create_session().session_id
    await chat(state, session_id)
    calls = len(state.generator.calls)
    retry = await chat(state, session_id, "A different request.")
    assert "already used" in retry["error"]["message"]
    assert "done" not in retry
    assert len(state.generator.calls) == calls
    assert len(state.task_store.list(session_id)) == 1
    assert len(_episode_rows(state, session_id)) == 0


@pytest.mark.parametrize("status_only", [True, False])
async def test_status_is_read_without_redelegation_and_ingestion_is_explicit(
    make_task_state, status_only,
):
    state = make_task_state([
        tool("task_control", operation="status", status_only=status_only,
             **({} if status_only else {"memory_reply": "A warranty covers repairs."})),
    ], ["The warranty comparison has one confirmed source."])
    session_id = state.sessions.create_session().session_id
    task = seed_task(state, session_id)
    state.task_store.update(
        session_id, task["task_id"], progress="One source confirmed.",
        findings=["Five years of coverage."], sources=["https://example.com/warranty"],
    )
    message = ("How is the research going?" if status_only else
               "How is the research going? What is a warranty?")
    events = await chat(state, session_id, message)
    assert events["done"]["committed"] is not status_only
    assert len(_episode_rows(state, session_id)) == int(not status_only)
    saved = state.sessions.find_trace(events["done"]["turn_id"])
    assert saved.verification.trustworthy
    assert saved.generation.response_text == (
        "The warranty comparison has one confirmed source."
    )
    assert len(state.sessions.chat_history(session_id)) == 1
    assert state.task_store.snapshot(session_id)["notifications"] == []
    assert len(state.task_store.list(session_id)) == 1
    payload = json.loads(state.generator.calls[-1]["messages"][-1]["content"])
    assert payload["tasks"][0]["task_id"] == task["task_id"]


async def test_supplemental_task_context_is_counted_without_changing_retrieval(
    make_task_state,
):
    state = make_task_state(["The remembered budget remains in effect."])
    session_id = state.sessions.create_session().session_id
    earlier = state.sessions.prepare_turn(session_id, "Remember the budget.")
    state.sessions.commit_turn(earlier, "The budget is 15,000 dollars.")
    task = seed_task(state, session_id)
    state.task_store.update(
        session_id, task["task_id"], findings=["Task evidence outside episodes."],
    )
    message = "What was the budget?"
    expected = state.sessions.prepare_turn(session_id, message)
    events = await chat(state, session_id, message)
    saved = state.sessions.find_trace(events["done"]["turn_id"])
    assert saved.context_block.payload == expected.trace.context_block.payload
    assert saved.verification.payload_identical
    assert saved.verification.report_fields_identical
    messages = state.generator.calls[0]["messages"]
    snapshot = next(item for item in messages if item["content"].startswith(
        "Recollect task snapshot;"
    ))
    assert snapshot["role"] == "user"
    assert "Task evidence outside episodes." in snapshot["content"]
    assert "Task evidence outside episodes." not in saved.context_block.payload
    generation = saved.generation
    assert generation.task_context_chars == len(snapshot["content"])
    assert generation.context_block_chars == len(saved.context_block.payload)
    assert generation.total_prompt_chars == sum(
        len(item["content"]) for item in messages
    )
    assert generation.task_ids == [task["task_id"]]


async def test_new_task_handoff_records_its_identity_and_prompt_cost(make_task_state):
    state = make_task_state([
        tool("run_subagent", task="Compare warranties", effort="focused"),
    ], ["I have saved the research request."])
    session_id = state.sessions.create_session().session_id
    events = await chat(state, session_id)
    task = state.task_store.list(session_id)[0]
    generation = events["done"]["generation"]
    assert task["task_id"] in generation["task_ids"]
    final_messages = state.generator.calls[-1]["messages"]
    content_chars = sum(len(item.get("content", "")) for item in final_messages)
    assert generation["total_prompt_chars"] >= content_chars
    assert any(record["kind"] == "conversation" for record in (
        state.task_store.messages(session_id, task["task_id"])
    ))


def test_handoff_withholds_an_objective_until_the_worker_reports():
    started = {
        "task_id": "task-1", "objective": "No reminder service is connected.",
        "accepted_revision": 0, "progress": "", "findings": [], "sources": [],
        "result": "", "error": None,
    }
    assert json.loads(_task_handoff(started))["objective"] is None
    for field, value in (
        ("accepted_revision", 1), ("progress", "Attempt 1: checks passed."),
        ("findings", ["The seam is absent."]), ("result", "None available."),
        ("error", "The worker stopped."),
    ):
        carried = json.loads(_task_handoff({**started, field: value}))
        assert carried["objective"] == "No reminder service is connected."


async def test_a_just_started_task_is_acknowledged_not_answered(make_task_state):
    """The chat must not answer for a worker that has reported nothing.

    A live turn dispatched the work and then told the user "I don't have a
    way to set recurring reminders" while its worker was booking five of
    them. The follow-up had asked it to answer the user's question; with
    nothing reported, the only honest answer is that work started, and the
    one certainly-wrong answer is that the request cannot be done.
    """
    state = make_task_state([
        tool("run_subagent", task="Book the Friday reminder.", effort="focused"),
    ], ["I've started on that."])
    session_id = state.sessions.create_session().session_id
    await chat(state, session_id, "Set a reminder every Friday at 1:30 pm.")
    system = state.generator.calls[-1]["messages"][0]["content"]
    assert "reported nothing yet" in system
    assert "do not say the request is impossible" in system
    # The prompt that invites answering from findings must not be the one used.
    assert "Include available findings" not in system


async def test_a_task_that_has_reported_is_answered_from_its_findings(
    make_task_state,
):
    """The converse: once evidence exists, the reply may use it."""
    state = make_task_state([
        tool("task_control", operation="status", status_only=True),
    ], ["It found five years of coverage."])
    session_id = state.sessions.create_session().session_id
    task = seed_task(state, session_id)
    state.task_store.update(
        session_id, task["task_id"], state="completed",
        findings=["The warranty covers five years."],
    )
    await chat(state, session_id, "Any news?")
    system = state.generator.calls[-1]["messages"][0]["content"]
    assert "Include available findings" in system
    assert "reported nothing yet" not in system


async def test_a_started_task_cannot_answer_the_user_from_its_own_objective(
    make_task_state,
):
    verdict = "No calendar service is connected, so report it as missing."
    state = make_task_state([
        tool("run_subagent", task=verdict, effort="focused"),
    ], ["I have started checking."])
    session_id = state.sessions.create_session().session_id
    await chat(state, session_id, "Set a reminder every Friday at 1:30 pm.")
    final = state.generator.calls[-1]["messages"]
    assert json.loads(final[-1]["content"])["objective"] is None
    # Neither quoted as the operation's evidence nor echoed back as the call.
    assert not any(verdict in item.get("content", "") for item in final)
    assert state.task_store.list(session_id)[0]["objective"] == verdict


@pytest.mark.parametrize("operation", ["cancel", "steer"])
async def test_ambiguous_task_control_error_cannot_become_a_successful_fallback(
    make_task_state, operation,
):
    state = make_task_state([
        tool("task_control", operation=operation, text="Use the new budget."),
    ], ['{"error":"The task reference is ambiguous."}'])
    session_id = state.sessions.create_session().session_id
    seed_task(state, session_id, "seed-1")
    seed_task(state, session_id, "seed-2")
    events = await chat(state, session_id, "Change that task.")
    tool_reply = json.loads(state.generator.calls[-1]["messages"][-1]["content"])
    assert "ambiguous" in tool_reply["error"]
    response = events["done"]["generation"]["response_text"]
    assert "ambiguous" in response.lower()
    assert "command was handled" not in response.lower()
    assert all(task["state"] == "queued" and task["revision"] == 1
               for task in state.task_store.list(session_id))


@pytest.mark.parametrize("phase", ["initial", "acknowledgment"])
async def test_generator_failure_is_visible_without_losing_an_accepted_task(
    make_task_state, monkeypatch, phase,
):
    state = make_task_state([
        "An unfinished reply." if phase == "initial" else
        tool("run_subagent", task="Compare warranties", effort="focused"),
    ], ["An unfinished acknowledgment."])
    original = state.generator.stream

    async def fail(messages, *, trace, tools=None, **kwargs):
        async for event in original(messages, trace=trace, tools=tools, **kwargs):
            yield event
        if (len(tools or ()) > 1) == (phase == "initial"):
            raise GenerationError("The model connection failed.")

    monkeypatch.setattr(state.generator, "stream", fail)
    session_id = state.sessions.create_session().session_id
    events = await chat(state, session_id)
    assert events["error"]["message"] == "The model connection failed."
    assert events["done"]["committed"] is False
    assert events["done"]["generation"]["error"] == "The model connection failed."
    assert _episode_rows(state, session_id) == []
    assert len(state.task_store.list(session_id)) == int(phase == "acknowledgment")
    saved = state.sessions.find_trace(events["done"]["turn_id"])
    assert saved.generation.error == "The model connection failed."


async def test_task_routes_scope_downloads_and_retain_saved_work_when_disabled(
    make_task_state, tmp_path,
):
    state = make_task_state([])
    session_id = state.sessions.create_session().session_id
    other_session = state.sessions.create_session().session_id
    task = seed_task(state, session_id)
    other_task = seed_task(state, session_id, "another")
    workspace = tmp_path / "artifact-workspace"
    workspace.mkdir()
    content = "option,warranty\nFirst,5 years\n"
    (workspace / "comparison.csv").write_bytes(content.encode("utf-8"))
    artifact = state.task_store.export_workspace(
        session_id, task["task_id"], workspace,
    )[0]
    path = (
        f"/api/sessions/{session_id}/tasks/{task['task_id']}"
        f"/artifacts/{artifact['artifact_id']}"
    )
    async with client_for(state) as client:
        response = await client.get(path)
        assert response.status_code == 200
        assert response.text == content
        assert "attachment" in response.headers["content-disposition"]
        assert "comparison.csv" in response.headers["content-disposition"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "private, no-store"
        wrong_session = await client.get(path.replace(session_id, other_session))
        assert wrong_session.status_code == 404
        assert (await client.get(path.replace(
            task["task_id"], other_task["task_id"],
        ))).status_code == 404
        state.tasks.enabled = False
        snapshot = await client.get(f"/api/sessions/{session_id}/tasks")
        assert snapshot.status_code == 200
        assert snapshot.json()["enabled"] is False
        assert len(snapshot.json()["tasks"]) == 2
        assert snapshot.json()["tasks"][0]["artifacts"][0]["artifact_id"] == (
            artifact["artifact_id"]
        )
        assert (await client.get(path)).status_code == 200
        command = await client.post(
            f"/api/sessions/{session_id}/tasks/{task['task_id']}/messages",
            json={"request_id": "cancel", "operation": "cancel"},
        )
        assert command.status_code == 409


async def test_task_route_reports_command_failure_without_mutating_saved_work(
    make_task_state, monkeypatch,
):
    state = make_task_state([])
    session_id = state.sessions.create_session().session_id
    task = seed_task(state, session_id)

    async def fail(*args):
        raise ValueError("The task mailbox is full.")

    monkeypatch.setattr(state.tasks, "command", fail)
    async with client_for(state) as client:
        response = await client.post(
            f"/api/sessions/{session_id}/tasks/{task['task_id']}/messages",
            json={"request_id": "direction", "operation": "steer", "text": "New"},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "The task mailbox is full."
    assert state.task_store.get(session_id, task["task_id"])["revision"] == 1


@pytest.mark.parametrize("operation", ["status", "cancel"])
async def test_successful_control_of_failed_task_preserves_conversational_reply(
    make_task_state, operation,
):
    reply = ("The task was blocked by an unavailable source." if operation == "status"
             else "I canceled that task. Its saved findings remain available.")
    state = make_task_state([], [reply])
    session_id = state.sessions.create_session().session_id
    task = seed_task(state, session_id)
    state.task_store.update(
        session_id, task["task_id"], state="blocked", error="Source unavailable.",
    )
    state.generator.scripts["main"].append(tool(
        "task_control", operation=operation, task_id=task["task_id"], status_only=True,
    ))
    events = await chat(state, session_id, "What happened to that task?")
    assert events["done"]["generation"]["response_text"] == reply
    assert events["done"]["generation"]["error"] is None
    assert "error" not in events
    result = json.loads(state.generator.calls[-1]["messages"][-1]["content"])
    assert result["error"] == "Source unavailable."
    if operation == "cancel":
        assert state.task_store.get(session_id, task["task_id"])["state"] == "canceled"


@pytest.mark.parametrize("display_only", [False, True])
@pytest.mark.parametrize("failure", [
    ValueError("Task mailbox is full."), KeyError("Task was deleted."),
    OSError("Task database write failed."),
])
async def test_task_projection_failure_cannot_hide_a_saved_conversational_reply(
    make_task_state, monkeypatch, caplog, display_only, failure,
):
    state = make_task_state([
        tool("task_control", operation="status", status_only=display_only,
             **({} if display_only else {"memory_reply": "Warranties cover repairs."})),
    ], ["The warranty comparison is queued."])
    session_id = state.sessions.create_session().session_id
    seed_task(state, session_id)

    def fail_projection(*args, **kwargs):
        raise failure

    monkeypatch.setattr(state.task_store, "message", fail_projection)
    message = ("How is the research going?" if display_only else
               "How is the research going? What is a warranty?")
    events = await chat(state, session_id, message)
    assert events["done"]["committed"] is not display_only
    assert events["token"]["text"] == "The warranty comparison is queued."
    assert events["done"]["generation"]["error"] is None
    assert "error" not in events
    assert len(_episode_rows(state, session_id)) == int(not display_only)
    saved = state.sessions.find_trace(events["done"]["turn_id"])
    assert saved.generation.response_text == events["token"]["text"]
    assert "could not be added" in caplog.text


async def test_task_objective_shown_to_the_user_excludes_execution_instructions(
    make_task_state,
):
    state = make_task_state([
        tool("run_subagent", task="Compare warranties", effort="focused"),
    ], ["I have saved the research request."])
    session_id = state.sessions.create_session().session_id
    await chat(state, session_id)
    task = state.task_store.list(session_id)[0]
    assert task["objective"] == "Compare warranties"
    assert task["original_message"] == "Research this."


async def test_targeted_status_links_its_reply_to_the_named_older_task(
    make_task_state,
):
    state = make_task_state([], ["The first warranty comparison is complete."])
    session_id = state.sessions.create_session().session_id
    tasks = [seed_task(state, session_id, f"seed-{index}") for index in range(4)]
    for task in tasks:
        state.task_store.update(session_id, task["task_id"], state="completed")
    target = tasks[0]["task_id"]
    _, before_ids = await state.tasks.context(session_id)
    assert target not in before_ids
    state.generator.scripts["main"].append(tool(
        "task_control", operation="status", task_id=target, status_only=True,
    ))
    events = await chat(state, session_id, "How did the first comparison finish?")
    assert target in events["done"]["generation"]["task_ids"]
    assert any(item["kind"] == "conversation" for item in (
        state.task_store.messages(session_id, target)
    ))
    assert not any(item["kind"] == "conversation" for item in (
        state.task_store.messages(session_id, tasks[-1]["task_id"])
    ))


async def test_continued_work_links_the_acknowledgment_to_its_new_task(
    make_task_state,
):
    state = make_task_state([], ["The requested revision is queued."])
    session_id = state.sessions.create_session().session_id
    parent = seed_task(state, session_id)
    state.task_store.update(session_id, parent["task_id"], state="completed")
    state.generator.scripts["main"].append(tool(
        "task_control", operation="continue", task_id=parent["task_id"],
        text="Add installation prices.",
    ))
    events = await chat(state, session_id, "Add installation prices.")
    child = state.task_store.request(session_id, "request-1")
    assert child["parent_task_id"] == parent["task_id"]
    assert child["task_id"] in events["done"]["generation"]["task_ids"]
    assert any(item["kind"] == "conversation" for item in (
        state.task_store.messages(session_id, child["task_id"])
    ))
    assert not any(item["kind"] == "conversation" for item in (
        state.task_store.messages(session_id, parent["task_id"])
    ))


@pytest.mark.parametrize("named", [False, True])
async def test_large_status_handoff_is_complete_json_and_preserves_selected_task(
    make_task_state, named,
):
    state = make_task_state([], ["The latest task has saved findings."])
    session_id = state.sessions.create_session().session_id
    tasks = [seed_task(state, session_id, f"seed-{index}") for index in range(8)]
    for task in tasks:
        state.task_store.update(
            session_id, task["task_id"], state="completed",
            progress='"\\' * 2_000,
            findings=[f"Finding {index}: " + '"\\' * 700 for index in range(6)],
            result='"\\' * 3_000,
            sources=[f"https://example.com/{index}/" + "x" * 280
                     for index in range(16)],
        )
    target = tasks[-1]["task_id"]
    arguments = {"operation": "status", "status_only": True}
    if named:
        arguments["task_id"] = target
    state.generator.scripts["main"].append(tool("task_control", **arguments))
    events = await chat(state, session_id, "Summarize the latest task.")
    assert events["done"]["generation"]["error"] is None
    encoded = state.generator.calls[-1]["messages"][-1]["content"]
    handoff = json.loads(encoded)
    assert len(encoded) <= 16_000
    if named:
        assert handoff["task_id"] == target
        assert handoff["state"] == "completed"
        assert handoff["revision"] == 1
        original_sources = state.task_store.get(session_id, target)["sources"]
        assert all(source in original_sources for source in handoff["sources"])
    else:
        assert target in {item["task_id"] for item in handoff["tasks"]}


@pytest.mark.parametrize("body", [
    {"message": "Question", "request_id": "../invalid"},
    {"message": "Question", "request_id": "x" * 129},
])
async def test_chat_route_rejects_invalid_request_ids_before_generation(
    make_task_state, body,
):
    state = make_task_state([])
    session_id = state.sessions.create_session().session_id
    async with client_for(state) as client:
        response = await client.post("/api/chat", json={
            "session_id": session_id, **body,
        })
    assert response.status_code == 422
    assert state.generator.calls == []
    assert state.task_store.list(session_id) == []


@pytest.mark.parametrize("status_only", [True, False])
async def test_routed_direct_reply_separates_status_from_substantive_memory(
    make_task_state, status_only,
):
    reply = ("The comparison is queued." if status_only else
             "The comparison is queued; I will remember your revised budget.")
    message = ("How is the task going?" if status_only else
               "How is the task going? My revised budget is 20,000 dollars.")
    state = make_task_state([
        tool("task_reply", text=reply, status_only=status_only,
             memory_reply=None if status_only else "Your revised budget is $20,000."),
    ])
    state.generator.settings = replace(state.generator.settings, require_tools=True)
    session_id = state.sessions.create_session().session_id
    seed_task(state, session_id)
    events = await chat(state, session_id, message)
    assert events["done"]["committed"] is not status_only
    assert events["token"]["text"] == reply
    assert len(state.generator.calls) == 1
    assert len(_episode_rows(state, session_id)) == int(not status_only)
    saved = state.sessions.find_trace(events["done"]["turn_id"])
    assert saved.verification.trustworthy
    assert saved.generation.tool_calls[0].name == "task_reply"
    assert saved.generation.response_text == reply
    assert saved.generation.task_context_chars > 0
    assert len(state.sessions.chat_history(session_id)) == 1
    assert state.task_store.snapshot(session_id)["notifications"] == []
    offered = state.generator.calls[0]["tools"]
    route = next(item for item in offered if item["function"]["name"] == "task_reply")
    assert route["function"]["parameters"]["required"] == [
        "text", "status_only", "memory_reply",
    ]


@pytest.mark.parametrize("arguments", [
    {"text": "A reply without a memory decision."},
    {"text": "A reply with a string flag.", "status_only": "false"},
    {"text": "A reply with an integer flag.", "status_only": 1},
    {"text": "", "status_only": True},
    {"text": "   ", "status_only": False},
    {"text": None, "status_only": False},
    {"status_only": False},
])
async def test_invalid_reply_route_fails_without_ingesting_the_turn(
    make_task_state, arguments,
):
    state = make_task_state([tool("task_reply", **arguments)])
    state.generator.settings = replace(state.generator.settings, require_tools=True)
    session_id = state.sessions.create_session().session_id
    events = await chat(state, session_id)
    assert events["done"]["committed"] is False
    assert events["done"]["generation"]["error"]
    assert "error" in events
    assert "token" not in events
    assert _episode_rows(state, session_id) == []
    assert len(state.generator.calls) == 1


async def test_required_reply_route_cannot_fall_back_to_unclassified_prose(
    make_task_state,
):
    state = make_task_state(["The task is still running."])
    state.generator.settings = replace(state.generator.settings, require_tools=True)
    session_id = state.sessions.create_session().session_id
    events = await chat(state, session_id, "How is the task going?")
    assert "routed conversational reply" in events["error"]["message"]
    assert events["done"]["committed"] is False
    assert "token" not in events
    assert _episode_rows(state, session_id) == []


@pytest.mark.parametrize("flag", [{}, {"status_only": "false"},
                                  {"status_only": 0}, {"status_only": None}])
async def test_status_control_requires_an_explicit_boolean_memory_decision(
    make_task_state, flag,
):
    state = make_task_state([tool("task_control", operation="status", **flag)])
    session_id = state.sessions.create_session().session_id
    seed_task(state, session_id)
    events = await chat(state, session_id, "How is the task going?")
    assert "memory handling" in events["error"]["message"]
    assert events["done"]["committed"] is False
    assert "token" not in events
    assert _episode_rows(state, session_id) == []
    assert len(state.generator.calls) == 1
    offered = state.generator.calls[0]["tools"]
    route = next(item for item in offered
                 if item["function"]["name"] == "task_control")
    assert route["function"]["parameters"]["required"] == ["operation", "status_only"]


class BuildQuestions:
    """A self-modification service with one pending go/no-go question."""

    def __init__(self, session_id, task_id):
        self.pending = (session_id, task_id)
        self.decisions = []

    def proposal(self, session_id, task_id):
        if (session_id, task_id) == self.pending:
            return {"missing_capability": "send HTTP POST requests",
                    "modification_request": "add an HTTP request tool"}
        return None

    async def decide(self, session_id, task_id, approve, *, announce=True):
        if (session_id, task_id) != self.pending:
            raise ValueError("No capability build is waiting for approval.")
        self.pending = None
        self.decisions.append((session_id, task_id, approve))
        self.announced = announce
        return {"task_id": task_id, "build": "started" if approve else "declined"}


def with_build_question(state, session_id):
    task = seed_task(state, session_id)
    state.task_store.update(session_id, task["task_id"], state="blocked",
                            progress="Want me to build that capability?")
    state.selfmod = BuildQuestions(session_id, task["task_id"])
    state.tasks.build_proposal = state.selfmod.proposal
    return task


def with_connect_question(state, session_id):
    task = seed_task(state, session_id)
    state.task_store.update(session_id, task["task_id"], state="blocked",
                            progress="Want me to connect Google Calendar?")
    state.selfmod = ConnectOffers(session_id, task["task_id"])
    state.tasks.connect_proposal = state.selfmod.connect_proposal
    return task


def with_step_question(state, session_id):
    task = seed_task(state, session_id)
    state.task_store.update(session_id, task["task_id"], state="blocked",
                            progress="I need your input to finish the build.")
    state.selfmod = StepQuestions(session_id, task["task_id"])
    state.tasks.step_proposal = state.selfmod.step_proposal
    return task


class ConnectOffers(BuildQuestions):
    """A self-modification service with one pending connect offer."""

    def connect_proposal(self, session_id, task_id):
        if (session_id, task_id) == self.pending:
            return {"connector": "google_calendar", "name": "Google Calendar",
                    "missing_capability": "calendar write"}
        return None


class StepQuestions:
    """A self-modification service paused on one ask step."""

    def __init__(self, session_id, task_id):
        self.pending = (session_id, task_id)
        self.answers = []

    def step_proposal(self, session_id, task_id):
        if (session_id, task_id) == self.pending:
            return {"kind": "ask", "question": "Which calendar should I use?"}
        return None

    async def answer_step(self, session_id, task_id, answer):
        if (session_id, task_id) != self.pending:
            raise ValueError("No build is waiting for an answer on that task.")
        self.pending = None
        self.answers.append((session_id, task_id, answer))
        return {"task_id": task_id, "answered": True}


@pytest.mark.parametrize("operation,approve",
                         [("connect", True), ("skip_connect", False)])
async def test_the_users_answer_in_chat_decides_the_pending_connection(
    make_task_state, operation, approve,
):
    state = make_task_state([], ["Got it."])
    session_id = state.sessions.create_session().session_id
    task = with_connect_question(state, session_id)
    snapshot = await state.tasks.snapshot(session_id)
    assert snapshot["tasks"][0]["connect_proposal"]["connector"] == (
        "google_calendar")
    context, _ = await state.tasks.context(session_id)
    assert "connect_proposal" in context and "google_calendar" in context
    state.generator.scripts["main"].append(tool(
        "task_control", operation=operation, status_only=True))
    events = await chat(state, session_id, "Yes, connect it." if approve
                        else "No, skip it.")
    assert "error" not in events
    assert state.selfmod.decisions == [(session_id, task["task_id"], approve)]
    assert state.selfmod.announced is False


async def test_connect_without_a_pending_offer_changes_nothing(make_task_state):
    state = make_task_state([
        tool("task_control", operation="connect", status_only=True),
    ], ["Nothing is waiting."])
    session_id = state.sessions.create_session().session_id
    with_connect_question(state, session_id)
    state.selfmod.pending = None
    await chat(state, session_id, "Connect it.")
    handoff = json.loads(state.generator.calls[-1]["messages"][-1]["content"])
    assert "No connection offer is waiting" in handoff["error"]
    assert state.selfmod.decisions == []


@pytest.mark.parametrize("operation,approve", [("build", True), ("skip_build", False)])
async def test_the_users_answer_in_chat_decides_the_pending_build(
    make_task_state, operation, approve,
):
    state = make_task_state([], ["Got it."])
    session_id = state.sessions.create_session().session_id
    task = with_build_question(state, session_id)
    snapshot = await state.tasks.snapshot(session_id)
    assert snapshot["tasks"][0]["build_proposal"]["missing_capability"] == (
        "send HTTP POST requests")
    context, _ = await state.tasks.context(session_id)
    assert "build_proposal" in context and "send HTTP POST requests" in context
    state.generator.scripts["main"].append(tool(
        "task_control", operation=operation, status_only=True))
    events = await chat(state, session_id, "Yes, go ahead." if approve else "No.")
    assert "error" not in events
    assert state.selfmod.decisions == [(session_id, task["task_id"], approve)]
    # The chat's own reply is the announcement, so no duplicate notice.
    assert state.selfmod.announced is False
    handoff = json.loads(state.generator.calls[-1]["messages"][-1]["content"])
    assert handoff["task_id"] == task["task_id"]


async def test_build_without_a_pending_question_changes_nothing(make_task_state):
    state = make_task_state([
        tool("task_control", operation="build", status_only=True),
    ], ["Nothing is waiting."])
    session_id = state.sessions.create_session().session_id
    with_build_question(state, session_id)
    state.selfmod.pending = None
    await chat(state, session_id, "Build it.")
    handoff = json.loads(state.generator.calls[-1]["messages"][-1]["content"])
    assert "No capability build is waiting" in handoff["error"]
    assert state.selfmod.decisions == []


async def test_the_users_answer_in_chat_unblocks_a_paused_build(make_task_state):
    state = make_task_state([], ["Got it."])
    session_id = state.sessions.create_session().session_id
    task = with_step_question(state, session_id)
    snapshot = await state.tasks.snapshot(session_id)
    assert snapshot["tasks"][0]["step_proposal"]["kind"] == "ask"
    assert snapshot["tasks"][0]["step_proposal"]["question"] == (
        "Which calendar should I use?")
    context, _ = await state.tasks.context(session_id)
    assert "step_proposal" in context and "Which calendar" in context
    state.generator.scripts["main"].append(tool(
        "task_control", operation="answer_step", text="The work one",
        status_only=True))
    events = await chat(state, session_id, "The work one.")
    assert "error" not in events
    assert state.selfmod.answers == [(session_id, task["task_id"],
                                      "The work one")]


async def test_answer_step_without_a_pending_question_changes_nothing(
    make_task_state,
):
    state = make_task_state([
        tool("task_control", operation="answer_step", text="hi",
             status_only=True),
    ], ["Nothing is waiting."])
    session_id = state.sessions.create_session().session_id
    with_step_question(state, session_id)
    state.selfmod.pending = None
    await chat(state, session_id, "The work one.")
    handoff = json.loads(state.generator.calls[-1]["messages"][-1]["content"])
    assert "No build is waiting for an answer" in handoff["error"]
    assert state.selfmod.answers == []


async def test_answer_step_rejects_non_text_without_unblocking(make_task_state):
    state = make_task_state([
        tool("task_control", operation="answer_step",
             text={"calendar": "work"}, status_only=True),
    ], ["The answer must be text."])
    session_id = state.sessions.create_session().session_id
    with_step_question(state, session_id)
    await chat(state, session_id, "The work one.")
    handoff = json.loads(state.generator.calls[-1]["messages"][-1]["content"])
    assert "The answer must be text" in handoff["error"]
    assert state.selfmod.answers == []


async def test_the_task_card_buttons_decide_through_the_api(make_task_state):
    state = make_task_state([])
    session_id = state.sessions.create_session().session_id
    task = with_build_question(state, session_id)
    body = {"session_id": session_id, "task_id": task["task_id"], "approve": False}
    async with client_for(state) as client:
        response = await client.post("/api/selfmod/decide", json=body)
        assert response.status_code == 200
        assert response.json() == {"task_id": task["task_id"], "build": "declined"}
        assert state.selfmod.announced is True
        again = await client.post("/api/selfmod/decide", json=body)
        assert again.status_code == 409
        state.selfmod = None
        assert (await client.post("/api/selfmod/decide", json=body)).status_code == 404
    assert state.selfmod is None


def test_build_operations_are_offered_only_while_a_question_waits():
    def operations(tools):
        control = next(t for t in tools if t["function"]["name"] == "task_control")
        return control["function"]["parameters"]["properties"]["operation"]["enum"]

    assert "build" not in operations(api_task_tools())
    assert {"build", "skip_build"} <= set(operations(api_task_tools(True)))
    assert "connect" not in operations(api_task_tools())
    assert {"connect", "skip_connect"} <= set(
        operations(api_task_tools(True, True)))
    assert "connect" not in operations(api_task_tools(True, False))
    assert "build" not in operations(api_task_tools(False, True))
    assert "answer_step" not in operations(api_task_tools())
    assert "answer_step" in operations(api_task_tools(True, True, True))
    assert "answer_step" not in operations(api_task_tools(True, True, False))


def api_task_tools(build_question=False, connect_question=False,
                   step_question=False):
    from recollect.task_chat import task_tools

    return task_tools(build_question, connect_question, step_question)
