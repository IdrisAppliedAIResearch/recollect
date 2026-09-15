from dataclasses import replace

import pytest

from recollect.selfmod import fixture_supervisor
from recollect.selfmod.containment import (
    MAX_LOG_BYTES,
    attest,
    create_arguments,
    frozen_input,
    release_record,
    verified_snapshot,
    verify_ready,
    wire_binding,
)
from recollect.selfmod.contracts import ChangePolicy
from recollect.selfmod.journal import IntegrityError, decode, encode, sha256
from tests.selfmod_containment_helpers import (
    CONTAINER_ID,
    SUPERVISOR_SHA,
    inspection,
    report,
    report_bytes,
    spec,
)


def test_fixed_networkless_profile_and_exact_attestation(tmp_path):
    config = spec()
    args = create_arguments(config, tmp_path)
    assert args[args.index("--network") + 1] == "none"
    assert args.count("--mount") == 1
    assert "readonly" in args[args.index("--mount") + 1]
    assert "--publish" not in args and "--add-host" not in args
    assert args[-6:] == [
        config.image_id,
        "-I",
        "-S",
        "-u",
        "-B",
        "/input/supervisor.py",
    ]
    assert "--pull=never" in args
    attest(inspection(tmp_path, config), config, tmp_path, CONTAINER_ID)


@pytest.mark.parametrize("value", [False, None])
def test_oom_killer_optional_false_or_unset_is_accepted(tmp_path, value):
    config = spec()
    actual = inspection(tmp_path, config)
    actual["HostConfig"]["OomKillDisable"] = value
    attest(actual, config, tmp_path, CONTAINER_ID)
    assert "--oom-kill-disable=false" in create_arguments(config, tmp_path)


@pytest.mark.parametrize("value", [True, 0, 1, 0.0, "false", "null", "", [], {}])
def test_oom_killer_disabled_or_malformed_is_rejected(tmp_path, value):
    config = spec()
    actual = inspection(tmp_path, config)
    actual["HostConfig"]["OomKillDisable"] = value
    with pytest.raises(IntegrityError, match="OOM-killer"):
        attest(actual, config, tmp_path, CONTAINER_ID)


def test_missing_oom_killer_field_is_not_an_explicit_null(tmp_path):
    config = spec()
    actual = inspection(tmp_path, config)
    del actual["HostConfig"]["OomKillDisable"]
    with pytest.raises(IntegrityError, match="OOM-killer"):
        attest(actual, config, tmp_path, CONTAINER_ID)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("HostConfig", "Privileged", True),
        ("HostConfig", "ReadonlyRootfs", False),
        ("HostConfig", "NetworkMode", "bridge"),
        ("HostConfig", "IpcMode", "host"),
        ("HostConfig", "PidMode", "host"),
        ("HostConfig", "UTSMode", "host"),
        ("HostConfig", "CgroupnsMode", "host"),
        ("HostConfig", "UsernsMode", "host"),
        ("HostConfig", "Memory", 0),
        ("HostConfig", "MemorySwap", -1),
        ("HostConfig", "NanoCpus", 0),
        ("HostConfig", "PidsLimit", -1),
        ("HostConfig", "CapAdd", ["SYS_ADMIN"]),
        ("HostConfig", "CapDrop", []),
        ("HostConfig", "SecurityOpt", ["seccomp=unconfined"]),
        ("HostConfig", "Tmpfs", {}),
        ("HostConfig", "DeviceRequests", [{"Count": -1}]),
        ("HostConfig", "Binds", ["/:/host"]),
        ("HostConfig", "GroupAdd", ["0"]),
        ("HostConfig", "PortBindings", {"80/tcp": []}),
        ("HostConfig", "ExtraHosts", ["host.docker.internal:host-gateway"]),
        ("HostConfig", "LogConfig", {"Type": "json-file"}),
        ("HostConfig", "RestartPolicy", {"Name": "always"}),
        ("HostConfig", "Ulimits", []),
        ("HostConfig", "Init", True),
        ("HostConfig", "AutoRemove", True),
        ("HostConfig", "Runtime", "different-runtime"),
        ("HostConfig", "OomKillDisable", True),
        ("HostConfig", "MaskedPaths", []),
        ("HostConfig", "ReadonlyPaths", []),
        ("Config", "User", "65532:65532"),
        ("Config", "Image", "moving:tag"),
        ("Config", "Entrypoint", ["/bin/sh"]),
        ("Config", "Cmd", ["/work/fixture.py"]),
        ("Config", "WorkingDir", "/tmp"),
        ("Config", "Tty", True),
        ("Config", "OpenStdin", False),
        ("Config", "Env", ["LD_PRELOAD=/work/lib.so"]),
        ("Config", "Volumes", {"/extra": {}}),
        ("Config", "Healthcheck", {"Test": ["CMD", "anything"]}),
        ("Config", "Labels", {}),
        ("Config", "StopTimeout", 30),
    ],
)
def test_effective_policy_drift_is_rejected(tmp_path, section, key, value):
    config = spec()
    actual = inspection(tmp_path, config)
    actual[section][key] = value
    with pytest.raises(ValueError):
        attest(actual, config, tmp_path, CONTAINER_ID)


@pytest.mark.parametrize(
    "change", ["id", "image", "name", "extra_mount", "rw", "duplicate"]
)
def test_identity_and_mount_drift_is_rejected(tmp_path, change):
    config = spec()
    actual = inspection(tmp_path, config)
    if change in {"id", "image", "name"}:
        actual[{"id": "Id", "image": "Image", "name": "Name"}[change]] = "wrong"
    elif change == "rw":
        actual["Mounts"][0]["RW"] = True
    else:
        actual["Mounts"].append(
            dict(actual["Mounts"][0])
            if change == "duplicate"
            else {"Type": "volume", "Destination": "/extra"}
        )
    with pytest.raises(IntegrityError):
        attest(actual, config, tmp_path, CONTAINER_ID)


@pytest.mark.parametrize(
    "options",
    [
        {"delete": ("protected.py",)},
        {"modify": ("missing.py",)},
        {"create_under": ("protected.py",)},
        {"create_under": ("protected.py/sub",)},
        {"create_under": ("generated", "generated/nested")},
    ],
)
def test_unenforceable_grants_are_rejected_before_launch(options):
    config = spec()
    with pytest.raises(ValueError):
        replace(config, policy=ChangePolicy(config.baseline.sha256, **options))


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_ms", 0),
        ("timeout_ms", True),
        ("timeout_ms", 60_001),
        ("image_id", "latest"),
        ("run_id", "../../other"),
        ("entrypoint", "missing.py"),
        ("image_environment", ("X=1", "X=2")),
    ],
)
def test_fixture_configuration_cannot_broaden_scope(field, value):
    with pytest.raises(ValueError):
        replace(spec(), **{field: value})


def test_input_bytes_and_standalone_supervisor_agree():
    config = spec()
    captured = {f.path: f.content for f in frozen_input(config).files}
    envelope, files = fixture_supervisor.load_spec(captured["spec.json"])
    assert envelope["supervisor_sha256"] == sha256(captured["supervisor.py"])
    assert envelope["spec_sha256"] == config.sha256
    assert fixture_supervisor.snapshot_digest(files) == config.baseline.sha256
    assert config.payload["limits"] == fixture_supervisor.LIMITS


def test_host_execution_and_namespace_kill_are_forbidden():
    for operation in (fixture_supervisor.main, fixture_supervisor.quiesce):
        with pytest.raises(RuntimeError, match="host execution is forbidden"):
            operation()


def test_release_binds_spec_supervisor_run_and_remaining_budget():
    config = spec()
    ready = encode({"kind": "ready", **wire_binding(config, SUPERVISOR_SHA)})
    verify_ready(ready, config, SUPERVISOR_SHA)
    assert decode(release_record(config, SUPERVISOR_SHA, 1000))["remaining_ms"] == 1000
    for remaining in (0, -1, True, config.timeout_ms + 1):
        with pytest.raises(IntegrityError):
            release_record(config, SUPERVISOR_SHA, remaining)
    with pytest.raises(IntegrityError):
        verify_ready(ready, replace(config, timeout_ms=1000), SUPERVISOR_SHA)


def test_exact_report_reconstructs_bounded_source_snapshot():
    config = spec()
    assert (
        verified_snapshot(report_bytes(config), config, SUPERVISOR_SHA)
        == config.baseline
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason", "watchdog_timeout"),
        ("exitcode", True),
        ("exitcode", 1),
        ("quiescent", False),
        ("capture_complete", False),
        ("errors", ["missing"]),
        ("binding", {}),
        ("spec_sha256", "0" * 64),
        ("run_id", "0" * 32),
        ("snapshot_sha256", "0" * 64),
        ("stdout", "invalid base64!"),
        ("files", []),
        ("entries", []),
    ],
)
def test_failed_partial_or_forged_report_is_not_a_candidate(field, value):
    config = spec()
    data = report(config)
    data[field] = value
    with pytest.raises((ValueError, TypeError)):
        verified_snapshot(encode(data), config, SUPERVISOR_SHA)


@pytest.mark.parametrize(
    "field,value",
    [
        ("links", 2),
        ("mode", 0o777),
        ("uid", 65532),
        ("bytes", True),
        ("kind", "special"),
        ("path", "../escape"),
    ],
)
def test_entry_metadata_corruption_is_rejected(field, value):
    config = spec()
    data = report(config)
    data["entries"][0][field] = value
    with pytest.raises(ValueError):
        verified_snapshot(encode(data), config, SUPERVISOR_SHA)


def test_duplicate_paths_keys_and_log_overflow_are_rejected():
    import base64

    config = spec()
    data = report(config)
    data["files"].append(data["files"][0])
    with pytest.raises(ValueError):
        verified_snapshot(encode(data), config, SUPERVISOR_SHA)
    raw = report_bytes(config).replace(b'"exitcode":0', b'"exitcode":1,"exitcode":0')
    with pytest.raises(ValueError):
        verified_snapshot(raw, config, SUPERVISOR_SHA)
    data = report(config)
    data["stdout"] = base64.b64encode(b"x" * (MAX_LOG_BYTES + 1)).decode()
    with pytest.raises(IntegrityError):
        verified_snapshot(encode(data), config, SUPERVISOR_SHA)


def test_shared_bind_propagation_is_rejected(tmp_path):
    config = spec()
    actual = inspection(tmp_path, config)
    actual["Mounts"][0]["Propagation"] = "rshared"
    with pytest.raises(IntegrityError):
        attest(actual, config, tmp_path, CONTAINER_ID)


@pytest.mark.parametrize("size", [MAX_LOG_BYTES - 1, MAX_LOG_BYTES, MAX_LOG_BYTES + 1])
@pytest.mark.parametrize("fragmented", [False, True])
def test_log_limit_is_identical_in_streaming_and_final_drain(size, fragmented):
    output = bytearray()
    chunks = [b"x" * size] if not fragmented else [b"x" * (size - 1), b"x"]
    overflow = False
    for chunk in chunks:
        overflow |= fixture_supervisor.retain_log(output, chunk)
    assert overflow == (size > MAX_LOG_BYTES)
    assert len(output) == min(size, MAX_LOG_BYTES)


def test_completion_observation_after_select_deadline_is_rejected(monkeypatch):
    from types import SimpleNamespace

    clock = SimpleNamespace(now=1.0)
    pipes = iter(((10, 11), (12, 13)))

    class Selector:
        def register(self, *args):
            pass

        def select(self, timeout):
            clock.now = 3.0
            return []

        def close(self):
            pass

    monkeypatch.setattr(fixture_supervisor, "supervisor_only", lambda: None)
    monkeypatch.setattr(fixture_supervisor.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(fixture_supervisor.os, "pipe", lambda: next(pipes))
    monkeypatch.setattr(fixture_supervisor.os, "fork", lambda: 42, raising=False)
    monkeypatch.setattr(fixture_supervisor.os, "WNOHANG", 1, raising=False)
    monkeypatch.setattr(fixture_supervisor.os, "waitpid", lambda *a: (42, 0))
    monkeypatch.setattr(fixture_supervisor.os, "close", lambda *a: None)
    monkeypatch.setattr(fixture_supervisor.os, "set_blocking", lambda *a: None)
    monkeypatch.setattr(fixture_supervisor.os, "read", lambda *a: b"")
    monkeypatch.setattr(fixture_supervisor, "processes", lambda: [])
    monkeypatch.setattr(fixture_supervisor, "quiesce", lambda: None)
    monkeypatch.setattr(fixture_supervisor.selectors, "DefaultSelector", Selector)
    reason, exitcode, _, _ = fixture_supervisor.execute(
        {"entrypoint": "fixture.py"}, 2.0
    )
    assert exitcode == 0 and reason == "watchdog_timeout"


@pytest.mark.parametrize(
    "observation,expected",
    [(1.9, "completed"), (2.0, "completed"), (2.1, "watchdog_timeout")],
)
def test_exact_completion_deadline(observation, expected):
    assert (
        fixture_supervisor.completed_reason(2.0, 0, False, clock=lambda: observation)
        == expected
    )


def test_watchdog_expires_without_supervisor_progress(monkeypatch):
    from types import SimpleNamespace

    clock = SimpleNamespace(now=0.0)
    killed = []
    monkeypatch.setattr(fixture_supervisor, "attachment_closed", lambda: False)

    class Selector:
        def register(self, *args):
            pass

        def select(self, timeout):
            clock.now += 1.0
            return []

        def close(self):
            pass

    monkeypatch.setattr(fixture_supervisor, "container_only", lambda: None)
    monkeypatch.setattr(fixture_supervisor.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(fixture_supervisor.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(fixture_supervisor.os, "WNOHANG", 1, raising=False)
    monkeypatch.setattr(fixture_supervisor.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(fixture_supervisor.os, "waitpid", lambda *args: (0, 0))
    monkeypatch.setattr(fixture_supervisor.os, "close", lambda *args: None)
    monkeypatch.setattr(
        fixture_supervisor.os, "kill", lambda *args: killed.append(args)
    )
    assert fixture_supervisor.watchdog(42, 10, 1000) == 124
    assert killed == [(-1, fixture_supervisor.signal.SIGKILL)]


@pytest.mark.parametrize(
    "scenario,expected",
    [("late_release", 124), ("orphan_stream", 124), ("leftover", 125),
     ("clean_exit", 0)],
)
def test_watchdog_release_reaping_and_exit_boundaries(monkeypatch, scenario, expected):
    from types import SimpleNamespace

    clock = SimpleNamespace(now=0.0, waits=0, selects=0)
    monkeypatch.setattr(fixture_supervisor, "attachment_closed", lambda: False)

    class Selector:
        def register(self, *args):
            pass

        def select(self, timeout):
            clock.selects += 1
            clock.now = 10.5 if scenario == "late_release" else clock.now + 0.1
            return [(None, None)] if clock.selects <= 2 else []

        def unregister(self, *args):
            pass

        def close(self):
            pass

    def waitpid(*args):
        clock.waits += 1
        if scenario == "orphan_stream":
            clock.now += 0.01
            assert clock.waits < 33 or clock.selects > 1
            return 100 + clock.waits, 0
        if clock.waits == 1:
            return 42, 0
        if scenario == "leftover":
            return 0, 0
        raise ChildProcessError

    monkeypatch.setattr(fixture_supervisor, "container_only", lambda: None)
    monkeypatch.setattr(fixture_supervisor.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(fixture_supervisor.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(fixture_supervisor.os, "WNOHANG", 1, raising=False)
    monkeypatch.setattr(fixture_supervisor.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(fixture_supervisor.os, "waitpid", waitpid)
    monkeypatch.setattr(
        fixture_supervisor.os, "waitstatus_to_exitcode", lambda status: status,
        raising=False,
    )
    monkeypatch.setattr(fixture_supervisor.os, "close", lambda *args: None)
    monkeypatch.setattr(fixture_supervisor.os, "kill", lambda *args: None)
    monkeypatch.setattr(
        fixture_supervisor.os, "read",
        lambda *args: (
            fixture_supervisor.canonical({"deadline": clock.now + 3})
            + fixture_supervisor.canonical({"settling": True})
            if clock.selects == 1 else b""
        ),
    )
    assert fixture_supervisor.watchdog(42, 10, 1000) == expected
    if scenario == "late_release":
        assert clock.waits == 0
