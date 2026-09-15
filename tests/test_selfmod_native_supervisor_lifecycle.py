import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from recollect.selfmod import native_supervisor as supervisor


class Retained(BaseException):
    pass


class Bridge:
    def __init__(self):
        self.failed = threading.Event()
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "EVIDENCE", tmp_path)
    capture = SimpleNamespace(_task=lambda *args: {"start": 42})
    value = supervisor.NativeLifecycle(Bridge(), capture, "a" * 64)
    value.process = SimpleNamespace(pid=7)

    def retain():
        raise Retained()

    monkeypatch.setattr(value, "retain", retain)
    return value


def test_failure_is_permanent_and_preserves_first_cause(owner, tmp_path):
    owner.fail("first", OSError())
    owner.fail("second", ValueError())
    assert owner.failed and owner.terminal.is_set() and owner.bridge.closed
    assert json.loads((tmp_path / "failure.json").read_bytes()) == {
        "phase": "first", "error_type": "OSError",
    }


def test_failure_is_latched_before_waking_terminal_collector(owner, monkeypatch):
    observed = []
    original = owner.terminal.set

    def wake():
        observed.append(owner.failed)
        original()

    monkeypatch.setattr(owner.terminal, "set", wake)
    owner.fail("native_exit", ChildProcessError())
    assert observed == [True]


@pytest.mark.parametrize("fault", ["observe", "collect", "result", "bridge"])
def test_cleanup_failures_cannot_bypass_retention(owner, tmp_path, monkeypatch, fault):
    calls = []

    def collect(*args, **kwargs):
        calls.append(kwargs)
        if fault == "collect":
            raise OSError("collector launch")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(supervisor.subprocess, "run", collect)
    if fault == "observe":
        monkeypatch.setattr(owner.capture, "_task", lambda *a: (_ for _ in ()).throw(
            OSError("cannot observe identity")))
    elif fault == "result":
        (tmp_path / "collector-result.json").mkdir()
    elif fault == "bridge":
        monkeypatch.setattr(owner.bridge, "close", lambda: (_ for _ in ()).throw(
            OSError("close failed")))
    with pytest.raises(Retained):
        owner.finish()
    assert owner.failed and owner.terminal.is_set()
    assert bool(calls) == (fault != "observe")


def test_missing_evidence_output_still_attempts_collector(owner, tmp_path, monkeypatch):
    (tmp_path / "disconnect-stop.json").mkdir()
    calls = []

    def collect(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(supervisor.subprocess, "run", collect)
    with pytest.raises(Retained):
        owner.finish()
    assert calls[0]["stdout"] == supervisor.subprocess.DEVNULL
    assert json.loads(calls[0]["input"])["native"] == {"pid": 7, "start": 42}
    assert owner.failed


@pytest.mark.parametrize("code", [0, 1, -9])
def test_collector_status_recorded_and_only_clean_stop_joins_drainers(
    owner, tmp_path, monkeypatch, code,
):
    joins = []
    owner.drainers.append(SimpleNamespace(join=lambda: joins.append(True)))
    monkeypatch.setattr(supervisor.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(returncode=code))
    with pytest.raises(Retained):
        owner.finish()
    assert json.loads((tmp_path / "collector-result.json").read_bytes()) == {
        "returncode": code,
    }
    assert bool(joins) == (code == 0)
    assert owner.failed == (code != 0)


class StalledInput:
    def __init__(self):
        self.reading = threading.Event()
        self.release = threading.Event()

    def readline(self, _):
        self.reading.set()
        assert self.release.wait(5)
        return b""


def test_actual_child_exit_wakes_owner_without_stdin_eof(owner, monkeypatch, tmp_path):
    pipe = StalledInput()
    flags = []
    for name, value in (("P_PID", 1), ("WEXITED", 4), ("WNOWAIT", 0x1000000)):
        monkeypatch.setattr(supervisor.os, name, value, raising=False)

    def waitid(kind, pid, options):
        flags.append((kind, pid, options))
        return SimpleNamespace(si_pid=pid, si_code=1, si_status=23)

    monkeypatch.setattr(supervisor.os, "waitid", waitid, raising=False)
    with ThreadPoolExecutor() as pool:
        result = pool.submit(lambda: list(owner.controls(pipe)))
        try:
            assert pipe.reading.wait(5)
            owner.watch_exit()
            assert result.result(5) == []
        finally:
            pipe.release.set()
    assert owner.failed and owner.bridge.closed
    assert flags == [(1, 7, 4 | 0x1000000)]
    assert json.loads((tmp_path / "native-exit.json").read_bytes())["status"] == 23


def test_bridge_failure_wakes_owner_without_stdin_eof(owner):
    pipe = StalledInput()
    with ThreadPoolExecutor() as pool:
        result = pool.submit(lambda: list(owner.controls(pipe)))
        try:
            assert pipe.reading.wait(5)
            owner.bridge.failed.set()
            assert result.result(5) == []
        finally:
            pipe.release.set()
    assert owner.failed


def test_queued_fence_is_delivered_before_following_eof(owner):
    frame = {"kind": "fence"}
    stream = owner.controls(io.BytesIO(supervisor.canonical(frame)))
    assert next(stream) == frame
    owner.terminal.set()
    assert list(stream) == []
    assert not owner.failed


def test_control_eof_is_failure(owner):
    assert list(owner.controls(io.BytesIO())) == []
    assert owner.failed


@pytest.mark.parametrize("reason", ["fence", "log_failure"])
def test_collection_does_not_wait_for_blocked_control_output(
    owner, monkeypatch, reason,
):
    output_blocked = threading.Event()
    release_output = threading.Event()
    collected = threading.Event()
    monkeypatch.setattr(owner, "collect", collected.set)

    def emit(value):
        output_blocked.set()
        assert release_output.wait(5)

    owner.start_cleanup()
    with ThreadPoolExecutor() as pool:
        if reason == "fence":
            future = pool.submit(owner.control_loop, io.BytesIO(
                supervisor.canonical({"kind": "fence", "run_id": "run"})),
                {"run_id": "run"}, emit)
        else:
            future = pool.submit(emit, {"kind": "native_started"})
        try:
            assert output_blocked.wait(5)
            if reason == "log_failure":
                owner.fail("log_write", OSError())
            assert collected.wait(5)
            assert not future.done()
        finally:
            release_output.set()
        future.result(5)


def test_failed_proxy_racing_fence_is_accounted_at_settlement(owner, monkeypatch):
    def controls(stream):
        owner.bridge.failed.set()
        yield {"kind": "fence"}

    monkeypatch.setattr(owner, "controls", controls)
    collected = []
    monkeypatch.setattr(owner, "collect", lambda: collected.append(True))
    owner.control_loop(None, {}, lambda value: None)
    with pytest.raises(Retained):
        owner.finish()
    assert owner.failed and collected == [True]


def test_result_persistence_failure_still_joins_drainers(owner, tmp_path, monkeypatch):
    (tmp_path / "collector-result.json").mkdir()
    joins = []
    owner.drainers.append(SimpleNamespace(join=lambda: joins.append(True)))
    monkeypatch.setattr(supervisor.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(Retained):
        owner.finish()
    assert owner.failed and joins == [True]


def test_failed_first_collector_output_close_does_not_skip_second(
    owner, tmp_path, monkeypatch,
):
    closed = []
    original = Path.open

    class Output(io.BytesIO):
        def __init__(self, path):
            super().__init__()
            self.path = path

        def close(self):
            closed.append(self.path.name)
            super().close()
            if self.path.name == "disconnect-stop.json":
                raise OSError("close failed")

    paths = {tmp_path / "disconnect-stop.json", tmp_path / "disconnect-stop.stderr"}
    monkeypatch.setattr(Path, "open", lambda p, *a, **kw:
                        Output(p) if p in paths else original(p, *a, **kw))
    monkeypatch.setattr(supervisor.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(Retained):
        owner.finish()
    assert closed[:2] == ["disconnect-stop.json", "disconnect-stop.stderr"]
    assert owner.failed


def test_log_drains_all_bytes_while_retaining_bounded_prefix(tmp_path):
    errors = []
    source = io.BytesIO(b"x" * 100_000)
    path = tmp_path / "native.stdout"
    supervisor.drain_log(source, path, lambda *args: errors.append(args))
    assert source.closed and not errors
    assert path.read_bytes() == b"x" * 65536
    assert json.loads(path.with_suffix(".stdout.json").read_bytes()) == {
        "bytes_retained": 65536, "truncated": True, "complete": True,
    }


@pytest.mark.parametrize("fault", ["open", "write", "metadata"])
def test_log_failure_notifies_owner_and_still_drains_input(
    owner, tmp_path, monkeypatch, fault,
):
    path = tmp_path / "native.stdout"
    source = io.BytesIO(b"x" * 100_000)
    consumed = []
    real_read = source.read

    def read(size):
        result = real_read(size)
        consumed.append(len(result))
        return result

    source.read = read
    if fault == "open":
        path.mkdir()
    elif fault == "metadata":
        path.with_suffix(".stdout.json").mkdir()
    else:
        original = Path.open

        class BrokenOutput(io.BytesIO):
            def write(self, value):
                raise OSError("disk write failed")

        monkeypatch.setattr(Path, "open", lambda p, *a, **kw:
                            BrokenOutput() if p == path else original(p, *a, **kw))
    supervisor.drain_log(source, path, owner.fail)
    assert sum(consumed) == 100_000 and source.closed
    assert owner.failed and owner.terminal.is_set()
    if fault != "metadata":
        assert not json.loads(path.with_suffix(".stdout.json").read_bytes())["complete"]


@pytest.mark.parametrize("fault", ["spawn", "identity", "persist", "started"])
def test_post_release_failure_always_retains_namespace(tmp_path, monkeypatch, fault):
    monkeypatch.setattr(supervisor, "EVIDENCE", tmp_path)
    bridge = Bridge()
    proxy = SimpleNamespace(
        ModelBridge=lambda emit: bridge,
        listener=lambda b: SimpleNamespace(serve_forever=lambda: None),
    )
    captures = []

    def observe(*args):
        if fault == "identity":
            raise OSError("pid observation failed")
        return {"start": 42}

    capture = SimpleNamespace(_task=observe)

    def spawn(*args, **kwargs):
        if fault == "spawn":
            raise OSError("spawn failed")
        return SimpleNamespace(pid=7, stdout=io.BytesIO(), stderr=io.BytesIO())

    def collect(*args, **kwargs):
        captures.append(kwargs)
        return SimpleNamespace(returncode=0)

    def retain(self):
        raise Retained()

    def emit(frame):
        if fault == "started":
            raise BrokenPipeError()

    monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
    monkeypatch.setattr(supervisor.subprocess, "run", collect)
    monkeypatch.setattr(supervisor.NativeLifecycle, "retain", retain)
    monkeypatch.setattr(supervisor.NativeLifecycle, "watch_exit", lambda self: None)
    if fault == "persist":
        (tmp_path / "native.json").mkdir()
    with pytest.raises(Retained):
        supervisor.run_native(proxy, capture, {"spec_sha256": "a" * 64},
                              {}, emit, io.BytesIO())
    assert bridge.closed
    assert bool(captures) == (fault in {"persist", "started"})
    assert (tmp_path / "failure.json").is_file()
