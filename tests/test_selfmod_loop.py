"""The gap-to-finished-request loop: tests once, retry from A until B finishes."""

import pytest

from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.deployment import BundleImages, DeploymentRouter
from recollect.selfmod.journal import Journal
from recollect.selfmod.loop import DeploymentSwitch, Outcome, SelfModificationLoop
from recollect.selfmod.tests_first import parse_tests
from tests.selfmod_fake_images import FakeImages, verified
from tests.test_selfmod_deployment import bundle
from tests.test_selfmod_tests_first import authored


def candidate(text):
    return Snapshot((File("dependencies.lock", b"httpx==0.28.1\n"),
                     File("extension.py", text.encode())))


@pytest.fixture
def journal(tmp_path):
    value = Journal.create(tmp_path / "loop")
    yield value
    value.close()


def kinds(journal):
    return [r.value["kind"] for r in journal.verify()]


class Switch:
    def __init__(self, results):
        self.results, self.activated, self.resets = list(results), [], []

    async def activate(self, attempt, value, tests, gap):
        self.activated.append(attempt)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def reset(self, reason):
        self.resets.append(reason)


async def test_loop_freezes_tests_once_and_retries_with_feedback(journal):
    authoring_calls, developed = [], []
    tests = parse_tests(authored())

    async def author_tests(gap, stopped, record):
        authoring_calls.append(gap)
        if len(authoring_calls) == 1:
            raise OSError("model server unavailable")
        return tests

    async def develop(attempt, frozen, feedback):
        developed.append((attempt, frozen, feedback))
        if attempt == 1:
            raise RuntimeError("scope violation")
        return candidate(f"attempt {attempt}")

    switch = Switch([RuntimeError("B crashed"),
                     Outcome(False, "resumed request ended blocked"),
                     Outcome(True, "done")])
    loop = SelfModificationLoop(journal, author_tests=author_tests, develop=develop,
                                switch=switch, retry_pause=0)
    outcome = await loop.run({"missing_capability": "calendar"})
    assert outcome == Outcome(True, "done")
    assert len(authoring_calls) == 2
    assert all(frozen is tests for _, frozen, _ in developed)
    assert [a for a, _, _ in developed] == [1, 2, 3, 4]
    assert developed[3][2] == (
        "attempt 1: RuntimeError: scope violation",
        "attempt 2: RuntimeError: B crashed",
        "attempt 3: resumed request ended blocked")
    assert switch.activated == [2, 3, 4] and len(switch.resets) == 3
    assert kinds(journal).count("attempt_failed") == 3
    assert kinds(journal)[-1] == "attempt_finished"


async def test_user_stop_ends_the_loop_without_a_new_attempt(journal):
    tests = parse_tests(authored())
    loop = None

    async def author_tests(gap, stopped, record):
        return tests

    async def develop(attempt, frozen, feedback):
        loop.stop()
        raise RuntimeError("failed")

    switch = Switch([])
    loop = SelfModificationLoop(journal, author_tests=author_tests, develop=develop,
                                switch=switch, retry_pause=0)
    assert await loop.run({}) == Outcome(False, "stopped by the user")
    assert switch.resets == ["RuntimeError: failed"]
    assert kinds(journal)[-1] == "loop_stopped"


class Images:
    def __init__(self):
        self.fake, self.count = FakeImages(), 0

    async def build(self, value):
        self.count += 1
        image = "sha256:" + format(self.count, "064x")
        self.fake.add(value, image)
        return image

    async def verify(self, value, image_id):
        return await BundleImages(self.fake).verify(value, image_id)


class Sandboxes:
    def __init__(self):
        self.registered = []

    def register(self, role, manager, receipt):
        self.registered.append((role, receipt.image_id))


class Coordinator:
    def __init__(self, router, states):
        self.router, self.states, self.calls = router, list(states), []
        self.store = self

    async def submit(self, session_id, request_id, objective, original_message,
                     effort="focused", parent_task_id=None, *, continuation=False):
        assert continuation and original_message == "the request"
        assert objective.startswith("A new tool was added so this request can be "
                                    "done: create_event, which provides calendar")
        task_id = "task-" + request_id
        self.router.link_continuation(task_id, parent_task_id)
        self.calls.append(("submit", task_id))
        return {"task_id": task_id}

    async def release_held(self, session_id, task_id):
        assert self.router.route(task_id).role == "B"
        self.calls.append(("release", task_id))

    def get(self, session_id, task_id):
        return {"state": self.states.pop(0), "progress": "needs a question answered"}


@pytest.fixture
def router(tmp_path):
    with Journal.create(tmp_path / "routing") as routing:
        value = DeploymentRouter(routing)
        value.register_a(verified(bundle(), "sha256:" + "b" * 64))
        value.bind("task-a")
        yield value


async def test_switch_resumes_on_b_and_rolls_back_to_a_for_a_retry(router):
    coordinator = Coordinator(router, ["running", "blocked", "completed"])
    switch = DeploymentSwitch(
        router=router, sandboxes=Sandboxes(), images=Images(),
        coordinator=coordinator, session_id="s", parent_task_id="task-a",
        request="the request", base_image_id=bundle().base_image_id,
        launch=(("entrypoint", "research"),), manager_factory=lambda receipt: object(),
        poll_seconds=0,
    )
    tests, gap = parse_tests(authored()), {"missing_capability": "calendar"}
    failed = await switch.activate(1, candidate("first"), tests, gap)
    assert not failed.finished and "blocked" in failed.detail
    assert router.serving.role == "B"
    await switch.reset(failed.detail)
    assert router.serving.role == "A" and not router.live_b
    await switch.reset("idempotent")
    finished = await switch.activate(2, candidate("second"), tests, gap)
    assert finished.finished and router.serving.role == "B"
    assert coordinator.calls == [
        ("submit", "task-selfmod-task-a-1"), ("release", "task-selfmod-task-a-1"),
        ("submit", "task-selfmod-task-a-2"), ("release", "task-selfmod-task-a-2")]
