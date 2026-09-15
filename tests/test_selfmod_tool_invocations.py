"""Host attribution of relay requests to exactly one in-flight MCP tool call."""

from recollect import tasks as tasks_module
from recollect.engine.subagent import SubagentResult
from recollect.selfmod.tool_invocations import ToolInvocations
from tests.test_tasks import environment, submit, wait_state  # noqa: F401


def part(status, *, call="c1", tool="recollect_research_make_event",
         session="s1"):
    return {"type": "message.part.updated", "properties": {"part": {
        "type": "tool", "sessionID": session, "callID": call, "tool": tool,
        "state": {"status": status}}}}


def test_single_running_mcp_call_is_the_attributed_invocation():
    tracker = ToolInvocations()
    tracker.observe("task", part("pending"), "s1", set())
    identity = tracker.running("task")
    assert identity == "task:s1:c1" and tracker.tool(identity) == "make_event"
    tracker.observe("task", part("running"), "s1", set())
    assert tracker.running("task") == identity
    tracker.observe("task", part("completed"), "s1", set())
    assert tracker.running("task") is None and tracker.tool(identity) == "make_event"


def test_observed_arguments_are_kept_for_an_exact_replay():
    tracker = ToolInvocations()
    event = part("running")
    event["properties"]["part"]["state"]["input"] = {"title": "x", "minutes": 30}
    tracker.observe("task", event, "s1", set())
    identity = tracker.running("task")
    copied = tracker.arguments(identity)
    assert copied == {"title": "x", "minutes": 30}
    copied["title"] = "changed"
    assert tracker.arguments(identity)["title"] == "x"
    assert tracker.arguments("unknown") is None


def test_ambiguous_foreign_or_builtin_calls_are_never_attributed():
    tracker = ToolInvocations()
    tracker.observe("task", part("running", tool="read"), "s1", set())
    tracker.observe("task", part("running", session="elsewhere"), "s1", set())
    tracker.observe("other", part("running", call="x"), "s1", set())
    assert tracker.running("task") is None
    tracker.observe("task", part("running", session="child"), "s1", {"child"})
    assert tracker.running("task") == "task:child:c1"
    tracker.observe("task", part("running", call="c2"), "s1", set())
    assert tracker.running("task") is None
    tracker.observe("task", {"type": "session.updated"}, "s1", set())
    tracker.observe("task", "not-an-event", "s1", set())


async def test_coordinator_hands_each_task_its_own_observer(
    environment, monkeypatch,  # noqa: F811 - pytest fixture
):
    state = environment
    tracker = ToolInvocations()
    state.coordinator.tool_observer = tracker.observe

    class Runner:
        def __init__(self, manager, config, *, observer):
            self.observer = observer

        async def run_continuous(self, session_id, task, **kwargs):
            self.observer(part("running"), "s1", set())
            state.calls.append(tracker.running(state.task_id))
            yield SubagentResult("task", "partial", "{}", "Stopped")

    monkeypatch.setattr(tasks_module, "OpenCodeRunner", Runner)
    await state.coordinator.start()
    task = await submit(state)
    state.task_id = task["task_id"]
    await wait_state(state, task["task_id"], "blocked")
    assert state.calls == [f"{task['task_id']}:s1:c1"]
