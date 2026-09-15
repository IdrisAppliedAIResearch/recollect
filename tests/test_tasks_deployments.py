"""Deployment-aware tasks: pinned A/B sandboxes and held original-task continuation."""

import asyncio

import pytest

from recollect import tasks as tasks_module
from recollect.engine.sandbox.manager import SandboxDeployment, SandboxManager
from recollect.engine.sandbox.runner import TaskReport
from recollect.engine.subagent import SubagentResult
from recollect.selfmod.deployment import (
    DeploymentRouter,
    DeploymentSandboxes,
    TaskDeployments,
    materialize_skills,
)
from recollect.selfmod.journal import IntegrityError, Journal
from tests.selfmod_fake_images import verified
from tests.test_selfmod_deployment_sandboxes import IMAGE_A, IMAGE_B, bundle
from tests.test_tasks import environment, submit, wait_state  # noqa: F401


@pytest.fixture
def routed(environment, tmp_path, monkeypatch):  # noqa: F811 - pytest fixture
    state = environment
    journal = Journal.create(tmp_path / "routing")
    router = DeploymentRouter(journal)
    sandboxes = DeploymentSandboxes(router)
    config = state.coordinator.config

    def manager(image, name, source):
        skills = materialize_skills(source, tmp_path / ("skills-" + name))
        return SandboxManager(config, deployment=SandboxDeployment(
            image, skills, tmp_path / ("root-" + name)))

    receipt_a = verified(bundle(), IMAGE_A)
    router.register_a(receipt_a)
    state.a_manager = manager(IMAGE_A, "a", bundle())
    sandboxes.register("A", state.a_manager, receipt_a)
    state.b_bundle = bundle(b"B generated reporting\n")
    state.receipt_b = verified(state.b_bundle, IMAGE_B)
    state.router, state.sandboxes, state.managers = router, sandboxes, []
    state.b_manager_factory = lambda: manager(IMAGE_B, "b", state.b_bundle)
    state.coordinator.deployments = TaskDeployments(router, sandboxes)

    class Runner:
        def __init__(self, used, config):
            state.managers.append(used)

        async def run_continuous(self, session_id, task, **kwargs):
            state.calls.append((session_id, task))
            # Report IDs are unique across a conversation's task mailbox.
            await kwargs["report"](TaskReport(
                "accepted", "Accepted", kwargs["revision"],
                call_id=f"accept-{len(state.calls)}"))
            yield SubagentResult("task", "ok", "{}", "Finished")

    monkeypatch.setattr(tasks_module, "OpenCodeRunner", Runner)
    yield state
    journal.close()


async def test_new_work_binds_to_a_and_runs_on_its_pinned_sandbox(routed):
    state = routed
    await state.coordinator.start()
    task = await submit(state)
    await wait_state(state, task["task_id"], "completed")
    assert state.router._tasks[task["task_id"]] == ("A", 1)
    assert state.managers == [state.a_manager]


async def test_continuation_is_held_until_commit_and_release_then_runs_on_b(routed):
    state = routed
    await state.coordinator.start()
    original = await submit(state, "original")
    await wait_state(state, original["task_id"], "completed")
    state.router.stage_b(state.receipt_b)
    b_manager = state.b_manager_factory()
    state.sandboxes.register("B", b_manager, state.receipt_b)
    state.router.begin_activation()
    held = await submit(state, "continue-original",
                        parent_task_id=original["task_id"], continuation=True)
    await asyncio.sleep(0.05)
    current = state.store.get(state.session_id, held["task_id"])
    assert current["state"] == "queued" and len(state.calls) == 1
    assert state.coordinator._has_owner(current)
    with pytest.raises(IntegrityError, match="committed B"):
        state.router.release_continuation(held["task_id"])
    state.router.commit()
    state.router.release_continuation(held["task_id"])
    await state.coordinator.release_held(state.session_id, held["task_id"])
    await wait_state(state, held["task_id"], "completed")
    assert state.managers == [state.a_manager, b_manager]
    with pytest.raises(ValueError, match="held"):
        await state.coordinator.release_held(state.session_id, held["task_id"])


async def test_uncommitted_b_work_is_blocked_not_served(routed):
    state = routed
    await state.coordinator.start()
    state.router.stage_b(state.receipt_b)
    state.sandboxes.register("B", state.b_manager_factory(), state.receipt_b)
    state.router.begin_activation()
    task = await submit(state, "new-during-activation")
    blocked = await wait_state(state, task["task_id"], "blocked")
    assert "activation commit" in blocked["error"] and not state.managers


async def test_held_continuation_requires_deployments_and_a_parent(routed):
    state = routed
    with pytest.raises(ValueError, match="parent"):
        await submit(state, "no-parent", continuation=True)
    state.coordinator.deployments = None
    with pytest.raises(ValueError, match="deployment"):
        await submit(state, "no-deployments", parent_task_id="x", continuation=True)


def test_task_deployments_accept_only_a_router_and_its_own_selector(tmp_path):
    first = Journal.create(tmp_path / "one")
    second = Journal.create(tmp_path / "two")
    try:
        router = DeploymentRouter(first)
        with pytest.raises(IntegrityError, match="router"):
            TaskDeployments(router, DeploymentSandboxes(DeploymentRouter(second)))
        TaskDeployments(router, DeploymentSandboxes(router))
    finally:
        first.close()
        second.close()
