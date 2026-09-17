"""App wiring: A serves from its own bundle, and one gap runs one loop."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import recollect.selfmod.service as service_module
from recollect.engine.sandbox.manager import SandboxDeployment
from recollect.selfmod.deployment import TaskDeployments, materialize_skills
from recollect.selfmod.loop import Outcome
from recollect.selfmod.service import SelfModificationService
from tests.selfmod_fake_images import BASE
from tests.test_selfmod_loop import Images

REPOSITORY = Path(__file__).resolve().parents[1]
GAP = {"task_id": "task-a", "missing_capability": "calendar write",
       "modification_request": "add a calendar tool"}


class Coordinator:
    def __init__(self):
        self.deployments = self.on_gap = None
        self.interrupts, self.notices, self.progress = [], [], []
        self.store = SimpleNamespace(
            get=lambda session_id, task_id: {"original_message": "book the room"},
            notify=lambda *args: self.notices.append(args),
            update=lambda session_id, task_id, **changes: self.progress.append(
                changes["progress"]))

    async def interrupt_for_selfmod(self, session_id, task_id, progress):
        self.interrupts.append((task_id, progress))


class Loop:
    def __init__(self, outcome, entered=None, release=None):
        self.outcome, self.entered, self.release = outcome, entered, release
        self.runs, self.stopped = [], False

    async def run(self, gap):
        self.runs.append(gap)
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        return self.outcome

    def stop(self):
        self.stopped = True


@pytest.fixture
def service(tmp_path):
    coordinator = Coordinator()
    loops = []

    def manager(receipt, name):
        skills = materialize_skills(receipt.bundle, tmp_path / ("skills-" + name))
        return SimpleNamespace(deployment=SandboxDeployment(
            receipt.image_id, skills, tmp_path / ("root-" + name)))

    def loop_factory(journal, session_id, task_id, request, gap):
        if not loops:
            loops.append(Loop(Outcome(True, "done")))
        loops[0].request = request
        return loops[0]

    value = SelfModificationService(
        SimpleNamespace(), coordinator, repository=REPOSITORY, root=tmp_path / "root",
        images=Images(), base_image_id=BASE, image_environment=("PATH=/usr/bin",),
        role_endpoint="http://127.0.0.1:8001/v1", role_model="local",
        runtime_factory=lambda: None, manager_factory=manager,
        loop_factory=loop_factory,
    )
    value.coordinator, value.loops = coordinator, loops
    return value


def kinds(service):
    return [r.value["kind"] for r in service._journal.verify()]


async def test_prepare_serves_a_from_its_bundle_and_installs_the_gap_hook(service):
    verified = await service.prepare()
    assert service.router.serving.role == "A"
    assert service.router.serving.image_id == verified.image_id
    assert isinstance(service.coordinator.deployments, TaskDeployments)
    assert service.coordinator.on_gap == service.handle_gap
    assert "a_registered" in kinds(service)
    await service.close()


async def test_a_second_install_on_one_root_does_not_collide(tmp_path, monkeypatch):
    """A later boot must not trip over the directories an earlier one left."""
    monkeypatch.setattr(
        service_module, "SandboxManager",
        lambda config, **kwargs: SimpleNamespace(deployment=kwargs["deployment"]))
    root = tmp_path / "root"
    sandboxes = tmp_path / "sandboxes"
    for _ in range(2):
        value = SelfModificationService(
            SimpleNamespace(sandbox_root=sandboxes), Coordinator(),
            repository=REPOSITORY, root=root,
            images=Images(), base_image_id=BASE, image_environment=(),
            role_endpoint="http://127.0.0.1:8001/v1", role_model="local",
            runtime_factory=lambda: None,
        )
        assert (await value.prepare()).image_id
        assert value.router.serving.role == "A"
        await value.close()
    # Sandbox trees live outside the app data directory, never in the repo.
    assert len(list(sandboxes.glob("skills-a-*"))) == 2
    assert not list(root.glob("skills-a-*"))


async def test_a_gap_asks_the_user_first_and_builds_nothing_until_yes(service):
    await service.prepare()
    assert await service.coordinator.on_gap("session", GAP) is None
    question = service_module.proposal_notice(GAP)
    assert "calendar write" in question and "Want me to build" in question
    assert service.coordinator.interrupts == [("task-a", question)]
    assert service.coordinator.notices == [
        ("session", "task-a", "selfmod-proposal-task-a", question)]
    assert service.status["state"] == "awaiting_approval"
    assert service.proposal("session", "task-a") == {
        "missing_capability": "calendar write",
        "modification_request": "add a calendar tool"}
    assert service.proposal("session", "other") is None
    assert service.coordinator.build_proposal == service.proposal
    assert not service.loops
    assert "gap_proposed" in kinds(service)
    await service.close()


async def test_yes_runs_the_loop_on_the_original_request(service):
    await service.prepare()
    await service.handle_gap("session", GAP)
    with pytest.raises(ValueError, match="No capability build"):
        await service.decide("session", "other-task", True)
    assert await service.decide("session", "task-a", True) == {
        "task_id": "task-a", "build": "started"}
    assert service.coordinator.progress[-1] == service_module.GAP_NOTICE
    # A button answer has no chat reply, so the notice announces it.
    assert service.coordinator.notices[-1][3] == service_module.GAP_NOTICE
    assert await service._runner == Outcome(True, "done")
    assert service.status["state"] == "finished"
    [loop] = service.loops
    assert loop.runs == [GAP] and loop.request == "book the room"
    assert service.proposal("session", "task-a") is None
    with pytest.raises(ValueError):
        await service.decide("session", "task-a", True)
    assert kinds(service)[-3:] == ["gap_decision", "gap_accepted", "loop_finished"]
    await service.close()


async def test_no_builds_nothing_and_leaves_the_task_blocked(service):
    await service.prepare()
    await service.handle_gap("session", GAP)
    assert await service.decide("session", "task-a", False) == {
        "task_id": "task-a", "build": "declined"}
    assert service.status["state"] == "declined"
    assert service.coordinator.notices[-1][3] == service_module.declined_notice(GAP)
    assert not service.loops and service._runner is None
    # A later gap can ask again.
    await service.handle_gap("session", GAP)
    assert service.status["state"] == "awaiting_approval"
    await service.close()


async def test_canceling_a_task_answers_its_pending_question(service):
    await service.prepare()
    await service.handle_gap("session", GAP)
    service.cancel_requested("session", "task-a")
    assert service.proposal("session", "task-a") is None
    assert service.status["state"] == "declined" and not service.loops
    await service.close()


async def test_a_second_gap_is_declined_while_one_waits_or_runs(service):
    await service.prepare()
    await service.handle_gap("session", GAP)
    await service.handle_gap("session", {**GAP, "task_id": "task-b"})
    assert service.proposal("session", "task-b") is None
    entered, release = asyncio.Event(), asyncio.Event()
    service.loops.append(Loop(Outcome(False, "stopped"), entered, release))
    await service.decide("session", "task-a", True)
    await asyncio.wait_for(entered.wait(), 5)
    await service.handle_gap("session", {**GAP, "task_id": "task-b"})
    assert service.proposal("session", "task-b") is None
    assert kinds(service).count("gap_declined") == 2
    release.set()
    assert await service._runner == Outcome(False, "stopped")
    # The loop already finished, so a later user stop has nothing to stop.
    assert service.stop() is False
    assert not service.loops[0].stopped
    await service.close()


async def test_cancel_on_the_task_card_stops_the_loop_mid_step(service):
    await service.prepare()
    entered, release = asyncio.Event(), asyncio.Event()
    service.loops.append(Loop(Outcome(True, "never"), entered, release))
    await service.handle_gap("session", GAP)
    await service.decide("session", "task-a", True)
    await asyncio.wait_for(entered.wait(), 5)
    assert service.status["state"] == "running"
    service.cancel_requested("other-session", "task-a")
    service.cancel_requested("session", "unrelated-task")
    assert not service._runner.done()
    service.cancel_requested("session", "task-a")
    outcome = await asyncio.wait_for(service._runner, 5)
    assert outcome.stopped and service.status["state"] == "stopped"
    assert service.coordinator.notices[-1][3] == service_module.STOPPED_NOTICE
    assert service.stop() is False
    assert "loop_finished" in kinds(service)
    await service.close()


async def test_milestones_become_notices_and_task_progress(service):
    service.status = {"state": "running", "session_id": "session",
                      "task_id": "task-a", "continuation_task_id": None}
    for kind, data in (
        ("tests_frozen", {"tool_name": "http_request", "checks": 4}),
        ("attempt_started", {"attempt": 1}),
        ("attempt_failed", {"attempt": 1, "reason": "RuntimeError: B crashed.\nmore"}),
        ("attempt_started", {"attempt": 2}),
        ("resuming", {"attempt": 2, "task_id": "task-b"}),
        ("loop_stopped", {}),
    ):
        await service._on_event(kind, data)
    # The chat hears the build start, failures, resumption and stops; frozen
    # tests and retries stay on the card and in the workspace.
    assert [n[3] for n in service.coordinator.notices] == [
        "I'm starting the build for the new HTTP request feature.",
        "Attempt 1 didn't pass: RuntimeError: B crashed. Trying again.",
        "The new capability passed. Resuming your request.",
        service_module.STOPPED_NOTICE,
    ]
    assert service.coordinator.progress == [
        *[n[3] for n in service.coordinator.notices[:2]],
        "Attempt 2 of the HTTP request feature is building.",
        *[n[3] for n in service.coordinator.notices[2:]]]
    assert [a["text"] for a in service.activity] == [
        "Tests frozen: 4 checks for http_request.",
        "Attempt 1 started.",
        "Attempt 1 failed: RuntimeError: B crashed. more",
        "Attempt 2 started.",
        "The new capability passed. Resuming the request.",
        "Stopped.",
    ]
    assert service.status["continuation_task_id"] == "task-b"
    assert service.status["milestone"] == service_module.STOPPED_NOTICE
    await service.close()


async def test_repeated_failures_update_progress_without_new_notices(service):
    service.status = {"state": "running", "session_id": "session",
                      "task_id": "task-a", "continuation_task_id": None}
    await service._on_event("attempt_failed", {"attempt": 1, "reason": "boom",
                                               "repeats": 1})
    await service._on_event("attempt_failed", {"attempt": 2, "reason": "boom",
                                               "repeats": 2})
    assert len(service.coordinator.notices) == 1
    assert service.coordinator.progress[-1] == (
        "Attempt 2 didn't pass: boom (2 in a row). Trying again.")
    await service.close()


async def test_service_development_settings_open_a_real_cycle_on_a_tree(service):
    """The settings the service builds must validate against A's real tree."""
    from recollect.selfmod.contracts import TaskContract
    from recollect.selfmod.round import ModificationRound, RoundConfig
    from recollect.selfmod.tests_first import parse_tests
    from tests.test_selfmod_tests_first import authored

    await service.prepare()
    tests = parse_tests(authored())
    contract = TaskContract("request", tests.contract_requirements, tests.names,
                            service.policy.sha256)
    round_ = ModificationRound.create(
        service._root / "real-cycle",
        RoundConfig("attempt-1", contract, service.baseline.sha256))
    try:
        development = round_.open_development(
            baseline=service.baseline, policy=service.policy,
            settings=service.development_settings())
        assert development.stage == "plan"
        # Role containers for planning and implementing must also accept the
        # real tree, the frozen checks and a plan that creates a new tool.
        from recollect.selfmod.contracts import Plan, PlannedChange, Verification
        from recollect.selfmod.roles import RoleSettings, make_context, role_spec

        profile = RoleSettings("http://127.0.0.1:8001/v1", "local", tests.checks)
        context = make_context(development, development.authorize("plan"), profile)
        role_spec(development, context, b"{}\n", profile, None)
        development._grants.clear()
        ids = tuple(r.id for r in contract.requirements)
        plan = Plan(contract.sha256, (
            PlannedChange("recollect/engine/subagent_tools/http_post.py", "create",
                          ids, "new tool"),
            PlannedChange("recollect/engine/mcp_research.py", "modify", ids,
                          "register the tool")),
            tuple(Verification(i, "checks") for i in ids))
        development._development.propose(development._id, plan)
        development._development._stage = "implement"
        context = make_context(development, development.authorize("execute"), profile)
        spec = role_spec(development, context, b"{}\n", profile, None)
        assert "source/recollect/engine/subagent_tools" in spec.policy.create_under
    finally:
        round_.close()
        await service.close()


async def test_workspace_describes_agent_steps_without_raw_internals(service):
    service.status = {"state": "running", "session_id": "session",
                      "task_id": "task-a", "continuation_task_id": None}
    plan = {"summary": "add a tool", "changes": [
        {"path": "recollect/engine/subagent_tools/post.py", "operation": "create"}]}
    for kind, data in (
        ("plan_review", {"attempt": 1, "approved": False,
                         "findings": [{"issue": "no timeout"}]}),
        ("plan_approved", {"attempt": 1, "plan": plan}),
        ("checks", {"attempt": 1, "passed": False, "results": [
            {"name": "a", "passed": True}, {"name": "b", "passed": False}]}),
        ("code_review", {"attempt": 1, "approved": True, "findings": []}),
        ("candidate", {"attempt": 1, "changes": ["create x.py"], "sha256": "0"}),
        ("gap_accepted", {"task_id": "task-a"}),
    ):
        await service._on_event(kind, data)
    assert [a["text"] for a in service.activity] == [
        "Plan review: changes requested: no timeout",
        "Plan approved: add a tool (create recollect/engine/subagent_tools/post.py)",
        "Checks (no network): 1/2 passed; failed: b",
        "Code review: approved.",
        "Candidate ready: create x.py",
    ]
    assert service.coordinator.notices == []
    await service.close()


async def test_a_finished_build_completes_the_original_task(service):
    await service.prepare()
    store = {"task-a": {"state": "blocked", "progress": "p", "result": None},
             "task-b": {"state": "completed", "progress": "HTTP 200",
                        "result": "HTTP 200"}}
    updates = []
    service.coordinator.store.get = lambda session_id, task_id: {
        "original_message": "book the room", **store[task_id]}
    service.coordinator.store.update = (
        lambda session_id, task_id, **changes: updates.append((task_id, changes)))

    class Resuming(Loop):
        async def run(self, gap):
            await service._on_event("resuming", {"attempt": 1, "task_id": "task-b"})
            return Outcome(True, "done")

    service.loops.append(Resuming(Outcome(True, "done")))
    await service.handle_gap("session", GAP)
    await service.decide("session", "task-a", True)
    assert (await service._runner).finished
    assert ("task-a", {"state": "completed", "progress": "HTTP 200",
                       "result": "HTTP 200"}) in updates
    assert [a["kind"] for a in service.activity][:2] == ["proposed", "decision"]
    await service.close()


async def test_an_answer_given_in_chat_updates_the_card_without_a_duplicate_notice(
        service):
    await service.prepare()
    await service.handle_gap("session", GAP)
    before = len(service.coordinator.notices)
    await service.decide("session", "task-a", True, announce=False)
    assert len(service.coordinator.notices) == before
    assert service.coordinator.progress[-1] == service_module.GAP_NOTICE
    await service._runner
    await service.close()
