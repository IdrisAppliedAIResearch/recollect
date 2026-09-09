"""A status lookup ends in a bounded reply, never a second task operation."""

import json

import httpx
import pytest

from recollect.engine.generator import Generator, GeneratorSettings
from tests import test_task_chat
from tests.test_subagent import _episode_rows
from tests.test_task_chat import chat, seed_task

make_task_state = test_task_chat.make_task_state


@pytest.mark.parametrize("message,mixed", [
    ("What have you found so far?", False),
    ("What have you learned so far?", False),
    ("What have you found so far? I teach biology.", True),
])
async def test_progress_question_cannot_be_reclassified_as_memory(
    make_task_state, message, mixed,
):
    state = make_task_state([test_task_chat.tool(
        "task_reply", text="The research found five years of coverage.",
        status_only=False,
        memory_reply="You teach biology." if mixed else "Five years of coverage.",
    )])
    session_id = state.sessions.create_session().session_id
    task = seed_task(state, session_id)
    state.task_store.update(session_id, task["task_id"], state="completed")
    events = await chat(state, session_id, message)
    assert "error" not in events
    assert events["done"]["committed"] is mixed
    assert len(_episode_rows(state, session_id)) == int(mixed)


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("stage", ["empty", "partial", "completed"])
@pytest.mark.parametrize("mixed", [False, True])
async def test_status_handoff_uses_reply_schema_and_preserves_memory_selection(
    make_task_state, named, stage, mixed,
):
    state = make_task_state([])
    session_id = state.sessions.create_session().session_id
    task = seed_task(state, session_id)
    findings = [] if stage == "empty" else ["The warranty covers five years."]
    state.task_store.update(
        session_id, task["task_id"],
        state="completed" if stage == "completed" else "running",
        findings=findings,
    )
    requests = []
    reply = (
        "The work is still running; no findings have been reported yet."
        if stage == "empty" else
        "The warranty covers five years. "
        + ("The work is complete." if stage == "completed" else
           "This is a partial finding; research is still running.")
    )

    def server(request):
        body = json.loads(request.content)
        requests.append(body)
        schema = body["response_format"]["json_schema"]["schema"]["oneOf"]
        names = [item["properties"]["name"]["const"] for item in schema]
        assert "tools" not in body and "tool_choice" not in body
        conversation_started = False
        for item in body["messages"]:
            if item["role"] == "system":
                assert not conversation_started
            else:
                conversation_started = True
        if len(requests) == 1:
            assert names == ["run_subagent", "task_control", "task_reply"]
            arguments = {
                "operation": "status", "status_only": not mixed,
                "memory_reply": "You teach biology." if mixed else None,
            }
            if named:
                arguments["task_id"] = task["task_id"]
            operation = {"name": "task_control", "arguments": arguments}
        else:
            assert names == ["task_reply"]
            assert all(item["role"] != "tool" and "tool_calls" not in item
                       for item in body["messages"])
            result = json.loads(body["messages"][-1]["content"])
            selected = result if named else result["tasks"][0]
            assert selected["task_id"] == task["task_id"]
            assert selected["findings"] == findings
            operation = {"name": "task_reply", "arguments": {
                "text": reply, "status_only": False,
                # A later acknowledgment must not override the first decision.
                "memory_reply": "UNWANTED OPERATIONAL MEMORY",
            }}
        frame = {"choices": [{
            "delta": {"content": json.dumps(operation)}, "finish_reason": "stop",
        }]}
        return httpx.Response(200, text=f"data: {json.dumps(frame)}\n\n")

    generator = Generator(GeneratorSettings(
        base_url="http://model/v1", model="test", require_tools=True,
    ))
    await generator._client.aclose()
    generator._client = httpx.AsyncClient(
        base_url="http://model/v1", transport=httpx.MockTransport(server),
    )
    state.generator = generator
    try:
        events = await chat(
            state, session_id,
            "What have you found so far?" + (" I teach biology." if mixed else ""),
        )
    finally:
        await generator.aclose()
    assert len(requests) == 2
    assert "error" not in events
    assert events["token"]["text"] == reply
    assert events["done"]["committed"] is mixed
    rows = _episode_rows(state, session_id)
    assert len(rows) == int(mixed)
    if mixed:
        assert rows[0]["assistant_message"] == "You teach biology."
    assert state.sessions.chat_history(session_id)[0].assistant_message == reply
    assert len(state.task_store.list(session_id)) == 1
    current = state.task_store.get(session_id, task["task_id"])
    assert current["revision"] == 1 and current["findings"] == findings
    assert current["state"] == ("completed" if stage == "completed" else "running")


@pytest.mark.parametrize("named", [False, True])
async def test_internal_markup_is_not_persisted_as_a_chat_reply(
    make_task_state, named,
):
    from tests.test_task_chat import tool

    state = make_task_state([], [
        "<tool_call><function=task_reply>Internal protocol</function></tool_call>",
    ])
    session_id = state.sessions.create_session().session_id
    task = seed_task(state, session_id)
    arguments = {"operation": "status", "status_only": True}
    if named:
        arguments["task_id"] = task["task_id"]
    state.generator.scripts["main"].append(tool("task_control", **arguments))
    events = await chat(state, session_id, "What have you found so far?")
    assert "error" in events and "token" not in events
    assert events["done"]["committed"] is False
    assert events["done"]["generation"]["response_text"] == ""
    assert state.sessions.chat_history(session_id)[0].assistant_message == ""
    assert _episode_rows(state, session_id) == []
    assert len(state.task_store.list(session_id)) == 1
