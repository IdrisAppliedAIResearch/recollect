"""Agentic development: free OpenCode sessions, JSON only at the router boundary."""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from recollect.engine.sandbox.configgen import build_development_config
from recollect.selfmod.agents import AgentDeveloper, DockerChecks, final_json
from recollect.selfmod.contracts import ChangePolicy, File, Snapshot
from recollect.selfmod.tests_first import parse_tests
from tests.test_selfmod_tests_first import authored

BASELINE = Snapshot((File("recollect/__init__.py", b""),
                     File("recollect/engine/mcp_research.py", b"TOOLS = []\n")))
POLICY = ChangePolicy(BASELINE.sha256, modify=("recollect/engine/mcp_research.py",),
                      create_under=("recollect/engine/subagent_tools",))
PLAN = {"summary": "add the tool", "changes": [
    {"path": "recollect/engine/subagent_tools/post.py", "operation": "create",
     "reason": "new tool"},
    {"path": "recollect/engine/mcp_research.py", "operation": "modify",
     "reason": "register"}]}
FIXED = "def post(): return 200\n"


class Agent:
    """A scripted OpenCode server: each role's replies, in order."""

    def __init__(self, workdir, script):
        self.workdir, self.script, self.messages = workdir, script, []

    def handler(self, request):
        if request.url.path.endswith("/message"):
            text = json.loads(request.content)["parts"][0]["text"]
            self.messages.append(text)
            reply = self.script.pop(0)
            content = reply(self.workdir, text) if callable(reply) else reply
            return httpx.Response(200, json={"parts": [
                {"type": "text", "text": content}]})
        return httpx.Response(200, json={})


class Manager:
    def __init__(self, tmp_path, name, script):
        self.workdir = tmp_path / name
        self.workdir.mkdir()
        self.agent = Agent(self.workdir, script)
        self.sessions, self.finished, self.torn_down = 0, 0, False

    async def begin_invocation(self, name, *, continuous):
        assert continuous
        self.sessions += 1
        client = httpx.AsyncClient(base_url="http://agent",
                                   transport=httpx.MockTransport(self.agent.handler))
        return SimpleNamespace(handle=SimpleNamespace(workdir=self.workdir,
                                                      client=client),
                               oc_session_id=f"s{self.sessions}")

    async def finish_invocation(self, invocation):
        self.finished += 1

    @property
    def selfmod_paths(self):
        return (self.workdir,)

    async def quiesce_invocation(self, invocation):
        pass

    async def teardown(self):
        self.torn_down = True


def write(path, text):
    def reply(workdir, message):
        target = workdir / "source" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())
        return "done"
    return reply


def delete(path):
    def reply(workdir, message):
        (workdir / "source" / path).unlink()
        return "done"
    return reply


def both(*steps):
    def reply(workdir, message):
        for step in steps:
            step(workdir, message)
        return "Changed the files."
    return reply


async def test_agents_plan_review_implement_and_fix_from_feedback(tmp_path):
    tests = parse_tests(authored())
    verdict = lambda approved: json.dumps({"approved": approved, "findings": (  # noqa: E731
        [] if approved else [{"severity": "blocking", "issue": "x", "fix": "y"}])})
    builder = Manager(tmp_path, "builder", [
        "I explored the code. Plan coming.",
        "Here is the plan:\n```json\n" + json.dumps(PLAN) + "\n```",
        "Revised plan: " + json.dumps(PLAN),
        # Implementation: an out-of-policy deletion first.
        both(write("recollect/engine/subagent_tools/post.py", "def post(): ...\n"),
             delete("recollect/__init__.py")),
        both(write("recollect/__init__.py", ""),
             write("recollect/engine/mcp_research.py", "TOOLS = ['post']\n"),
             write("scratch/__pycache__/x.pyc", "cache")),
        both(write("recollect/engine/subagent_tools/post.py", FIXED)),
    ])
    diffs = []

    def reviewed(workdir, text):
        # The diff reaches the reviewer's workspace, which is removed afterwards.
        diffs.append((workdir / "changes.diff").read_text(encoding="utf-8"))
        return verdict(True)

    reviewer = Manager(tmp_path, "reviewer", [
        "Looks fine?",                       # unreadable verdict, asked again
        verdict(False), verdict(True),       # plan review: reject, then approve
        reviewed,                            # code review
    ])
    managers = iter([builder, reviewer])
    checks_seen = []

    async def run_checks(tree, checks):
        checks_seen.append(tree)
        passed = b"return 200" in dict((f.path, f.content) for f in tree.files).get(
            "recollect/engine/subagent_tools/post.py", b"")
        return [{"name": c.path[:-3], "passed": passed, "exitcode": 0 if passed else 1,
                 "stdout": "", "stderr": base64.b64encode(b"AssertionError").decode()}
                for c in checks]

    events = []

    async def on_event(kind, data):
        events.append(kind)

    developer = AgentDeveloper(
        request="post it", gap={"missing_capability": "POST"},
        baseline=BASELINE, policy=POLICY, protected=("recollect/__init__.py",),
        manager_factory=lambda name: next(managers), run_checks=run_checks,
        on_event=on_event)
    candidate = await developer(1, tests, ("attempt 0: boom",))

    files = {f.path: f.content for f in candidate.files}
    assert files["recollect/engine/subagent_tools/post.py"] == FIXED.encode()
    assert files["recollect/engine/mcp_research.py"] == b"TOOLS = ['post']\n"
    assert not any("scratch" in p for p in files)
    # The builder is one continuous session: plan, implement and every fix.
    assert builder.sessions == 1 and builder.finished == 1
    first, *rest = builder.agent.messages
    assert "<request>\npost it" in first and "attempt 0: boom" in first
    assert "one JSON object" in rest[0]
    assert "rejected the plan" in rest[1]
    assert "<approved_plan>" in rest[2]
    assert "not allowed" in rest[3] and "delete recollect/__init__.py" in rest[3]
    assert "AssertionError" in rest[4]
    # Reviews are fresh sessions each time, on their own sandbox.
    assert reviewer.sessions == 3 and reviewer.finished == 3
    assert builder.torn_down and reviewer.torn_down
    # A build leaves no workspace behind, only the candidate it returned.
    assert not builder.workdir.exists() and not reviewer.workdir.exists()
    assert "+def post(): return 200" in diffs[0]
    assert len(checks_seen) == 2
    assert events == ["plan_unreadable", "plan_review_unreadable", "plan_review",
                      "plan_review", "plan_approved", "implementation_turn",
                      "policy_violations", "implementation_turn", "checks",
                      "implementation_turn", "checks", "code_review", "candidate"]


async def test_a_cancelled_attempt_quiesces_and_releases_the_sandbox(tmp_path):
    import asyncio

    started = asyncio.Event()
    builder = Manager(tmp_path, "builder", [])
    quiesced = []

    async def quiesce(invocation):
        quiesced.append(invocation)

    builder.quiesce_invocation = quiesce

    async def slow(request):
        started.set()
        await asyncio.Event().wait()

    builder.agent.handler = slow
    reviewer = Manager(tmp_path, "reviewer", [])
    managers = iter([builder, reviewer])
    developer = AgentDeveloper(
        request="r", gap={}, baseline=BASELINE, policy=POLICY,
        protected=(), manager_factory=lambda name: next(managers),
        run_checks=None)
    job = asyncio.create_task(developer(1, parse_tests(authored()), ()))
    await asyncio.wait_for(started.wait(), 5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert quiesced and builder.finished == 1 and builder.torn_down


async def test_frozen_checks_run_without_network_or_host_mounts(tmp_path):
    import io
    import tarfile

    calls = []

    class Docker:
        async def run(self, *args, data=None):
            calls.append(args)
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                names = tar.getnames()
            assert "source/recollect/engine/mcp_research.py" in names
            assert "checks/a.py" in names
            return (0 if args[-1].endswith("/a.py") else 3), b"out", b"err"

    checks = (File("a.py", b"pass\n"), File("b.py", b"raise SystemExit(3)\n"))
    results = await DockerChecks(Docker(), "sha256:" + "0" * 64)(BASELINE, checks)
    assert [(r["name"], r["passed"], r["exitcode"]) for r in results] == [
        ("a", True, 0), ("b", False, 3)]
    for args in calls:
        assert args[args.index("--network") + 1] == "none"
        assert "--read-only" in args and "--mount" not in args


def test_development_config_is_stock_opencode_with_everything_allowed():
    config = build_development_config(base_url="http://m/v1", model="local",
                                      api_key="k", context_limit=131072,
                                      output_limit=32768)
    assert config["permission"] == {"*": "allow", "question": "deny",
                                    "external_directory": "deny"}
    assert "mcp" not in config and "agent" not in config and "skills" not in config


def test_final_json_takes_the_last_object_with_the_keys():
    text = 'first {"approved": false, "findings": []} then {"x": 1} ' \
            'finally {"approved": true, "findings": [{"a": {"b": 1}}]} bye'
    assert final_json(text, {"approved", "findings"}) == {
        "approved": True, "findings": [{"a": {"b": 1}}]}
    assert final_json("no json here", {"approved"}) is None


def test_step_parsers_accept_only_connect_and_ask():
    from recollect.selfmod.agents import parse_step, valid_step
    assert valid_step({"kind": "connect", "connector": "google_calendar"})
    assert valid_step({"kind": "ask", "question": "which calendar?"})
    assert not valid_step({"kind": "connect"})
    assert not valid_step({"kind": "ask"})
    assert not valid_step({"kind": "other", "connector": "x"})
    assert not valid_step(None)
    text = ('work done {"step": {"kind": "connect", "connector": '
            '"google_calendar", "why": "need it"}}')
    assert parse_step(text) == {"kind": "connect",
                                "connector": "google_calendar",
                                "why": "need it"}
    assert parse_step("no step here") is None
    assert parse_step('{"unrelated": 1}') is None


def _run_checks_factory():
    async def run_checks(tree, checks):
        passed = b"return 200" in dict(
            (f.path, f.content) for f in tree.files).get(
            "recollect/engine/subagent_tools/post.py", b"")
        return [{"name": c.path[:-3], "passed": passed,
                 "exitcode": 0 if passed else 1, "stdout": "",
                 "stderr": base64.b64encode(b"ok").decode()} for c in checks]
    return run_checks


async def test_develop_pauses_on_a_step_and_resumes_in_a_fresh_session(tmp_path):
    tests = parse_tests(authored())
    verdict = lambda approved: json.dumps({"approved": approved, "findings": (  # noqa: E731
        [] if approved else [{"severity": "blocking", "issue": "x", "fix": "y"}])})
    STEP = json.dumps({"step": {"kind": "connect",
                                "connector": "google_calendar",
                                "why": "the tool needs a connected account"}})

    def request_step(workdir, message):
        target = workdir / "source" / "recollect" / "engine" / "subagent_tools"
        target.mkdir(parents=True, exist_ok=True)
        (target / "post.py").write_bytes(b"def post(): ...\n")
        return STEP

    def finish(workdir, message):
        (workdir / "source" / "recollect" / "engine" / "subagent_tools" /
         "post.py").write_bytes(FIXED.encode())
        (workdir / "source" / "recollect" / "engine" /
         "mcp_research.py").write_bytes(b"TOOLS = ['post']\n")
        return "done"

    builder = Manager(tmp_path, "builder", [
        "Plan coming.", "Plan: " + json.dumps(PLAN), request_step, finish])
    reviewer = Manager(tmp_path, "reviewer", [verdict(True), verdict(True)])
    managers = iter([builder, reviewer])
    steps = []

    async def on_step(step, tree, plan):
        steps.append((step, {f.path: f.content for f in tree.files}))
        return "google_calendar is now connected."

    events = []

    async def on_event(kind, data):
        events.append(kind)

    developer = AgentDeveloper(
        request="post it", gap={"missing_capability": "POST"},
        baseline=BASELINE, policy=POLICY, protected=("recollect/__init__.py",),
        manager_factory=lambda name: next(managers),
        run_checks=_run_checks_factory(), on_event=on_event, on_step=on_step)
    candidate = await developer(1, tests, ())

    files = {f.path: f.content for f in candidate.files}
    assert files["recollect/engine/subagent_tools/post.py"] == FIXED.encode()
    assert files["recollect/engine/mcp_research.py"] == b"TOOLS = ['post']\n"
    # One step was requested, with the work-in-progress captured at the pause.
    assert len(steps) == 1
    step, tree = steps[0]
    assert step["kind"] == "connect" and step["connector"] == "google_calendar"
    assert tree["recollect/engine/subagent_tools/post.py"] == b"def post(): ...\n"
    # The pause ended one session; the resume opened a fresh one on the builder.
    assert builder.sessions == 2 and builder.finished == 2
    assert reviewer.sessions == 2
    # The resume brief carries the step's outcome.
    assert "<step_outcome>" in builder.agent.messages[3]
    assert "now connected" in builder.agent.messages[3]
    assert "step_requested" in events and "step_resolved" in events


async def test_develop_falls_back_when_no_step_handler_is_wired(tmp_path):
    tests = parse_tests(authored())
    verdict = lambda approved: json.dumps({"approved": approved, "findings": []})  # noqa: E731
    STEP = json.dumps({"step": {"kind": "ask",
                                "question": "which calendar should I use?"}})

    def ask(workdir, message):
        return STEP

    def finish(workdir, message):
        target = workdir / "source" / "recollect" / "engine" / "subagent_tools"
        target.mkdir(parents=True, exist_ok=True)
        (target / "post.py").write_bytes(FIXED.encode())
        (workdir / "source" / "recollect" / "engine" /
         "mcp_research.py").write_bytes(b"TOOLS = ['post']\n")
        return "done"

    builder = Manager(tmp_path, "builder", [
        "Plan: " + json.dumps(PLAN), ask, finish])
    reviewer = Manager(tmp_path, "reviewer", [verdict(True), verdict(True)])
    managers = iter([builder, reviewer])
    developer = AgentDeveloper(
        request="post it", gap={"missing_capability": "POST"},
        baseline=BASELINE, policy=POLICY, protected=(),
        manager_factory=lambda name: next(managers),
        run_checks=_run_checks_factory())
    candidate = await developer(1, tests, ())
    # Without a handler the step is reported unavailable and the agent carries on.
    assert builder.sessions == 2
    assert "could not be completed" in builder.agent.messages[2]
    files = {f.path: f.content for f in candidate.files}
    assert files["recollect/engine/subagent_tools/post.py"] == FIXED.encode()


async def test_develop_persists_a_pause_before_blocking_on_the_step(tmp_path):
    tests = parse_tests(authored())
    verdict = lambda approved: json.dumps({"approved": approved, "findings": (  # noqa: E731
        [] if approved else [{"severity": "blocking", "issue": "x", "fix": "y"}])})
    STEP = json.dumps({"step": {"kind": "ask", "question": "which calendar?"}})

    def ask(workdir, message):
        target = workdir / "source" / "recollect" / "engine" / "subagent_tools"
        target.mkdir(parents=True, exist_ok=True)
        (target / "post.py").write_bytes(b"def post(): ...\n")
        return STEP

    def finish(workdir, message):
        (workdir / "source" / "recollect" / "engine" / "subagent_tools" /
         "post.py").write_bytes(FIXED.encode())
        (workdir / "source" / "recollect" / "engine" /
         "mcp_research.py").write_bytes(b"TOOLS = ['post']\n")
        return "done"

    builder = Manager(tmp_path, "builder", [
        "Plan: " + json.dumps(PLAN), ask, finish])
    reviewer = Manager(tmp_path, "reviewer", [verdict(True), verdict(True)])
    managers = iter([builder, reviewer])
    calls, pauses = [], []

    async def on_pause(step, tree, plan, attempt, tests_, feedback):
        pauses.append((step, attempt, tuple(feedback)))
        calls.append("pause")

    async def on_step(step, tree, plan):
        calls.append("step")
        return "The work one."

    developer = AgentDeveloper(
        request="post it", gap={"missing_capability": "POST"},
        baseline=BASELINE, policy=POLICY, protected=(),
        manager_factory=lambda name: next(managers),
        run_checks=_run_checks_factory(), on_step=on_step, on_pause=on_pause)
    candidate = await developer(2, tests, ("attempt 1: boom",))
    # The record is on disk before the build blocks, so a restart can resume it.
    assert calls == ["pause", "step"]
    step, attempt, feedback = pauses[0]
    assert step["kind"] == "ask" and attempt == 2
    assert feedback == ("attempt 1: boom",)
    files = {f.path: f.content for f in candidate.files}
    assert files["recollect/engine/subagent_tools/post.py"] == FIXED.encode()


async def test_develop_resumes_a_paused_attempt_from_its_captured_tree(tmp_path):
    tests = parse_tests(authored())
    verdict = lambda approved: json.dumps({"approved": approved, "findings": []})  # noqa: E731
    RESUME_STEP = {"kind": "ask", "question": "which calendar?"}
    # The captured tree is the whole source at the pause: the baseline plus the
    # half-finished tool, so resuming shows no phantom deletion.
    RESUME_TREE = Snapshot((File("recollect/__init__.py", b""),
                            File("recollect/engine/mcp_research.py",
                                 b"TOOLS = []\n"),
                            File("recollect/engine/subagent_tools/post.py",
                                 b"def post(): ...\n")))

    def finish(workdir, message):
        (workdir / "source" / "recollect" / "engine" / "subagent_tools" /
         "post.py").write_bytes(FIXED.encode())
        (workdir / "source" / "recollect" / "engine" /
         "mcp_research.py").write_bytes(b"TOOLS = ['post']\n")
        return "done"

    builder = Manager(tmp_path, "builder", [finish])
    reviewer = Manager(tmp_path, "reviewer", [verdict(True)])
    managers = iter([builder, reviewer])
    steps, events = [], []

    async def on_step(step, tree, plan):
        steps.append((step, {f.path: f.content for f in tree.files}))
        return "The work one."

    async def on_event(kind, data):
        events.append(kind)

    developer = AgentDeveloper(
        request="post it", gap={"missing_capability": "POST"},
        baseline=BASELINE, policy=POLICY, protected=(),
        manager_factory=lambda name: next(managers),
        run_checks=_run_checks_factory(), on_event=on_event, on_step=on_step,
        resume=(PLAN, RESUME_TREE, RESUME_STEP))
    candidate = await developer(2, tests, ())
    # It re-requested the paused step (carrying the captured tree) before work.
    assert len(steps) == 1
    step, tree = steps[0]
    assert step == RESUME_STEP
    assert tree["recollect/engine/subagent_tools/post.py"] == b"def post(): ...\n"
    # No planning: the first message is the resume brief with the step's outcome.
    assert "<step_outcome>" in builder.agent.messages[0]
    assert "The work one." in builder.agent.messages[0]
    assert "plan_approved" not in events
    assert "step_requested" in events and "step_resolved" in events
    files = {f.path: f.content for f in candidate.files}
    assert files["recollect/engine/subagent_tools/post.py"] == FIXED.encode()


async def test_a_failed_resume_plans_fresh_rather_than_replaying_the_step(tmp_path):
    """The paused record applies to one attempt: when that attempt fails, the
    next one re-plans from the baseline with the new feedback, instead of
    re-requesting the step against the stale plan and tree."""
    tests = parse_tests(authored())
    verdict = lambda approved: json.dumps({"approved": approved, "findings": []})  # noqa: E731
    RESUME_STEP = {"kind": "ask", "question": "which calendar?"}
    RESUME_TREE = Snapshot((File("recollect/__init__.py", b""),
                            File("recollect/engine/mcp_research.py",
                                 b"TOOLS = []\n"),
                            File("recollect/engine/subagent_tools/post.py",
                                 b"def post(): ...\n")))

    def boom(workdir, message):
        raise RuntimeError("the sandbox died")

    def finish(workdir, message):
        tools = workdir / "source" / "recollect" / "engine" / "subagent_tools"
        tools.mkdir(parents=True, exist_ok=True)
        (tools / "post.py").write_bytes(FIXED.encode())
        (workdir / "source" / "recollect" / "engine" /
         "mcp_research.py").write_bytes(b"TOOLS = ['post']\n")
        return "done"

    builder_resumed = Manager(tmp_path, "builder", [boom])
    reviewer_resumed = Manager(tmp_path, "reviewer", [])
    builder_fresh = Manager(tmp_path, "builder-fresh", [
        "Plan: " + json.dumps(PLAN), finish])
    reviewer_fresh = Manager(tmp_path, "reviewer-fresh",
                             [verdict(True), verdict(True)])
    managers = iter([builder_resumed, reviewer_resumed,
                     builder_fresh, reviewer_fresh])
    steps, events = [], []

    async def on_step(step, tree, plan):
        steps.append(step)
        return "The work one."

    async def on_event(kind, data):
        events.append(kind)

    developer = AgentDeveloper(
        request="post it", gap={"missing_capability": "POST"},
        baseline=BASELINE, policy=POLICY, protected=(),
        manager_factory=lambda name: next(managers),
        run_checks=_run_checks_factory(), on_event=on_event, on_step=on_step,
        resume=(PLAN, RESUME_TREE, RESUME_STEP))
    with pytest.raises(RuntimeError, match="the sandbox died"):
        await developer(2, tests, ())
    # The record was consumed: the step was re-requested exactly once, and it
    # no longer applies to a later attempt.
    assert steps == [RESUME_STEP]
    assert developer._resume is None

    candidate = await developer(3, tests, ("attempt 2: the sandbox died",))
    # The next attempt planned fresh from the baseline, carrying the failure.
    assert steps == [RESUME_STEP]
    assert "plan_approved" in events
    first = builder_fresh.agent.messages[0]
    assert "<step_outcome>" not in first
    assert "<earlier_attempts>" in first
    files = {f.path: f.content for f in candidate.files}
    assert files["recollect/engine/subagent_tools/post.py"] == FIXED.encode()
