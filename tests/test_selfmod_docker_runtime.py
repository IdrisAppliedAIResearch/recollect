"""Adapter command/fault tests using an explicit fake Docker daemon and CLI."""

import json
from pathlib import Path

import pytest

from recollect.selfmod.containment import wire_binding
from recollect.selfmod.docker_runtime import DockerFixtureRuntime
from recollect.selfmod.executor import FixtureExecutor
from recollect.selfmod.journal import IntegrityError, decode, encode, sha256
from tests.selfmod_checkpoint_helpers import FakeClock
from tests.selfmod_containment_helpers import CONTAINER_ID, inspection, report, spec


class FakeCommand:
    def __init__(self, daemon, argv, **kwargs):
        self.daemon, self.args = daemon, argv[5:]
        self.stdout = self.stderr = b""
        self.sent = False
        daemon.calls.append((argv, kwargs))

    def finish(self):
        d, args = self.daemon, self.args
        if args[0] == "create":
            d.exists, d.status = True, "created"
            self.stdout = (CONTAINER_ID + "\n").encode()
        elif args[0] == "inspect":
            value = inspection(d.inputs, d.spec)
            value["State"] = {
                "Status": d.status,
                "Running": d.status == "running",
                "Restarting": False,
                "Pid": 123 if d.status == "running" else 0,
                "ExitCode": 0,
                "OOMKilled": False,
            }
            d.change_inspection(value)
            self.stdout = json.dumps([value]).encode()
        elif args[0] == "start":
            d.status = "exited"
            value = report(d.spec)
            value["supervisor_sha256"] = d.supervisor_sha()
            self.stdout = encode(value)
        elif args[0] == "container":
            self.stdout = (CONTAINER_ID + "\n").encode() if d.exists else b""
        elif args[0] == "kill":
            assert args[-1] == CONTAINER_ID
            d.status = "exited"
        elif args[0] == "rm":
            assert args == ["rm", CONTAINER_ID]
            assert d.status in {"created", "exited"}
            d.exists = False
        else:
            raise AssertionError(args)
        d.hook(args[0], self)
        return self.stdout, self.stderr, 0

    def line(self, limit):
        assert limit == 2048
        self.daemon.status = "running"
        self.daemon.hook("ready", self)
        self.stdout = encode(
            {
                "kind": "ready",
                **wire_binding(
                    self.daemon.spec,
                    self.daemon.supervisor_sha(),
                ),
            }
        )
        return self.stdout

    def send(self, record):
        assert not self.sent
        self.sent = True
        self.daemon.release = record
        self.daemon.hook("release", self)

    def close(self):
        self.daemon.hook("close", self)

    def evidence(self):
        return self.stdout, self.stderr


class Daemon:
    def __init__(self, root, config):
        self.spec = config
        self.inputs = root / ("selfmod-fixture-input-" + config.run_id)
        self.calls = []
        self.exists, self.status = False, "created"
        self.hook = lambda *_: None
        self.change_inspection = lambda _: None

    def factory(self, argv, deadline, **kwargs):
        return FakeCommand(self, argv, **kwargs)

    def supervisor_sha(self):
        return sha256((self.inputs / "supervisor.py").read_bytes())


@pytest.fixture
def setup(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    executable = tmp_path / "docker.exe"
    executable.write_bytes(b"fake CLI; factory intercepts every invocation")
    config, clock = spec(), FakeClock()
    daemon = Daemon(shared, config)
    runtime = DockerFixtureRuntime(
        executable,
        shared,
        "npipe:////./pipe/dockerFixture",
        clock=clock,
        command_factory=daemon.factory,
    )
    runner = FixtureExecutor.create(
        tmp_path / "archive",
        config,
        original_started=clock(),
        deadline_ns=21_000_000_000,
        max_refreshes=1,
        clock=clock,
    )
    yield runner, runtime, daemon, clock
    runner.close()


def commands(daemon):
    return [argv[5:] for argv, _ in daemon.calls]


def test_full_adapter_lifecycle_only_removes_owned_container(setup):
    runner, runtime, daemon, _ = setup
    receipt = runner.run(runtime)
    assert receipt.snapshot == runner.spec.baseline
    assert not daemon.exists
    issued = commands(daemon)
    assert issued[0][0] == "create"
    assert "--pull=never" in issued[0]
    assert ["rm", CONTAINER_ID] in issued
    assert all(c[0] not in {"build", "pull", "run"} for c in issued)
    assert decode(daemon.release)["remaining_ms"] <= runner.spec.timeout_ms
    assert (daemon.inputs / "spec.json").exists()  # Evidence intentionally retained.
    assert all("--host" in argv and "--config" in argv for argv, _ in daemon.calls)


def test_cancelled_create_with_no_visible_container_is_uncertain(setup):
    runner, runtime, daemon, _ = setup

    def fail(kind, _):
        if kind == "create":
            daemon.exists = False
            raise TimeoutError("create response lost")

    daemon.hook = fail
    with pytest.raises(TimeoutError):
        runner.run(runtime)
    with pytest.raises(IntegrityError, match="confirmed stop"):
        runner.authorize_refresh()
    assert not any(c[0] in {"kill", "rm"} for c in commands(daemon))


@pytest.mark.parametrize("point", ["create", "ready", "release"])
def test_ambiguous_operation_reconciles_found_id_and_removes_it(setup, point):
    runner, runtime, daemon, _ = setup

    def fail(kind, _):
        if kind == point:
            raise OSError("response lost")

    daemon.hook = fail
    with pytest.raises(OSError):
        runner.run(runtime)
    assert not daemon.exists
    assert ["rm", CONTAINER_ID] in commands(daemon)
    assert runner.authorize_refresh().failed_run_id == daemon.spec.run_id


@pytest.mark.parametrize("field", ["Name", "Image", "Id", "label"])
def test_foreign_identity_never_killed_or_removed(setup, field):
    runner, runtime, daemon, _ = setup

    def change(value):
        if field == "label":
            value["Config"]["Labels"]["recollect.selfmod"] = "other"
        else:
            value[field] = "other"

    daemon.change_inspection = change
    with pytest.raises(IntegrityError):
        runner.run(runtime)
    assert not any(c[0] in {"kill", "rm"} for c in commands(daemon))
    with pytest.raises(IntegrityError):
        runner.authorize_refresh()


def test_owned_but_bad_profile_is_still_cleaned_up(setup):
    runner, runtime, daemon, _ = setup
    daemon.change_inspection = lambda value: value["HostConfig"].update(Privileged=True)
    with pytest.raises(IntegrityError):
        runner.run(runtime)
    assert not daemon.exists
    assert ["rm", CONTAINER_ID] in commands(daemon)


def test_host_input_drift_prevents_release(setup):
    runner, runtime, daemon, _ = setup

    def drift(kind, _):
        if kind == "ready":
            (daemon.inputs / "spec.json").write_bytes(b"changed")

    daemon.hook = drift
    with pytest.raises(IntegrityError, match="differs"):
        runner.run(runtime)
    assert not hasattr(daemon, "release")


def test_cancellation_does_not_cancel_cleanup_commands(setup):
    runner, runtime, daemon, _ = setup

    def cancel(kind, _):
        if kind == "release":
            runtime.cancel()

    daemon.hook = cancel
    with pytest.raises(InterruptedError):
        runner.run(runtime)
    assert not daemon.exists
    assert any(args[0] == "kill" for args in commands(daemon))
    cleanup = [
        (argv, options)
        for argv, options in daemon.calls
        if argv[5] in {"kill", "rm", "container"}
    ]
    assert all(options["cancelled"] is None for _, options in cleanup)


def test_cli_environment_ignores_docker_and_proxy_overrides(setup, monkeypatch):
    runner, runtime, daemon, _ = setup
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")
    monkeypatch.setenv("HTTP_PROXY", "http://example.invalid")
    runner.run(runtime)
    for _, options in daemon.calls:
        assert not any(
            k.upper().startswith("DOCKER_") or "PROXY" in k.upper()
            for k in options["env"]
        )


@pytest.mark.parametrize(
    "endpoint", ["tcp://localhost:2375", "ssh://host", "npipe://remote"]
)
def test_nonlocal_endpoint_rejected(setup, endpoint):
    _, runtime, _, _ = setup
    with pytest.raises(ValueError, match="local"):
        DockerFixtureRuntime(Path(runtime._argv[0]), runtime._root, endpoint)


def test_runtime_is_single_use_even_after_termination(setup):
    runner, runtime, _, _ = setup
    runner.run(runtime)
    with pytest.raises(IntegrityError, match="single-use"):
        runtime.prepare(runner.spec, runner._inputs, None)


@pytest.mark.parametrize("point", ["kill", "rm", "container"])
def test_failed_cleanup_does_not_authorize_refresh(setup, point):
    runner, runtime, daemon, _ = setup

    def fail(kind, _):
        if kind == "release" or kind == point:
            raise OSError("uncertain")

    daemon.hook = fail
    with pytest.raises(OSError):
        runner.run(runtime)
    with pytest.raises(IntegrityError, match="confirmed stop"):
        runner.authorize_refresh()


def test_cli_configuration_drift_prevents_dispatch(setup):
    runner, runtime, daemon, _ = setup
    (runtime._config / "config.json").write_bytes(b'{"currentContext":"remote"}')
    with pytest.raises(IntegrityError):
        runner.run(runtime)
    assert not daemon.calls


def test_failed_command_raw_bytes_retained(setup):
    runner, runtime, daemon, _ = setup

    def fail(kind, command):
        if kind == "release":
            command.stderr = b"bounded transport failure"
            raise OSError("write failed")

    daemon.hook = fail
    with pytest.raises(OSError):
        runner.run(runtime)
    evidence = next(
        r for r in runner.journal.verify() if r.value["kind"] == "termination_verified"
    ).files
    assert any(f.content == b"bounded transport failure" for f in evidence.files)


def test_cleanup_clock_is_independent_of_failed_execution_clock(setup):
    runner, runtime, daemon, _ = setup
    closed = []

    def broken():
        raise OSError("execution clock failed")

    def hook(kind, command):
        if kind == "release":
            runtime._clock = broken
        if kind == "close":
            closed.append(command.args[0])

    daemon.hook = hook
    with pytest.raises(OSError, match="clock failed"):
        runner.run(runtime)
    assert not daemon.exists
    assert "start" in closed
    assert ["rm", CONTAINER_ID] in commands(daemon)


@pytest.mark.parametrize("point", ["kill", "rm", "container"])
def test_unconfirmed_cleanup_retains_failed_command_stderr(setup, point):
    runner, runtime, daemon, _ = setup

    def hook(kind, command):
        if kind == "release":
            raise OSError("release failure")
        if kind == point:
            command.stderr = b"distinctive cleanup failure"
            raise OSError("cleanup failure")

    daemon.hook = hook
    with pytest.raises(OSError):
        runner.run(runtime)
    observed = [
        r for r in runner.journal.verify() if r.value["kind"] == "termination_observed"
    ]
    assert observed[-1].value["data"]["confirmed"] is False
    assert any(
        f.content == b"distinctive cleanup failure"
        for r in observed
        for f in r.files.files
    )
    with pytest.raises(IntegrityError, match="confirmed stop"):
        runner.authorize_refresh()


def test_cleanup_clock_failure_still_closes_attachment(setup):
    runner, runtime, daemon, _ = setup
    closed = []

    def broken():
        raise OSError("cleanup clock failed")

    def hook(kind, command):
        if kind == "release":
            runtime._cleanup_clock = broken
            raise OSError("release failure")
        if kind == "close":
            closed.append(command.args[0])

    daemon.hook = hook
    with pytest.raises(OSError):
        runner.run(runtime)
    assert "start" in closed
    with pytest.raises(IntegrityError, match="confirmed stop"):
        runner.authorize_refresh()
