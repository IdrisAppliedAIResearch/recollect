"""App wiring: A serves from its own bundle, and one gap runs one loop."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import recollect.selfmod.service as service_module
from recollect.engine.sandbox.manager import SandboxDeployment
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.deployment import Deployments, materialize_skills
from recollect.selfmod.loop import Outcome
from recollect.selfmod.paused import PausedBuild, load, save
from recollect.selfmod.service import SelfModificationService, _resume_paused_build
from recollect.selfmod.subagent_tree import change_policy
from recollect.selfmod.tests_first import parse_tests
from tests.selfmod_fake_images import BASE
from tests.test_selfmod_loop import Images
from tests.test_selfmod_tests_first import authored

REPOSITORY = Path(__file__).resolve().parents[1]
GAP = {"task_id": "task-a", "missing_capability": "calendar write",
       "modification_request": "add a calendar tool"}
BASELINE_TREE = Snapshot((File("recollect/__init__.py", b""),
                          File("recollect/engine/mcp_research.py", b"TOOLS = []\n")))


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
        self.seed = self.resume = None

    async def run(self, gap, seed=None):
        self.runs.append(gap)
        self.seed = seed
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

    def loop_factory(session_id, task_id, request, gap, resume=None):
        if not loops:
            loops.append(Loop(Outcome(True, "done")))
        loops[0].request = request
        loops[0].resume = resume
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
        async def run(self, gap, seed=None):
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
        return Promotion("abc123def4567", "selfmod-before-create-event-1", "0" * 40,
                         "selfmodifying-experiment", ("src/x.py",))

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
    assert service.status["commit"] == "abc123def4567"
    assert service.status["rollback_tag"] == "selfmod-before-create-event-1"
    assert service.activity[-1]["text"] == (
        "Committed on selfmodifying-experiment as abc123def456; it now serves new "
        "work. Roll back with: git reset --hard selfmod-before-create-event-1")
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
    assert "commit" not in service.status
    await service.close()


class FakeConnectors:
    """The issue #28 seam: a connector offered, granted, or failed on demand."""

    def __init__(self, found=None, error=None):
        self.found, self.error = found, error
        self.connected_ids = []

    def find(self, gap):
        return self.found

    def connected(self):
        return list(self.connected_ids)

    def __contains__(self, connector_id):
        return connector_id == "google_calendar"

    def get(self, connector_id):
        if connector_id != "google_calendar":
            raise KeyError(connector_id)
        return CALENDAR

    async def connect(self, connector_id):
        if self.error is not None:
            raise self.error
        self.connected_ids.append(connector_id)
        return {"connector_id": connector_id, "name": "Google Calendar",
                "tools": ["calendar_list_events", "calendar_create_event"]}

    async def close(self):
        pass


CALENDAR = SimpleNamespace(id="google_calendar", name="Google Calendar")


def resume_service(service, connectors):
    """A coordinator that accepts a continuation and finishes it completed."""
    service._connectors = connectors
    service._continuation_poll = 0
    submitted = []

    async def submit(session_id, client_id, brief, request, *,
                     parent_task_id=None, continuation=False):
        submitted.append({"client_id": client_id, "brief": brief,
                          "request": request, "parent_task_id": parent_task_id,
                          "continuation": continuation})
        return {"task_id": "task-cont"}

    async def release_held(session_id, task_id):
        submitted.append({"released": task_id})

    def get(session_id, task_id):
        if task_id == "task-cont":
            return {"state": "completed"}
        return {"original_message": "book the room"}

    service.coordinator.submit, service.coordinator.release_held = (submit,
                                                                    release_held)
    service.coordinator.store.get = get
    return submitted


async def test_a_gap_with_a_connector_offers_to_connect_instead_of_build(service):
    resume_service(service, FakeConnectors(found=CALENDAR))
    await service.prepare()
    assert await service.coordinator.on_gap("session", GAP) is None
    question = service_module.connect_notice(GAP, "Google Calendar")
    assert "Google Calendar can" in question and "allow it" in question
    assert service.coordinator.interrupts == [("task-a", question)]
    assert service.status["state"] == "awaiting_connect"
    assert service.proposal("session", "task-a") is None
    assert service.connect_proposal("session", "task-a") == {
        "connector": "google_calendar", "name": "Google Calendar",
        "missing_capability": "calendar write"}
    assert service.coordinator.connect_proposal == service.connect_proposal
    assert "connect_proposed" in kinds(service)
    assert not service.loops
    await service.close()


async def test_yes_connects_then_resumes_the_request_on_the_connector(service):
    submitted = resume_service(service, FakeConnectors(found=CALENDAR))
    await service.prepare()
    await service.handle_gap("session", GAP)
    assert await service.decide("session", "task-a", True) == {
        "task_id": "task-a", "build": "started"}
    await service._runner
    assert service._connectors.connected_ids == ["google_calendar"]
    assert not service.loops  # nothing was built
    brief = [entry for entry in submitted if "brief" in entry][0]
    assert brief["continuation"] and brief["parent_task_id"] == "task-a"
    assert brief["request"] == "book the room"
    assert "calendar_list_events" in brief["brief"]
    assert "don't report the same gap again" in brief["brief"]
    assert {"released": "task-cont"} in submitted
    assert service.status["state"] == "finished"
    assert "connect_decision" in kinds(service)
    assert "connect_succeeded" in kinds(service)
    assert "connect_resumed" in kinds(service)
    assert any("Connected to Google Calendar" in notice[3]
               for notice in service.coordinator.notices)
    await service.close()


async def test_declining_the_connection_says_what_stays_blocked(service):
    submitted = resume_service(service, FakeConnectors(found=CALENDAR))
    await service.prepare()
    await service.handle_gap("session", GAP)
    assert await service.decide("session", "task-a", False,
                                announce=False) == {
        "task_id": "task-a", "build": "declined"}
    assert service.status["state"] == "declined"
    assert not submitted and not service.loops
    assert service.coordinator.progress[-1] == service_module.connect_declined_notice(
        GAP, "Google Calendar")
    assert "calendar write" in service.coordinator.progress[-1]
    await service.close()


async def test_a_continuation_that_fails_gives_the_request_a_last_word(service):
    submitted = resume_service(service, FakeConnectors(found=CALENDAR))

    def failed_get(session_id, task_id):
        if task_id == "task-cont":
            return {"state": "blocked", "error": "the calendar said no."}
        return {"original_message": "book the room"}

    service.coordinator.store.get = failed_get
    await service.prepare()
    await service.handle_gap("session", GAP)
    assert await service.decide("session", "task-a", True) == {
        "task_id": "task-a", "build": "started"}
    await service._runner
    assert {"released": "task-cont"} in submitted
    assert service.status["state"] == "failed"
    assert service.coordinator.progress[-1] == (
        "Google Calendar is connected, but the request could not be finished: "
        "the calendar said no. Ask me again whenever you're ready.")
    await service.close()


async def test_a_failed_connect_fails_only_the_connection(service):
    connectors = FakeConnectors(
        found=CALENDAR,
        error=RuntimeError("The Google Calendar sign-in was not completed "
                           "in time."))
    submitted = resume_service(service, connectors)
    await service.prepare()
    await service.handle_gap("session", GAP)
    assert await service.decide("session", "task-a", True) == {
        "task_id": "task-a", "build": "started"}
    await service._runner
    assert service.status["state"] == "failed"
    assert "connect_failed" in kinds(service)
    assert not submitted  # the request was never resumed
    assert service.coordinator.progress[-1] == (
        "The connection to Google Calendar didn't complete: The Google "
        "Calendar sign-in was not completed in time. Ask me again whenever "
        "you're ready.")
    await service.close()


def running_status(service):
    service.status = {"state": "running", "session_id": "session",
                      "task_id": "task-a", "continuation_task_id": None}


async def test_a_connect_step_connects_the_service_and_reports_it(service):
    running_status(service)
    connectors = FakeConnectors()
    service._connectors = connectors
    response = await service._step(
        {"kind": "connect", "connector": "google_calendar", "why": "calendar"},
        session_id="session", task_id="task-a")
    assert connectors.connected_ids == ["google_calendar"]
    assert "Google Calendar is now connected" in response
    assert "calendar_list_events" in response
    texts = [n[3] for n in service.coordinator.notices]
    assert texts[0].startswith("To build this I need to connect Google Calendar")
    assert texts[-1] == "Connected to Google Calendar. Continuing the build."
    assert "step_connect_succeeded" in kinds(service)
    await service.close()


async def test_a_connect_step_without_a_connector_says_so(service):
    running_status(service)
    service._connectors = None
    response = await service._step(
        {"kind": "connect", "connector": "google_calendar"},
        session_id="session", task_id="task-a")
    assert "not available" in response
    assert not service.coordinator.notices
    await service.close()


async def test_a_failed_connect_step_tells_the_agent_to_go_on(service):
    running_status(service)
    service._connectors = FakeConnectors(
        error=RuntimeError("The Google Calendar sign-in was not completed "
                           "in time."))
    response = await service._step(
        {"kind": "connect", "connector": "google_calendar"},
        session_id="session", task_id="task-a")
    assert "did not complete" in response
    assert "step_connect_failed" in kinds(service)
    await service.close()


async def test_an_ask_step_waits_for_the_answer_then_resumes(service):
    running_status(service)
    step = {"kind": "ask", "question": "Which calendar should I use?"}
    task = asyncio.create_task(service._step(step, session_id="session",
                                             task_id="task-a"))
    for _ in range(100):
        if service._pending_step is not None:
            break
        await asyncio.sleep(0)
    assert service.status["pending_step"] == step
    assert "Which calendar should I use?" in service.coordinator.notices[-1][3]
    assert await service.answer_step("session", "task-a", "The work one") == {
        "task_id": "task-a", "answered": True}
    assert await task == "You answered: The work one"
    assert service._pending_step is None and "pending_step" not in service.status
    assert "step_ask_answered" in kinds(service)
    await service.close()


async def test_an_ask_step_with_no_question_does_not_wait(service):
    running_status(service)
    response = await service._step(
        {"kind": "ask", "question": "   "}, session_id="session",
        task_id="task-a")
    assert "without stating it" in response
    assert service._pending_step is None
    await service.close()


async def test_answer_step_rejects_when_nothing_is_waiting(service):
    running_status(service)
    with pytest.raises(ValueError):
        await service.answer_step("session", "task-a", "hi")
    await service.close()


async def test_answer_step_rejects_the_wrong_task(service):
    running_status(service)
    task = asyncio.create_task(service._step(
        {"kind": "ask", "question": "Which one?"},
        session_id="session", task_id="task-a"))
    for _ in range(100):
        if service._pending_step is not None:
            break
        await asyncio.sleep(0)
    with pytest.raises(ValueError):
        await service.answer_step("session", "other-task", "hi")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await service.close()


async def test_stopping_during_an_ask_step_clears_it(service):
    running_status(service)
    task = asyncio.create_task(service._step(
        {"kind": "ask", "question": "Which one?"},
        session_id="session", task_id="task-a"))
    for _ in range(100):
        if service._pending_step is not None:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service._pending_step is None and "pending_step" not in service.status
    await service.close()


def _pause(**changes):
    base = dict(session_id="session", task_id="task-a", request="book the room",
                gap=GAP, attempt=3, feedback=("attempt 1: boom",),
                plan={"summary": "s", "changes": []},
                step={"kind": "ask", "question": "which calendar?"},
                tree=Snapshot(()), tests=parse_tests(authored()),
                connections=None, baseline_sha256=BASELINE_TREE.sha256)
    base.update(changes)
    return PausedBuild(**base)


async def test_a_resumed_run_seeds_the_loop_and_clears_the_record(service):
    save(service._root, _pause())
    assert load(service._root) is not None
    outcome = await service._run("session", "task-a", GAP,
                                 resume=_pause())
    assert outcome == Outcome(True, "done")
    assert service.loops[0].resume is not None
    assert service.loops[0].seed == {
        "tests": service.loops[0].resume.tests, "attempt": 3,
        "feedback": ("attempt 1: boom",)}
    assert load(service._root) is None  # dropped once the build has run
    assert service.status["state"] == "finished"
    await service.close()


async def test_build_loop_wires_an_on_pause_that_persists_to_root(service):
    service.baseline = BASELINE_TREE
    service.policy = change_policy(BASELINE_TREE)

    async def _complete(*args, **kwargs):
        return "{}"

    service._completer = lambda *args, **kwargs: _complete
    loop = service._build_loop("session", "task-a", "book the room", GAP)
    on_pause = loop._develop._on_pause
    assert on_pause is not None
    step = {"kind": "ask", "question": "which calendar?"}
    tree = Snapshot((File("recollect/engine/subagent_tools/post.py", b"x\n"),))
    await on_pause(step, tree, {"summary": "s", "changes": []}, 2,
                   parse_tests(authored()), ("attempt 1: boom",))
    record = load(service._root)
    assert record is not None and record.step == step
    assert record.attempt == 2 and record.feedback == ("attempt 1: boom",)
    assert record.session_id == "session" and record.task_id == "task-a"
    assert record.baseline_sha256 == BASELINE_TREE.sha256
    assert record.tree == tree and record.tests == parse_tests(authored())
    await service.close()


async def test_resume_picks_up_a_matching_paused_build(service):
    service.baseline = BASELINE_TREE
    save(service._root, _pause())
    _resume_paused_build(service, service._root)
    for _ in range(10):
        await asyncio.sleep(0)
    assert service.loops[0].resume is not None
    assert service.loops[0].seed["attempt"] == 3
    assert load(service._root) is None  # consumed by the resumed run
    await service.close()


async def test_resume_abandons_a_paused_build_whose_tree_drifted(service):
    service.baseline = BASELINE_TREE
    save(service._root, _pause(baseline_sha256="ff" * 32))
    _resume_paused_build(service, service._root)
    assert service._loop is None  # nothing was scheduled
    assert load(service._root) is None
    assert "resume_abandoned" in kinds(service)
    await service.close()


async def test_a_resumed_build_is_stored_as_the_tracked_runner(service):
    """The resume runs through _runner, so stop() and close() can see it."""
    service.baseline = BASELINE_TREE
    save(service._root, _pause())
    _resume_paused_build(service, service._root)
    for _ in range(10):
        await asyncio.sleep(0)
    assert service._runner is not None
    outcome = await service._runner
    assert outcome.finished
    assert service.status["state"] == "finished"
    await service.close()


async def test_resume_abandons_while_a_build_is_in_flight(service):
    """A build started before the resume may claim the slot: only one runs."""
    service.baseline = BASELINE_TREE
    save(service._root, _pause())
    service._runner = asyncio.create_task(asyncio.sleep(60))
    _resume_paused_build(service, service._root)
    for _ in range(10):
        await asyncio.sleep(0)
    assert service._loop is None  # nothing was scheduled
    assert not service._runner.done()  # the in-flight build kept its slot
    assert load(service._root) is None  # the stale record is dropped
    assert "resume_abandoned" in kinds(service)
    service._runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await service._runner
    await service.close()
