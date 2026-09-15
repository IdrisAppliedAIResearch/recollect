"""Host lifecycle faults using fake runtime calls; no Docker or host workers."""

import asyncio
from dataclasses import replace

import pytest

from recollect.selfmod.containment import MAX_WIRE_BYTES, frozen_input, wire_binding
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.executor import (
    Collected,
    FixtureExecutor,
    Prepared,
    Termination,
)
from recollect.selfmod.journal import IntegrityError, encode, sha256
from tests.selfmod_containment_helpers import CONTAINER_ID, inspection, report, spec
from tests.selfmod_round_helpers import FakeClock, Fault


class Runtime:
    def __init__(self, root, clock):
        self.root, self.clock = root, clock
        root.mkdir()
        self.calls = []
        self.hook = lambda _: None
        self.status = "created"
        self.clean_stop = True
        self.result_change = lambda value: value
        self.inspection_change = lambda value: value

    def step(self, name):
        self.calls.append(name)
        self.hook(name)

    def prepare(self, config, inputs, deadline):
        self.config, self.inputs, self.deadline = config, inputs, deadline
        self.status = "created"
        self.step("prepare")
        return Prepared(CONTAINER_ID, self.root)

    def inspect(self, worker, deadline):
        self.step("inspect_" + self.status)
        result = inspection(self.root, self.config)
        result["State"] = {
            "Status": self.status,
            "Running": self.status == "running",
            "Pid": 1000 if self.status == "running" else 0,
            "ExitCode": 0,
            "OOMKilled": False,
        }
        return self.inspection_change(result)

    def start(self, worker, deadline):
        self.step("start")
        self.status = "running"
        supervisor = next(
            f.content for f in self.inputs.files if f.path == "supervisor.py"
        )
        self.supervisor_sha = sha256(supervisor)
        return encode(
            {"kind": "ready", **wire_binding(self.config, self.supervisor_sha)}
        )

    def read_inputs(self, worker, deadline):
        self.step("read_inputs")
        return self.inputs

    def release(self, worker, record, deadline):
        self.step("release")
        self.release_bytes = record

    def collect(self, worker, limit, deadline):
        assert limit == MAX_WIRE_BYTES
        self.step("collect")
        self.status = "exited"
        value = report(self.config)
        value["supervisor_sha256"] = self.supervisor_sha
        return Collected(encode(self.result_change(value)), 0)

    def terminate(self, config, worker, timeout_ms):
        assert timeout_ms == 3000
        self.step("terminate")
        self.status = "exited"
        return Termination(
            config.run_id,
            config.sha256,
            CONTAINER_ID if worker else None,
            self.clean_stop,
            Snapshot((File("stop.txt", b"fake namespace stopped"),)),
        )


@pytest.fixture
def setup(tmp_path):
    clock, fault = FakeClock(), Fault()
    runner = FixtureExecutor.create(
        tmp_path / "archive",
        spec(),
        original_started=clock(),
        deadline_ns=clock.ns + 20_000_000_000,
        max_refreshes=1,
        clock=clock,
        fault=fault,
    )
    runtime = Runtime(tmp_path / "inputs", clock)
    yield runner, runtime, clock, fault
    runner.close()


def records(runner, kind):
    return [r for r in runner.journal.verify() if r.value["kind"] == kind]


def fail_collect(runner, runtime):
    def hook(name):
        if name == "collect":
            raise OSError("fake worker failed")

    runtime.hook = hook
    with pytest.raises(OSError):
        runner.run(runtime)
    runtime.hook = lambda _: None


def test_success_is_archived_quiescent_snapshot_not_deployment(setup):
    runner, runtime, _, _ = setup
    receipt = runner.run(runtime)
    assert receipt.snapshot == runner.spec.baseline
    assert receipt.archive_anchor == runner.journal.head
    assert receipt.diagnostic_only is False
    assert not runner.primary_failed
    kinds = [r.value["kind"] for r in runner.journal.verify()]
    assert kinds == [
        "executor_claimed",
        "executor_opened",
        "run_consumed",
        "release_intent",
        "release_sent",
        "report_collected",
        "exit_observed",
        "termination_observed",
        "termination_verified",
        "snapshot_verified",
    ]
    assert (
        records(runner, "release_sent")[0].files.files[0].content
        == runtime.release_bytes
    )
    assert runtime.calls[-1] == "terminate"
    with pytest.raises(IntegrityError, match="consumed"):
        runner.run(runtime)
    with pytest.raises(IntegrityError, match="Refresh"):
        runner.authorize_refresh()


@pytest.mark.parametrize(
    "point",
    [
        "prepare",
        "inspect_created",
        "start",
        "inspect_running",
        "read_inputs",
        "release",
        "collect",
        "inspect_exited",
        "terminate",
    ],
)
def test_every_runtime_failure_fences_run_and_attempts_cleanup(setup, point):
    runner, runtime, _, _ = setup

    def hook(name):
        if name == point:
            raise OSError("injected")

    runtime.hook = hook
    with pytest.raises(OSError):
        runner.run(runtime)
    assert runner.primary_failed
    assert "terminate" in runtime.calls
    with pytest.raises(IntegrityError, match="consumed"):
        runner.run(runtime)
    if point == "terminate":
        with pytest.raises(IntegrityError, match="confirmed stop"):
            runner.authorize_refresh()


@pytest.mark.parametrize("point", ["prepare", "release", "collect"])
def test_cancellation_still_reconciles_and_records_failure(setup, point):
    runner, runtime, _, _ = setup

    def hook(name):
        if name == point:
            raise asyncio.CancelledError()

    runtime.hook = hook
    with pytest.raises(asyncio.CancelledError):
        runner.run(runtime)
    assert runtime.calls[-1] == "terminate"
    assert runner.primary_failed
    assert records(runner, "fixture_failure_accounted")


@pytest.mark.parametrize(
    "point",
    [
        "prepare",
        "inspect_created",
        "start",
        "inspect_running",
        "read_inputs",
        "release",
        "collect",
        "inspect_exited",
        "terminate",
    ],
)
def test_late_runtime_return_cannot_pass_or_refresh_deadline(setup, point):
    runner, runtime, clock, _ = setup

    def hook(name):
        if name == point:
            clock.ns += 30_000_000_000

    runtime.hook = hook
    with pytest.raises(IntegrityError, match="deadline"):
        runner.run(runtime)
    assert runner.primary_failed
    assert runtime.calls[-1] == "terminate"
    with pytest.raises(IntegrityError):
        runner.authorize_refresh()


@pytest.mark.parametrize(
    "kind",
    [
        "run_consumed",
        "release_intent",
        "release_sent",
        "report_collected",
        "exit_observed",
        "termination_verified",
        "snapshot_verified",
    ],
)
@pytest.mark.parametrize(
    "boundary", ["before_commit", "after_commit", "before_readback"]
)
def test_persistence_failure_never_returns_result_or_allows_refresh(
    setup, kind, boundary
):
    runner, runtime, _, fault = setup
    fault.at = f"journal.{boundary}:{kind}"
    with pytest.raises(OSError):
        runner.run(runtime)
    assert runner.primary_failed
    assert "terminate" in runtime.calls
    if kind in {"run_consumed", "release_intent"}:
        assert "release" not in runtime.calls
    with pytest.raises(IntegrityError):
        runner.authorize_refresh()


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "f" * 32),
        ("reason", "timeout"),
        ("quiescent", False),
        ("capture_complete", False),
        ("exitcode", 1),
        ("snapshot_sha256", "0" * 64),
    ],
)
def test_bad_worker_result_retained_but_unusable(setup, field, value):
    runner, runtime, _, _ = setup

    def change(result):
        result[field] = value
        return result

    runtime.result_change = change
    with pytest.raises(IntegrityError):
        runner.run(runtime)
    assert records(runner, "report_collected")
    assert not records(runner, "snapshot_verified")


@pytest.mark.parametrize(
    "field,value",
    [
        ("Running", True),
        ("Pid", 1000),
        ("ExitCode", 1),
        ("ExitCode", False),
        ("OOMKilled", True),
        ("Status", "dead"),
    ],
)
def test_independent_exit_state_required(setup, field, value):
    runner, runtime, _, _ = setup

    def change(result):
        if runtime.status == "exited":
            result["State"][field] = value
        return result

    runtime.inspection_change = change
    with pytest.raises(IntegrityError, match="exit"):
        runner.run(runtime)
    from recollect.selfmod.journal import decode

    observed = records(runner, "exit_observed")[0].files.files[0].content
    assert decode(observed)["State"][field] == value


def test_input_bytes_cannot_drift(setup):
    runner, runtime, _, _ = setup
    runtime.read_inputs = lambda *_: Snapshot((File("spec.json", b"changed"),))
    with pytest.raises(IntegrityError, match="inputs changed"):
        runner.run(runtime)
    assert "release" not in runtime.calls


def test_archive_must_not_be_worker_input(setup):
    runner, runtime, _, _ = setup
    runtime.root = runner.journal.root
    with pytest.raises(IntegrityError, match="overlap"):
        runner.run(runtime)
    assert "start" not in runtime.calls


def test_frozen_policy_attested_before_start(setup):
    runner, runtime, _, _ = setup

    def change(result):
        result["HostConfig"]["Privileged"] = True
        return result

    runtime.inspection_change = change
    with pytest.raises(IntegrityError):
        runner.run(runtime)
    assert "start" not in runtime.calls


def test_setup_cost_and_archive_cost_reduce_release_budget(setup):
    runner, runtime, clock, fault = setup

    def hook(name):
        if name == "prepare":
            clock.ns += 400_000_000

    def delay(point):
        if point == "journal.after_readback:release_intent":
            clock.ns += 100_000_000

    runtime.hook, fault.callback = hook, delay
    runner.run(runtime)
    from recollect.selfmod.journal import decode

    assert decode(runtime.release_bytes)["remaining_ms"] == 1500


@pytest.mark.parametrize("change", ["backward", "boot"])
def test_clock_continuity_loss_still_stops_worker(setup, change):
    runner, runtime, clock, _ = setup

    def hook(name):
        if name == "collect":
            if change == "boot":
                clock.boot = "new-process"
            else:
                clock.ns -= 1

    runtime.hook = hook
    with pytest.raises(IntegrityError, match="continuity"):
        runner.run(runtime)
    assert runtime.calls[-1] == "terminate"
    with pytest.raises(IntegrityError):
        runner.authorize_refresh()


def test_refresh_is_explicit_fresh_frozen_and_permanently_diagnostic(setup):
    runner, runtime, clock, _ = setup
    original = runner.spec
    fail_collect(runner, runtime)
    assert runtime.calls.count("prepare") == 1
    grant = runner.authorize_refresh()
    assert runtime.calls.count("prepare") == 1
    runner.refresh(grant)
    new = runner.spec
    assert new.run_id != original.run_id
    assert new.binding.attempt_id != original.binding.attempt_id
    assert new.binding.instance_id != original.binding.instance_id
    assert new.baseline == original.baseline
    assert new.policy == original.policy
    assert new.image_id == original.image_id
    assert new.timeout_ms == original.timeout_ms
    assert new.binding.contract_sha256 == original.binding.contract_sha256
    assert new.binding.artifact_sha256 is None
    assert runner.primary_failed
    assert frozen_input(new) == runner._inputs
    with pytest.raises(IntegrityError, match="consumed"):
        runner.refresh(grant)
    receipt = runner.run(runtime)
    assert receipt.diagnostic_only is True
    assert runner.primary_failed
    opened = records(runner, "executor_opened")[0].value["data"]
    assert opened["deadline_ns"] == 21_000_000_000


def test_unknown_termination_blocks_refresh(setup):
    runner, runtime, _, _ = setup
    runtime.clean_stop = False
    fail_collect(runner, runtime)
    with pytest.raises(IntegrityError, match="confirmed stop"):
        runner.authorize_refresh()


def test_refresh_grant_cannot_change_scope(setup):
    runner, runtime, _, _ = setup
    fail_collect(runner, runtime)
    grant = runner.authorize_refresh()
    forged = replace(grant, replacement=replace(grant.replacement, timeout_ms=60000))
    with pytest.raises(IntegrityError, match="Unissued"):
        runner.refresh(forged)
    assert runner.spec == runtime.config


def test_refresh_limit_is_lineage_wide(setup):
    runner, runtime, _, _ = setup
    fail_collect(runner, runtime)
    runner.refresh(runner.authorize_refresh())
    fail_collect(runner, runtime)
    with pytest.raises(IntegrityError, match="Refresh"):
        runner.authorize_refresh()


def test_refresh_cannot_reset_original_attempt_deadline(setup):
    runner, runtime, clock, _ = setup
    fail_collect(runner, runtime)
    grant = runner.authorize_refresh()
    clock.ns = 21_000_000_000
    with pytest.raises(IntegrityError, match="deadline"):
        runner.refresh(grant)
    assert runtime.calls.count("prepare") == 1


def test_reentrant_run_is_rejected_without_interfering_with_owner(setup):
    runner, runtime, _, _ = setup

    def hook(name):
        if name == "collect":
            with pytest.raises(IntegrityError, match="in progress"):
                runner.run(runtime)

    runtime.hook = hook
    assert runner.run(runtime).snapshot == runner.spec.baseline


@pytest.mark.parametrize(
    "kind",
    [
        "fixture_failure_accounted",
        "diagnostic_refresh_authorized",
        "diagnostic_refresh_consumed",
        "diagnostic_worker_prepared",
    ],
)
def test_refresh_persistence_faults_fail_closed(setup, kind):
    runner, runtime, _, fault = setup
    fault.at = "journal.after_commit:" + kind
    fail_collect(runner, runtime)
    if kind == "fixture_failure_accounted":
        with pytest.raises(IntegrityError):
            runner.authorize_refresh()
    elif kind == "diagnostic_refresh_authorized":
        with pytest.raises(OSError):
            runner.authorize_refresh()
    else:
        grant = runner.authorize_refresh()
        with pytest.raises(OSError):
            runner.refresh(grant)
    with pytest.raises(IntegrityError):
        runner.run(runtime)
    assert runtime.calls.count("prepare") == 1


def test_late_final_archive_readback_invalidates_snapshot(setup):
    runner, runtime, clock, fault = setup

    def delay(point):
        if point == "journal.after_readback:snapshot_verified":
            clock.ns += 30_000_000_000

    fault.callback = delay
    with pytest.raises(IntegrityError, match="deadline"):
        runner.run(runtime)
    assert runner.primary_failed
    assert records(runner, "snapshot_verified")
    assert records(runner, "fixture_failure_accounted")
    assert runtime.calls.count("terminate") == 1


def test_late_report_bytes_are_retained_as_failure_evidence(setup):
    runner, runtime, clock, _ = setup

    def delay(name):
        if name == "collect":
            clock.ns += 30_000_000_000

    runtime.hook = delay
    with pytest.raises(IntegrityError, match="deadline"):
        runner.run(runtime)
    assert records(runner, "report_collected")
    assert not records(runner, "snapshot_verified")


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "f" * 32),
        ("spec_sha256", "f" * 64),
        ("container_id", "f" * 64),
        ("confirmed", 1),
        ("evidence", Snapshot(())),
    ],
)
def test_unbound_or_empty_termination_evidence_cannot_authorize_refresh(
    setup,
    field,
    value,
):
    runner, runtime, _, _ = setup
    stop = runtime.terminate
    runtime.terminate = lambda *args: replace(stop(*args), **{field: value})
    with pytest.raises(IntegrityError, match="termination"):
        runner.run(runtime)
    with pytest.raises(IntegrityError, match="confirmed stop"):
        runner.authorize_refresh()


@pytest.mark.parametrize("exitcode", [1, False, None])
def test_attachment_failure_cannot_be_overridden_by_valid_report(setup, exitcode):
    runner, runtime, _, _ = setup
    collect = runtime.collect
    runtime.collect = lambda *args: replace(
        collect(*args), attachment_exitcode=exitcode
    )
    with pytest.raises(IntegrityError, match="exit"):
        runner.run(runtime)
    assert records(runner, "report_collected")


def test_no_release_with_less_than_one_millisecond_remaining(setup):
    runner, runtime, clock, _ = setup

    def delay(name):
        if name == "read_inputs":
            clock.ns += 1_999_500_000

    runtime.hook = delay
    with pytest.raises(IntegrityError, match="budget"):
        runner.run(runtime)
    assert "release" not in runtime.calls


def test_closed_archive_never_dispatches(setup):
    runner, runtime, _, _ = setup
    runner.close()
    with pytest.raises(IntegrityError, match="closed"):
        runner.run(runtime)
    assert runtime.calls == []


@pytest.mark.parametrize("change", ["backward", "boot"])
def test_restoring_clock_cannot_restore_refresh_authority(setup, change):
    runner, runtime, clock, _ = setup

    def hook(name):
        if name == "collect":
            if change == "boot":
                clock.boot = "another-clock"
            else:
                clock.ns -= 1

    runtime.hook = hook
    with pytest.raises(IntegrityError, match="continuity"):
        runner.run(runtime)
    clock.boot, clock.ns = "simulated-process", 1_000_000_001
    with pytest.raises(IntegrityError, match="permanently"):
        runner.authorize_refresh()
    assert runner.primary_failed


@pytest.mark.parametrize("state", ["ready", "complete", "failed", "refreshed"])
def test_existing_journal_cannot_initialize_a_new_executor(setup, state):
    runner, runtime, clock, _ = setup
    if state == "complete":
        runner.run(runtime)
    elif state in {"failed", "refreshed"}:
        fail_collect(runner, runtime)
        if state == "refreshed":
            runner.refresh(runner.authorize_refresh())
    with pytest.raises(IntegrityError, match="unclaimed"):
        FixtureExecutor(runner.journal, runner.spec, clock(), 21_000_000_000, 3, clock)


def test_failed_exit_identity_is_archived_before_rejection(setup):
    runner, runtime, _, _ = setup

    def change(value):
        if runtime.status == "exited":
            value["Id"] = "f" * 64
        return value

    runtime.inspection_change = change
    with pytest.raises(IntegrityError):
        runner.run(runtime)
    assert (
        b'"Id":"' + b"f" * 64
        in records(
            runner,
            "exit_observed",
        )[0]
        .files.files[0]
        .content
    )


def test_oversized_exit_observation_retains_bounded_marked_prefix(setup):
    runner, runtime, _, _ = setup

    def change(value):
        if runtime.status == "exited":
            value["extra"] = "x" * (64 * 1024)
        return value

    runtime.inspection_change = change
    with pytest.raises(IntegrityError, match="bound"):
        runner.run(runtime)
    record = records(runner, "exit_observed")[0]
    assert record.value["data"]["truncated"] is True
    assert len(record.files.files[0].content) == 64 * 1024


def test_clock_exception_permanently_disables_refresh(setup):
    runner, runtime, clock, _ = setup

    def broken():
        raise OSError("clock unavailable")

    def hook(name):
        if name == "collect":
            runner._clock = broken

    runtime.hook = hook
    with pytest.raises(OSError, match="clock unavailable"):
        runner.run(runtime)
    runner._clock = clock
    with pytest.raises(IntegrityError, match="permanently"):
        runner.authorize_refresh()
