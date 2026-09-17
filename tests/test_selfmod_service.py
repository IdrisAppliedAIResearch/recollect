"""App wiring: A serves from its own bundle, and one gap runs one loop."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import recollect.selfmod.service as service_module
from recollect.engine.sandbox.manager import SandboxDeployment
from recollect.selfmod.deployment import Deployments, materialize_skills
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

    def loop_factory(session_id, task_id, request, gap):
        if not loops:
            loops.append(Loop(Outcome(True, "done")))
        loops[0].request = request
        return loops[0]

    value = SelfModificationService(
        SimpleNamespace(), coordinator, repository=REPOSITORY, root=tmp_path / "root",
        images=Images(), base_image_id=BASE,
        role_endpoint="http://127.0.0.1:8001/v1", role_model="local",
        manager_factory=manager, loop_factory=loop_factory, retire_poll=0,
    )
    value.coordinator, value.loops = coordinator, loops
    value.recorded = []
    value._record = lambda kind, data: value.recorded.append(kind)
    return value


def kinds(service):
    return service.recorded


async def test_prepare_serves_a_from_its_bundle_and_installs_the_gap_hook(service):
    verified = await service.prepare()
    assert service.deployments.a.role == "A"
    assert service.deployments.a.image_id == verified.image_id
    assert isinstance(service.coordinator.deployments, Deployments)
    assert service.coordinator.on_gap == service.handle_gap
    assert "a_serving" in kinds(service)
    await service.close()


async def test_a_later_start_clears_what_an_earlier_one_left(tmp_path, monkeypatch):
    """Directories and B images from an earlier run never survive a start."""
    monkeypatch.setattr(
        service_module, "SandboxManager",
        lambda config, **kwargs: SimpleNamespace(deployment=kwargs["deployment"]))
    root = tmp_path / "root"
    sandboxes = tmp_path / "sandboxes"
    for _ in range(2):
        value = SelfModificationService(
            SimpleNamespace(sandbox_root=sandboxes), Coordinator(),
            repository=REPOSITORY, root=root,
            images=Images(), base_image_id=BASE,
            role_endpoint="http://127.0.0.1:8001/v1", role_model="local",
        )
        assert (await value.prepare()).image_id
        assert value.deployments.a.role == "A"
        await value.close()
    # Sandbox trees live outside the app data directory, never in the repo.
    assert len(list((sandboxes / "selfmod").glob("skills-a-*"))) == 1
    assert not list(root.glob("**/skills-a-*"))


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


class Manager:
    def __init__(self, deployment):
        self.deployment, self.torn_down = deployment, False

    async def teardown(self):
        self.torn_down = True


async def test_prepare_removes_every_other_bundle_image(service):
    service._images.sweep_calls = []

    async def sweep(keep):
        service._images.sweep_calls.append(keep)
        return ["sha256:" + "9" * 64]

    service._images.sweep = sweep
    stale = service._workspace / "root-b-old"
    stale.mkdir(parents=True)
    verified = await service.prepare()
    assert service._images.sweep_calls == [verified.image_id]
    assert not stale.exists()
    await service.close()


async def promoted_service(service, tmp_path, saved):
    from recollect.selfmod.contracts import File, Snapshot
    from recollect.selfmod.deployment import Deployment
    from recollect.selfmod.tests_first import parse_tests
    from tests.test_selfmod_tests_first import authored

    service._promote_files = saved
    service.status = {"state": "running", "session_id": "session",
                      "task_id": "task-a", "continuation_task_id": None,
                      "feature": "create event"}
    await service.prepare()
    old = service.deployments.a
    old.manager = Manager(old.manager.deployment)
    candidate = Snapshot((*service.baseline.files,
                          File("recollect/engine/subagent_tools/event.py", b"x\n")))
    b = Deployment("B", old.verified, Manager(None))
    service.deployments.stage_b(b)
    await service._promote(candidate, parse_tests(authored()))
    await asyncio.gather(*service._retiring)
    return old, b, candidate


async def test_a_finished_b_is_saved_on_a_branch_and_replaces_a(service, tmp_path):
    from recollect.selfmod.promotion import Promotion

    calls = []

    def saved(repository, baseline, candidate, feature):
        calls.append((repository, feature))
        return Promotion("selfmod/create-event-1", "main", "abc", ("src/x.py",))

    removed = []

    async def remove(image_id):
        removed.append(image_id)

    service._images.remove = remove
    old, b, candidate = await promoted_service(service, tmp_path, saved)
    assert calls == [(REPOSITORY, "create event")]
    assert service.deployments.a is b and b.role == "A"
    assert service.baseline is candidate
    assert service.policy.permits("recollect/engine/subagent_tools/event.py",
                                  "modify")
    # The replaced A leaves nothing: sandbox, image and directories are gone.
    assert old.manager.torn_down and removed == [old.image_id]
    assert not any(path.exists() for path in old.paths)
    assert service.status["branch"] == "selfmod/create-event-1"
    assert service.activity[-1]["text"].startswith(
        "Saved as branch selfmod/create-event-1")
    await service.close()


async def test_a_failed_save_still_serves_b_until_restart(service, tmp_path):
    from recollect.selfmod.promotion import PromotionError

    def saved(*args):
        raise PromotionError("git commit failed: no identity")

    async def remove(image_id):
        pass

    service._images.remove = remove
    _, b, _ = await promoted_service(service, tmp_path, saved)
    assert service.deployments.a is b
    assert "no identity" in service.activity[-1]["text"]
    assert "branch" not in service.status
    await service.close()
