"""The gap-to-finished-request loop: tests once, retry from A until B finishes."""

import pytest

from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.deployment import BundleImages, Deployment, Deployments
from recollect.selfmod.loop import DeploymentSwitch, Outcome, SelfModificationLoop
from recollect.selfmod.tests_first import parse_tests
from tests.selfmod_fake_images import FakeImages
from tests.test_selfmod_deployment import bundle
from tests.test_selfmod_tests_first import authored


def candidate(text):
    return Snapshot((File("dependencies.lock", b"httpx==0.28.1\n"),
                     File("extension.py", text.encode())))


class Events(list):
    async def __call__(self, kind, data):
        self.append((kind, data))


@pytest.fixture
def events():
    return Events()


def kinds(events):
    return [kind for kind, _ in events]


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


async def test_loop_freezes_tests_once_and_retries_with_feedback(events):
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
    loop = SelfModificationLoop(author_tests=author_tests, develop=develop,
                                switch=switch, retry_pause=0,
                                on_event=events)
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
    assert kinds(events).count("attempt_failed") == 3
    assert kinds(events)[-1] == "attempt_finished"


async def test_user_stop_ends_the_loop_without_a_new_attempt(events):
    tests = parse_tests(authored())
    loop = None

    async def author_tests(gap, stopped, record):
        return tests

    async def develop(attempt, frozen, feedback):
        loop.stop()
        raise RuntimeError("failed")

    switch = Switch([])
    loop = SelfModificationLoop(author_tests=author_tests, develop=develop,
                                switch=switch, retry_pause=0,
                                on_event=events)
    assert await loop.run({}) == Outcome(False, "stopped by the user", stopped=True)
    assert switch.resets == ["RuntimeError: failed"]
    assert kinds(events)[-1] == "loop_stopped"


async def receipt(value, image_id):
    fake = FakeImages()
    fake.add(value, image_id)
    return await BundleImages(fake).verify(value, image_id)


class Images:
    def __init__(self):
        self.fake, self.count, self.removed = FakeImages(), 0, []

    async def build(self, value):
        self.count += 1
        image = "sha256:" + format(self.count, "064x")
        self.fake.add(value, image)
        return image

    async def verify(self, value, image_id):
        return await BundleImages(self.fake).verify(value, image_id)

    async def remove(self, image_id):
        self.removed.append(image_id)

    async def sweep(self, keep):
        return []


class Coordinator:
    def __init__(self, deployments, states):
        self.deployments, self.states, self.calls = deployments, list(states), []
        self.store = self

    async def submit(self, session_id, request_id, objective, original_message,
                     effort="focused", parent_task_id=None, *, continuation=False):
        assert continuation and original_message == "the request"
        assert objective.startswith("A new tool was added so this request can be "
                                    "done: create_event, which provides calendar")
        task_id = "task-" + request_id
        self.deployments.link_continuation(task_id, parent_task_id)
        self.calls.append(("submit", task_id))
        return {"task_id": task_id}

    async def release_held(self, session_id, task_id):
        assert self.deployments.manager_for(task_id) is self.deployments.b.manager
        self.calls.append(("release", task_id))

    def get(self, session_id, task_id):
        return {"state": self.states.pop(0), "progress": "needs a question answered"}


async def test_switch_resumes_on_b_discards_a_failure_and_promotes_a_success():
    a = Deployment("A", await receipt(bundle(), "sha256:" + "b" * 64), object())
    deployments = Deployments(a)
    deployments.bind("task-a")
    images = Images()
    coordinator = Coordinator(deployments, ["running", "blocked", "completed"])
    staged, discarded, promoted = [], [], []

    def stage(receipt):
        staged.append(Deployment("B", receipt, object()))
        return staged[-1]

    async def discard(deployment):
        discarded.append(deployment)

    async def promote(value, tests):
        promoted.append(value)
        deployments.promote()

    switch = DeploymentSwitch(
        deployments=deployments, images=images, coordinator=coordinator,
        session_id="s", parent_task_id="task-a", request="the request",
        base_image_id=bundle().base_image_id, launch=(("entrypoint", "research"),),
        stage=stage, promote=promote, discard=discard, poll_seconds=0)
    tests, gap = parse_tests(authored()), {"missing_capability": "calendar"}
    failed = await switch.activate(1, candidate("first"), tests, gap)
    assert not failed.finished and "blocked" in failed.detail
    assert deployments.b is staged[0]
    await switch.reset(failed.detail)
    assert deployments.b is None and discarded == [staged[0]]
    await switch.reset("idempotent")
    assert discarded == [staged[0]]
    second = candidate("second")
    finished = await switch.activate(2, second, tests, gap)
    assert finished.finished and promoted == [second]
    assert deployments.a is staged[1] and deployments.b is None
    assert coordinator.calls == [
        ("submit", "task-selfmod-task-a-1"), ("release", "task-selfmod-task-a-1"),
        ("submit", "task-selfmod-task-a-2"), ("release", "task-selfmod-task-a-2")]


async def test_a_b_image_that_never_staged_is_removed_on_reset():
    a = Deployment("A", await receipt(bundle(), "sha256:" + "b" * 64), object())
    images = Images()

    def stage(receipt):
        raise RuntimeError("sandbox refused to start")

    switch = DeploymentSwitch(
        deployments=Deployments(a), images=images, coordinator=None,
        session_id="s", parent_task_id="task-a", request="the request",
        base_image_id=bundle().base_image_id, launch=(("entrypoint", "research"),),
        stage=stage, promote=None, discard=None, poll_seconds=0)
    with pytest.raises(RuntimeError):
        await switch.activate(1, candidate("x"), parse_tests(authored()), {})
    await switch.reset("sandbox refused to start")
    assert images.removed == ["sha256:" + format(1, "064x")]


async def test_canceled_resume_stops_and_events_reach_the_observer():
    tests = parse_tests(authored())
    events = []

    async def author_tests(gap, stopped, record):
        await record("tests_frozen", {"tests_sha256": tests.sha256,
                                      "tool_name": "create_event", "checks": 3})
        return tests

    async def develop(attempt, frozen, feedback):
        return candidate("x")

    async def on_event(kind, data):
        events.append(kind)

    switch = Switch([Outcome(False, "the resumed request was canceled", stopped=True)])
    loop = SelfModificationLoop(author_tests=author_tests, develop=develop,
                                switch=switch, retry_pause=0, on_event=on_event)
    outcome = await loop.run({})
    assert outcome.stopped and switch.activated == [1]
    assert switch.resets == ["stopped by the user"]
    assert events == ["loop_started", "tests_frozen", "attempt_started", "loop_stopped"]


async def test_identical_failures_back_off_and_a_new_reason_resets(events,
                                                                  monkeypatch):
    import recollect.selfmod.loop as loop_module

    tests = parse_tests(authored())
    pauses = []

    async def sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(loop_module.asyncio, "sleep", sleep)

    async def author_tests(gap, stopped, record):
        return tests

    reasons = iter(["same", "same", "same", "different", "different"])

    async def develop(attempt, frozen, feedback):
        reason = next(reasons, None)
        if reason is None:
            return candidate("ok")
        raise RuntimeError(reason)

    switch = Switch([Outcome(True, "done")])
    loop = SelfModificationLoop(author_tests=author_tests, develop=develop,
                                switch=switch, retry_pause=1.0,
                                on_event=events)
    assert (await loop.run({})).finished
    assert pauses == [1.0, 2.0, 4.0, 1.0, 2.0]
    failed = [data for kind, data in events if kind == "attempt_failed"]
    assert [f["repeats"] for f in failed] == [1, 2, 3, 1, 2]
    assert loop_module.MAX_PAUSE == 300.0
