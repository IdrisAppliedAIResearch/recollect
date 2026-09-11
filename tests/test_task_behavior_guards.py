"""Regression cases found by real conversation replays."""

import asyncio
import json

import httpx
import pytest

from recollect.engine import webtools
from recollect.engine.sandbox.evidence import ResearchEvidence
from recollect.engine.sandbox.runner import TaskCommand
from recollect.task_replies import substantive_memory, unstarted_work
from tests import test_task_chat as chat_tests
from tests.test_continuous_runtime import NativeProtocol, report_part
from tests.test_subagent import _episode_rows
from tests.test_task_chat import chat, seed_task, tool

base_task_state = chat_tests.make_task_state


@pytest.fixture
def make_task_state(base_task_state):
    def create(main, final=()):
        state = base_task_state(main, final)
        state.generator.key_fn = lambda tools: (
            "main" if tools and tools[0]["function"]["name"] != "task_reply"
            else "final"
        )
        return state
    return create


@pytest.mark.parametrize("native_error", [None, {"name": "APIError"}])
async def test_parent_result_receipt_survives_empty_closing_response(
    tmp_path, native_error,
):
    native = NativeProtocol(tmp_path)
    native.release.set()
    original = native.request

    async def request(req):
        response = await original(req)
        if req.method == "POST" and req.url.path.endswith("/message"):
            native.parts.append(report_part("result"))
            return httpx.Response(200, json={
                "parts": [], "info": {"error": native_error},
            })
        return response

    native.client._transport = httpx.MockTransport(request)
    reports = []

    async def report(item):
        reports.append(item)

    try:
        results = [item async for item in native.runner.run_continuous(
            "conversation", "Research", commands=asyncio.Queue(), report=report,
        )]
        if native_error:
            assert results[-1].status == "error"
            assert "APIError" in results[-1].error
        else:
            assert results[-1].status == "ok"
            assert results[-1].summary == "Verified report"
            assert not any(r.kind == "blocked" for r in reports)
    finally:
        await native.client.aclose()


async def test_steering_unblocks_children_and_preserves_queued_directions(tmp_path):
    native = NativeProtocol(tmp_path)
    original = native.request
    partial = native.workdir / "partial.md"

    async def request(req):
        if req.url.path.endswith("/children"):
            return httpx.Response(200, json=[{
                "id": "ses_child", "parentID": "ses_parent",
            }])
        if req.url.path == "/session/ses_child/abort":
            native.order.append("child-abort")
            return httpx.Response(200, json=True)
        if req.url.path == "/session/ses_child/message":
            return httpx.Response(200, json=[])
        if (req.method == "POST" and req.url.path.endswith("/message")
                and native.prompts):
            assert "child-abort" in native.order
            assert partial.read_text() == "Saved work"
            native.prompts.append(json.loads(req.content)["parts"][0]["text"])
            native.parts.extend([
                report_part("accepted", 3, "third"), report_part("result", 3),
            ])
            return httpx.Response(200, json={"parts": []})
        return await original(req)

    native.client._transport = httpx.MockTransport(request)
    commands = asyncio.Queue()
    reports = []

    async def report(item):
        reports.append(item)

    async def consume():
        return [item async for item in native.runner.run_continuous(
            "conversation", "Research", commands=commands, report=report,
        )]

    running = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(native.first_started.wait(), 1)
        partial.write_text("Saved work", encoding="utf-8")
        commands.put_nowait(TaskCommand("second", "steer", "Include dates.", 2))
        commands.put_nowait(TaskCommand("third", "steer", "Exclude Artemis.", 3))
        results = await asyncio.wait_for(running, 3)
        assert results[-1].status == "ok"
        assert len(native.prompts) == 2
        assert "Include dates." in native.prompts[-1]
        assert "Exclude Artemis." in native.prompts[-1]
        assert any(r.kind == "accepted" and r.revision == 3 for r in reports)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await native.client.aclose()


async def test_research_promise_retries_with_an_operation_and_starts_once(
    make_task_state,
):
    promise = "I'm going to do some quick research on the company now."
    state = make_task_state([
        tool("task_reply", text=promise, status_only=False, memory_reply=promise),
        tool("run_subagent", task="Research TRIA Federal", effort="focused"),
    ], ["The research is queued."])
    session = state.sessions.create_session().session_id
    message = ("I want to introduce you to my team at TRIA Federal. Do you want "
               "to do some quick research to see what our company does?")
    events = await chat(state, session, message)
    assert events["done"]["committed"] is False
    assert [t["function"]["name"] for t in state.generator.calls[1]["tools"]] == [
        "run_subagent",
    ]
    assert len(state.task_store.list(session)) == 1
    assert _episode_rows(state, session) == []
    await chat(state, session, message)
    assert len(state.task_store.list(session)) == 1


async def test_recovery_cannot_accept_another_empty_promise(make_task_state):
    promise = tool("task_reply", text="I'll research it now.",
                   status_only=True, memory_reply=None)
    state = make_task_state([promise, promise])
    session = state.sessions.create_session().session_id
    events = await chat(state, session, "Research this company.")
    assert "did not start" in events["error"]["message"]
    assert not state.task_store.list(session)
    assert not _episode_rows(state, session)


async def test_recovery_cannot_substitute_a_status_read_for_starting_work(
    make_task_state,
):
    state = make_task_state([
        tool("task_reply", text="I can help.", status_only=True, memory_reply=None),
        tool("task_control", operation="status", status_only=True),
    ])
    session = state.sessions.create_session().session_id
    events = await chat(state, session, "Research this company.")
    assert "did not start" in events["error"]["message"]
    assert not state.task_store.list(session)


@pytest.mark.parametrize("text", [None, "", "   "])
async def test_missing_control_text_preserves_the_user_revision(make_task_state, text):
    arguments = {} if text is None else {"text": text}
    state = make_task_state([], ["The revision is queued."])
    session = state.sessions.create_session().session_id
    parent = seed_task(state, session)
    state.task_store.update(session, parent["task_id"], state="completed")
    state.generator.scripts["main"].append(tool(
        "task_control", operation="continue", task_id=parent["task_id"],
        status_only=True, **arguments,
    ))
    revision = "Revise brief.md to say Tuesday. Keep Casey as owner."
    events = await chat(state, session, revision)
    assert "error" not in events
    child = state.task_store.request(session, "request-1")
    assert child["parent_task_id"] == parent["task_id"]
    assert child["objective"] == revision
    assert child["original_message"] == revision


@pytest.mark.parametrize("memory", [
    "I'm creating brief.md now with Monday and Casey.",
    "I've kicked off the research task.",
    "I have queued the research.",
])
async def test_task_promise_is_not_ingested_even_if_model_marks_it_memory(
    make_task_state, memory,
):
    state = make_task_state([
        tool("run_subagent", task="Create brief.md", memory_reply=memory),
    ], ["The file task is queued."])
    session = state.sessions.create_session().session_id
    events = await chat(state, session, "Create a Markdown file from these facts.")
    assert not events["done"]["committed"]
    assert not _episode_rows(state, session)


def test_mixed_memory_retains_the_user_fact_without_the_promise():
    assert substantive_memory("You teach biology. I'll research enzymes now.") == (
        "You teach biology."
    )
    assert substantive_memory("Casey is creating a schedule.") == (
        "Casey is creating a schedule."
    )


def test_task_decline_is_operational_but_personal_event_is_not():
    assert substantive_memory("The user declined further research into TRIA.") is None
    assert substantive_memory("You teach biology. You paused the research.") == (
        "You teach biology."
    )
    personal = "You declined a job offer."
    assert substantive_memory(personal) == personal


@pytest.mark.parametrize("message,reply", [
    ("Explain research methods.", "I'll research methods in this example."),
    ("Don't research this.", "I'll research it."),
    ("Translate 'I'll research this'.", "I'll research this."),
    ("Research flights.", "If you give me dates, I'll research flights."),
    ("Research this company.", "Which company do you mean?"),
])
def test_quotes_negations_and_clarifications_do_not_trigger_work(message, reply):
    assert not unstarted_work(message, reply)


def test_no_files_constraint_does_not_disable_research_recovery():
    assert unstarted_work("Research the company. Don't make files.",
                          "I'll research the company now.")
    assert unstarted_work(
        "Could you look up the launch dates?", "I can help with that!",
    )
    assert not unstarted_work("What is research?", "Research means investigation.")
    assert unstarted_work(
        "Research Artemis I and II.",
        "If you'd like me to verify the details, I can start research. Want me to?",
    )


def test_evidence_ignores_limits_errors_and_shorter_views():
    evidence = ResearchEvidence()
    def fetch(text, limit):
        evidence.observe(
            "web_fetch", {"url": "https://example.org", "max_chars": limit},
            json.dumps({"final_url": "https://example.org", "text": text}),
        )
    fetch("Launch: Monday. Landing: Friday.", 4000)
    fetch("Launch: Monday. Landing: Friday.", 8000)
    fetch("Launch: Monday.", 200)
    evidence.observe("skill", {}, "new skill metadata")
    evidence.observe("web_fetch", {}, '{"error":"different network failure"}')
    assert evidence.version == 1
    fetch("Launch: Monday. Landing: Friday. Crew: Four.", 8000)
    assert evidence.version == 2


async def test_native_repeated_fetches_with_changing_limits_stop(tmp_path):
    native = NativeProtocol(tmp_path)
    native.release.set()
    original = native.request

    async def capped(request):
        response = await original(request)
        if request.method == "POST" and request.url.path.endswith("/message"):
            count = len(native.prompts)
            native.parts.extend([{
                "type": "tool", "sessionID": "ses_parent", "callID": f"fetch_{count}",
                "tool": "recollect_research_web_fetch",
                "state": {"status": "completed", "input": {
                    "url": "https://example.org", "max_chars": 1000 * count,
                }, "output": json.dumps({
                    "text": "Launch label without the date.",
                    "final_url": "https://example.org",
                })},
            }, {
                "type": "tool", "sessionID": "ses_parent", "callID": f"skill_{count}",
                "tool": "skill", "state": {"status": "completed", "input": {},
                                           "output": f"skill metadata {count}"},
            }])
            return httpx.Response(200, json={"parts": [{
                "type": "text", "text": "MAXIMUM STEPS REACHED",
            }]})
        return response

    native.client._transport = httpx.MockTransport(capped)
    reports = []
    async def report(item):
        reports.append(item)
    try:
        async with asyncio.timeout(3):
            results = [item async for item in native.runner.run_continuous(
                "conversation", "Research dates", commands=asyncio.Queue(),
                report=report,
            )]
        assert len(native.prompts) == 3
        assert results[-1].status == "partial"
        assert "without new evidence" in results[-1].error
        assert any(item.kind == "blocked" for item in reports)
    finally:
        await native.client.aclose()


async def test_page_view_recovers_factual_cards_without_script_or_navigation(
    monkeypatch,
):
    monkeypatch.setattr(webtools, "_blocked_host", lambda _: "")
    document = """<html><nav>Noise</nav><main><h1>Apollo 12</h1>
    <div><div><p>Launch</p></div><div>Nov. 14, 1969</div></div>
    <script>fake facts</script><div hidden>secret</div>
    <p>The second crewed landing on the Moon.</p></main></html>"""
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text=document),
    )) as client:
        result = json.loads(await webtools.web_fetch(
            client, "https://example.org/mission", view="page",
        ))
    assert "Launch Nov. 14, 1969" in result["text"]
    assert not any(text in result["text"] for text in ("Noise", "fake facts", "secret"))


@pytest.mark.parametrize("obeys", [True, False])
async def test_no_evidence_loop_is_interrupted_before_native_cap(
    tmp_path, obeys, monkeypatch,
):
    monkeypatch.setattr(
        "recollect.engine.sandbox.runner._RECONCILE_SECONDS", 0.02,
    )
    native = NativeProtocol(tmp_path)
    original = native.request

    async def looping(request):
        if request.method == "POST" and request.url.path.endswith("/message"):
            prompt = json.loads(request.content)["parts"][0]["text"]
            native.prompts.append(prompt)
            if len(native.prompts) == 1:
                native.parts.append(report_part())
            elif obeys:
                native.parts.append(report_part(kind="result"))
                return httpx.Response(200, json={"parts": []})
            for index in range(6):
                native.parts.append({
                    "type": "tool", "sessionID": "ses_parent",
                    "callID": f"failed_{len(native.prompts)}_{index}",
                    "tool": "recollect_research_web_fetch", "state": {
                        "status": "completed", "input": {
                            "url": "https://example.org/denied",
                            "max_chars": 1000 + index,
                        }, "output": '{"error":"HTTP 403"}',
                    },
                })
            await asyncio.Event().wait()
        return await original(request)

    native.client._transport = httpx.MockTransport(looping)
    async def report(item):
        pass
    try:
        async with asyncio.timeout(8):
            result = [item async for item in native.runner.run_continuous(
                "conversation", "Research dates", commands=asyncio.Queue(),
                report=report,
            )]
        assert len(native.prompts) == 2
        assert "six research calls" in native.prompts[-1]
        assert "abort" in native.order
        if obeys:
            assert result[-1].status == "ok"
            assert result[-1].summary == "Verified report"
        else:
            assert result[-1].status != "ok"
            assert "without new evidence" in result[-1].error
    finally:
        await native.client.aclose()


async def test_report_limits_are_disclosed_and_enforced():
    from mcp.server.fastmcp import FastMCP

    from recollect.engine.mcp_research import report_message

    server = FastMCP('report-contract')
    server.tool()(report_message)
    tool = (await server.list_tools())[0]
    assert tool.inputSchema['properties']['text']['maxLength'] == 4000
    assert tool.inputSchema['properties']['revision']['minimum'] == 1
    with pytest.raises(ValueError, match='4000'):
        await report_message('result', 'x' * 4001, 1)
    receipt = json.loads(await report_message('result', 'x' * 4000, 1))
    assert len(receipt['text']) == 4000


async def test_reported_result_recovers_missing_acknowledgment_once(tmp_path):
    native = NativeProtocol(tmp_path)
    original = native.request

    async def request(message):
        if message.method == "POST" and message.url.path.endswith("/message"):
            native.prompts.append(json.loads(message.content)["parts"][0]["text"])
            native.parts.append(report_part(
                "result" if len(native.prompts) == 1 else "accepted",
            ))
            return httpx.Response(200, json={"parts": []})
        return await original(message)

    async def report(*args):
        return None

    native.client._transport = httpx.MockTransport(request)
    try:
        async with asyncio.timeout(5):
            results = [item async for item in native.runner.run_continuous(
                "conversation", "Create a file", commands=asyncio.Queue(),
                report=report,
            )]
        assert len(native.prompts) == 2
        assert "Do not redo the work" in native.prompts[-1]
        assert results[-1].status == "ok"
        assert results[-1].summary == "Verified report"
    finally:
        await native.client.aclose()
