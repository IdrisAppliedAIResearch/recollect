"""Production adapter qualification; real Docker, synthetic non-target workers.

Faults hide responses only after the native command has acted. They do not stop
the shared daemon, change its configuration, or stand in for kernel evidence.
"""

import asyncio
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from recollect.selfmod.clock import current_stamp
from recollect.selfmod.containment import verify_ready
from recollect.selfmod.contracts import (
    File,
    Plan,
    PlannedChange,
    Requirement,
    Snapshot,
    TaskContract,
    Verification,
)
from recollect.selfmod.docker_runtime import DockerFixtureRuntime
from recollect.selfmod.executor import FixtureExecutor
from recollect.selfmod.integration import DevelopmentSettings
from recollect.selfmod.journal import IntegrityError, decode, encode, sha256
from recollect.selfmod.process import PipeCommand
from recollect.selfmod.round import ModificationRound
from tests.selfmod_containment_helpers import spec
from tests.selfmod_round_helpers import round_config, submitted
from tests.test_selfmod_containment_docker import docker
from tests.test_selfmod_integration import checks, review

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RECOLLECT_RUN_SELFMOD_DOCKER_TESTS") != "1",
        reason="opt in to real selfmod Docker qualification",
    ),
]

CHANGE = b"from pathlib import Path\nPath('editable.py').write_bytes(b'value = 2\\n')\n"
WAIT = b"import time\ntime.sleep(60)\n"


class ObservedCommand:
    """Keep the native transport; inject only a named post-operation fault."""

    def __init__(self, owner, argv, deadline, **kwargs):
        self.owner, self.args = owner, argv[5:]
        self.result = self.ready = self.sent = None
        self.command = PipeCommand(argv, deadline, **kwargs)
        owner.commands.append(self)

    def finish(self):
        result = self.command.finish()
        self.result = result
        self.owner.hook(self.args[0], self)
        return result

    def line(self, limit):
        result = self.command.line(limit)
        self.ready = result
        self.owner.hook("ready", self)
        return result

    def send(self, record):
        self.command.send(record)
        self.sent = record
        self.owner.hook("release", self)

    def close(self):
        self.command.close()

    def evidence(self):
        return self.command.evidence()


class TransportProbe:
    def __init__(self):
        self.commands = []
        self.faults = []
        self.hook = lambda *_: None

    def __call__(self, argv, deadline, **kwargs):
        return ObservedCommand(self, argv, deadline, **kwargs)

    def lose(self, point):
        def fault(kind, command):
            if kind == point:
                # One lost response, never a retry of the requested operation.
                self.hook = lambda *_: None
                self.faults.append((point, command))
                raise OSError("injected lost response: " + point)

        self.hook = fault


async def cleanup_runtime(runtime, ids, observe):
    """A failed pipe close must not bypass independent container reconciliation."""
    errors = []
    if runtime._attachment is not None:
        try:
            await asyncio.to_thread(runtime._attachment.close)
        except Exception as exc:
            errors.append(exc)
    fixture = runtime._spec
    if fixture is not None:
        try:
            identities = await ids(fixture)
        except Exception as exc:
            errors.append(exc)
            return errors
        for identity in identities:
            try:
                value = json.loads(await observe("inspect", identity))[0]
                assert len(identity) == 64 and value["Id"] == identity
                assert value["Name"] == "/" + fixture.name
                assert value["Image"] == fixture.image_id
                assert value["Config"]["Labels"]["recollect.selfmod"] == fixture.run_id
                assert value["Config"]["Labels"]["recollect.spec"] == fixture.sha256
                if value["State"]["Running"]:
                    await observe("kill", "--signal=KILL", identity)
                value = json.loads(await observe("inspect", identity))[0]
                assert value["State"]["Status"] in {"created", "exited"}
                assert value["State"]["Pid"] == 0 and not value["State"]["Running"]
                await observe("rm", identity)
            except Exception as exc:
                errors.append(exc)
        try:
            assert not await ids(fixture)
        except Exception as exc:
            errors.append(exc)
    return errors


@pytest.fixture
async def live(tmp_path):
    endpoint = (
        await docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    ).decode().strip()
    assert (
        await docker("--host", endpoint, "info", "--format", "{{.OSType}}")
    ).strip() == b"linux"
    image = json.loads(
        await docker(
            "--host", endpoint, "image", "inspect",
            "recollect-opencode-sandbox:1.18.18",
        )
    )[0]
    assert not image["Config"].get("Volumes")
    executable = Path(shutil.which("docker")).resolve()
    share = (
        Path(os.environ["LOCALAPPDATA"]) / "recollect" / "sandboxes"
        if os.name == "nt" else tmp_path
    ).resolve()
    share.mkdir(parents=True, exist_ok=True)
    root = share / ("selfmod-runtime-qualification-" + uuid.uuid4().hex)
    root.mkdir()
    runtimes, executors, controllers = [], [], []

    async def observe(*args):
        return await docker("--host", endpoint, *args)

    def make_spec(code=CHANGE, timeout_ms=15_000):
        return replace(
            spec(code, timeout_ms=timeout_ms), run_id=uuid.uuid4().hex,
            image_id=image["Id"],
            image_environment=tuple(image["Config"].get("Env") or ()),
        )

    def runtime(probe=None):
        result = DockerFixtureRuntime(
            executable, root, endpoint, command_factory=probe or PipeCommand,
        )
        runtimes.append(result)
        return result

    def executor(fixture, *, unbounded=False):
        started = current_stamp()
        result = FixtureExecutor.create(
            tmp_path / ("executor-" + fixture.run_id), fixture,
            original_started=started,
            deadline_ns=(None if unbounded else
                         started.monotonic_ns + 120_000_000_000), max_refreshes=1,
        )
        executors.append(result)
        return result

    async def ids(fixture):
        result = set()
        for selector in (
            "label=recollect.selfmod=" + fixture.run_id,
            "name=^/" + fixture.name + "$",
        ):
            raw = await docker(
                "--host", endpoint, "container", "ls", "--all", "--no-trunc",
                "--filter", selector, "--format", "{{.ID}}",
            )
            result.update(raw.decode().splitlines())
        return result

    (tmp_path / "environment.json").write_bytes(encode({
        "image_id": image["Id"], "endpoint": endpoint,
        "executable": str(executable), "shared_root": str(root),
    }))
    yield SimpleNamespace(
        spec=make_spec, runtime=runtime, executor=executor, ids=ids,
        controllers=controllers, archive=tmp_path,
    )
    # Independent emergency teardown is not qualification success. Fault tests
    # assert the production outcome before this removes any test-owned leftovers.
    errors = []
    for runtime in reversed(runtimes):
        errors.extend(await cleanup_runtime(runtime, ids, observe))
    for resource in [*executors, *controllers]:
        try:
            resource.close()
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise ExceptionGroup("Qualification teardown failed; retained " + str(root),
                             errors)
    assert root.resolve().parent == share
    assert root.name.startswith("selfmod-runtime-qualification-")
    assert not root.is_symlink() and not root.is_junction()
    await asyncio.to_thread(shutil.rmtree, root)


def records(runner, kind):
    return [r for r in runner.journal.verify() if r.value["kind"] == kind]


def assert_faults(probe, point, fixture, runtime):
    assert probe.faults, "The intended native response loss was not injected"
    for kind, command in probe.faults:
        assert kind == point
        if point == "ready":
            supervisor = next(f.content for f in runtime._inputs.files
                              if f.path == "supervisor.py")
            verify_ready(command.ready, fixture, sha256(supervisor))
        elif point == "release":
            assert command.sent is not None
            assert decode(command.sent)["run_id"] == fixture.run_id
        else:
            assert command.result is not None and command.result[2] == 0
            if point == "create":
                identity = command.result[0].decode().strip()
                assert identity == runtime._termination.container_id


def changed(fixture):
    return Snapshot(tuple(
        File(f.path, b"value = 2\n" if f.path == "editable.py" else f.content)
        for f in sorted(fixture.baseline.files, key=lambda f: f.path)
    ))


async def test_production_adapter_exact_snapshot_and_owned_removal(live):
    fixture = live.spec()
    runner, runtime = live.executor(fixture), live.runtime()
    receipt = await runner.run_async(runtime)
    assert receipt.snapshot == changed(fixture)
    assert not receipt.diagnostic_only and not runner.primary_failed
    assert runner.verified_receipt(receipt)[-1].anchor == receipt.archive_anchor
    assert records(runner, "release_intent") and records(runner, "release_sent")
    assert records(runner, "termination_verified")
    assert not await live.ids(fixture)


async def test_unbounded_startup_and_quiet_work_survive_old_limits(live):
    fixture = live.spec(b"import time\ntime.sleep(61)\n" + CHANGE, timeout_ms=None)
    probe = TransportProbe()

    def slow_release(point, command):
        if point == "ready":
            # Laboratory delay only; active startup must not expire at ten seconds.
            time.sleep(11)

    probe.hook = slow_release
    runner, runtime = live.executor(fixture, unbounded=True), live.runtime(probe)
    started = time.monotonic()
    receipt = await runner.run_async(runtime)
    assert time.monotonic() - started >= 72
    assert receipt.snapshot == changed(fixture)
    assert not runner.primary_failed and not await live.ids(fixture)
    release = next(c.sent for c in probe.commands if c.sent is not None)
    assert decode(release)["remaining_ms"] is None


@pytest.mark.parametrize("point", ["create", "ready", "release"])
async def test_lost_native_response_reconciles_without_primary_retry(live, point):
    fixture, probe = live.spec(WAIT), TransportProbe()
    probe.lose(point)
    runner = live.executor(fixture)
    runtime = live.runtime(probe)
    with pytest.raises(OSError, match="injected lost response"):
        await runner.run_async(runtime)
    assert len(probe.faults) == 1
    assert_faults(probe, point, fixture, runtime)
    assert runner.primary_failed and not records(runner, "snapshot_verified")
    assert records(runner, "termination_verified")
    assert records(runner, "fixture_failure_accounted")
    assert not await live.ids(fixture)
    assert runner.authorize_refresh().failed_run_id == fixture.run_id
    assert sum(c.args[0] == "create" for c in probe.commands) == 1
    removals = [c.args for c in probe.commands if c.args[0] == "rm"]
    assert len(removals) == 1 and len(removals[0][1]) == 64
    assert all("--force" not in c.args and "-f" not in c.args for c in probe.commands)


async def test_native_execution_deadline_never_delivers_snapshot(live):
    fixture = live.spec(WAIT, timeout_ms=3000)
    runner = live.executor(fixture)
    started = time.monotonic()
    with pytest.raises((TimeoutError, IntegrityError), match="deadline"):
        await runner.run_async(live.runtime())
    assert time.monotonic() - started < 10  # Includes independent 3s cleanup.
    assert records(runner, "release_sent"), "Must reach the running-worker deadline"
    assert records(runner, "termination_verified")
    assert runner.primary_failed and not records(runner, "snapshot_verified")
    assert not await live.ids(fixture)


async def test_cancel_waits_for_real_cleanup_despite_repeated_cancel(live):
    fixture, probe = live.spec(WAIT), TransportProbe()
    released, stopping, proceed = (threading.Event() for _ in range(3))

    def hook(point, command):
        if point == "release":
            released.set()
        elif point == "container" and not stopping.is_set():
            stopping.set()
            assert proceed.wait(2), "test cleanup barrier timed out"

    probe.hook = hook
    runner = live.executor(fixture)
    task = asyncio.create_task(runner.run_async(live.runtime(probe)))
    try:
        assert await asyncio.to_thread(released.wait, 8)
        task.cancel()
        assert await asyncio.to_thread(stopping.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(IntegrityError, match="in progress"):
            runner.authorize_refresh()
    finally:
        proceed.set()
        if not task.done():
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    failure = records(runner, "fixture_failure_accounted")[-1].value["data"]
    assert failure["error_type"] == "CallerCancelled"
    assert failure["termination_confirmed"] is True
    assert runner.primary_failed and not records(runner, "snapshot_verified")
    assert not await live.ids(fixture)


@pytest.mark.parametrize("point", ["container", "rm"])
async def test_lost_cleanup_response_blocks_receipt_and_refresh(live, point):
    fixture, probe = live.spec(), TransportProbe()
    # Keep failing this observation: the executor's failure cleanup must not
    # turn absence after an uncertain removal into confirmed primary success.
    def hook(kind, command):
        if kind == point:
            probe.faults.append((point, command))
            raise OSError("injected cleanup response loss")

    probe.hook = hook
    runner = live.executor(fixture)
    runtime = live.runtime(probe)
    with pytest.raises(IntegrityError, match="termination remains uncertain"):
        await runner.run_async(runtime)
    assert_faults(probe, point, fixture, runtime)
    if point == "rm":
        assert len(probe.faults) == 1
    assert runner.primary_failed and not records(runner, "snapshot_verified")
    observed = records(runner, "termination_observed")[-1]
    assert observed.value["data"]["confirmed"] is False
    assert not records(runner, "termination_verified")
    assert records(runner, "fixture_failure_accounted")[-1].value["data"][
        "termination_confirmed"
    ] is False
    assert any(f.path.startswith("cli/") for f in observed.files.files)
    with pytest.raises(IntegrityError, match="confirmed stop"):
        runner.authorize_refresh()
    remaining = await live.ids(fixture)
    assert bool(remaining) is (point == "container")


async def test_live_diagnostic_refresh_cannot_restore_primary_eligibility(live):
    fixture, probe = live.spec(), TransportProbe()
    probe.lose("create")
    runner = live.executor(fixture)
    deadline = runner._deadline_ns
    first_runtime = live.runtime(probe)
    with pytest.raises(OSError):
        await runner.run_async(first_runtime)
    assert len(probe.faults) == 1
    assert_faults(probe, "create", fixture, first_runtime)
    grant = runner.authorize_refresh()
    runner.refresh(grant)
    assert runner.spec.run_id != fixture.run_id
    assert runner.spec.binding.attempt_id != fixture.binding.attempt_id
    assert runner.spec.binding.instance_id != fixture.binding.instance_id
    assert runner.spec.baseline == fixture.baseline
    assert runner.spec.policy == fixture.policy
    assert runner.spec.timeout_ms == fixture.timeout_ms
    assert runner._deadline_ns == deadline
    with pytest.raises(IntegrityError, match="consumed refresh"):
        runner.refresh(grant)
    next_runtime = live.runtime()
    assert next_runtime is not first_runtime
    receipt = await runner.run_async(next_runtime)
    assert receipt.snapshot == changed(fixture)
    assert receipt.diagnostic_only and runner.primary_failed
    with pytest.raises(IntegrityError, match="primary result"):
        runner.verified_receipt(receipt)
    with pytest.raises(IntegrityError, match="confirmed stop"):
        runner.authorize_refresh()
    assert not await live.ids(fixture) and not await live.ids(runner.spec)
    assert first_runtime._termination.container_id != next_runtime._worker.container_id


async def test_failed_replacement_exhausts_live_refresh_limit(live):
    runner = live.executor(live.spec())
    identities = []
    for attempt in range(2):
        probe = TransportProbe()
        probe.lose("create")
        runtime = live.runtime(probe)
        with pytest.raises(OSError, match="injected lost response"):
            await runner.run_async(runtime)
        assert len(probe.faults) == 1
        assert_faults(probe, "create", runner.spec, runtime)
        assert runner.primary_failed and not await live.ids(runner.spec)
        assert records(runner, "fixture_failure_accounted")[-1].value["data"][
            "termination_confirmed"
        ] is True
        identities.append(runtime._termination.container_id)
        if attempt == 0:
            runner.refresh(runner.authorize_refresh())
    assert len(set(identities)) == 2
    with pytest.raises(IntegrityError, match="confirmed stop"):
        runner.authorize_refresh()
    assert not records(runner, "snapshot_verified")


def development(live, fixture):
    contract = TaskContract(
        "Change the fixture's allowed value", (
            Requirement("value", "value equals 2", "fixture evaluator"),
        ), ("unit", "regression"), fixture.policy.sha256,
    )
    controller = ModificationRound.create(
        live.archive / "controller", round_config(contract, fixture.baseline.sha256),
    )
    live.controllers.append(controller)
    dev = controller.open_development(
        baseline=fixture.baseline, policy=fixture.policy,
        settings=DevelopmentSettings(
            fixture.image_id, fixture.image_environment, fixture.entrypoint,
        ),
    )
    plan = Plan(contract.sha256, (
        PlannedChange("editable.py", "modify", ("value",), "original task"),
    ), (Verification("value", "unit and independent evaluation"),))
    dev.propose(dev.authorize("plan"), plan)
    review(dev)  # Synthetic review; no claim of authenticated model processes.
    return controller, dev


async def test_real_executor_bytes_reach_the_submitted_candidate(live):
    fixture = live.spec()
    controller, dev = development(live, fixture)
    runtime = live.runtime()
    await dev.execute(dev.authorize("execute"), runtime)
    checks(dev)
    review(dev)
    number, identity = dev.submit(dev.authorize("submit"))
    assert number == 1 and identity == changed(fixture).sha256
    cp2 = submitted(controller)
    assert cp2["candidate/editable.py"] == b"value = 2\n"
    assert cp2["candidate/protected.py"] == b"protected original bytes"
    executions = [r for r in controller.journal.verify()
                  if r.value["kind"] == "development_execution"]
    assert [r.value["data"]["state"] for r in executions] == ["claimed", "verified"]
    for record in executions:
        if record.value["data"]["state"] == "verified":
            assert any(f.path.endswith("attachment.stdout") for f in record.files.files)
        for file in record.files.files:
            assert cp2[f"records/{record.anchor.sequence}/{file.path}"] == file.content
    assert not await live.ids(runtime._spec)


async def test_live_cancel_fails_round_without_candidate(live):
    controller, dev = development(live, live.spec(WAIT))
    probe, released = TransportProbe(), threading.Event()
    probe.hook = lambda point, _: released.set() if point == "release" else None
    runtime = live.runtime(probe)
    task = asyncio.create_task(dev.execute(dev.authorize("execute"), runtime))
    try:
        assert await asyncio.to_thread(released.wait, 8)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not controller.eligible
    with pytest.raises(AssertionError, match="no candidate"):
        submitted(controller)
    failures = [r for r in controller.journal.verify()
                if r.value["kind"] == "development_failure"]
    assert failures and failures[-1].value["data"]["termination_confirmed"] is True
    assert failures[-1].value["data"]["capture_complete"] is True
    assert any(b'"error_type":"CallerCancelled"' in f.content
               for f in failures[-1].files.files)
    assert not await live.ids(runtime._spec)
