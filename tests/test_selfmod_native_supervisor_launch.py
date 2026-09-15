"""PID 1 launch gate and helper lanes with fake processes; no Docker or models."""

import base64
import io
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from recollect.selfmod import native_http_relay as relay
from recollect.selfmod import native_supervisor as supervisor
from tests.test_selfmod_native_supervisor_lifecycle import Bridge, Retained


class Input(io.BytesIO):
    def close(self):
        self.written = self.getvalue()
        super().close()


class Process:
    def __init__(self, stdout=b"", stderr=b"", code=0, pid=4242):
        self.pid, self.returncode, self.code = pid, None, code
        self.stdin = Input()
        self.stdout, self.stderr = io.BytesIO(stdout), io.BytesIO(stderr)

    def wait(self):
        self.returncode = self.code
        return self.code


def relay_output(*frames):
    return b"".join((json.dumps(f, separators=(",", ":")) + "\n").encode()
                    for f in frames)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "EVIDENCE", tmp_path)
    killed = []
    monkeypatch.setattr(supervisor, "wait_exit", lambda pid: None)
    monkeypatch.setattr(supervisor, "send_kill", killed.append)
    capture = SimpleNamespace(_task=lambda proc, pid, tid: {"pid": pid, "start": 9})
    value = supervisor.NativeLifecycle(Bridge(), capture, "a" * 64)
    value.process = SimpleNamespace(pid=7)
    value.killed = killed
    value.emitted = queue.Queue()
    value.launched = []

    def retain():
        raise Retained()

    monkeypatch.setattr(value, "retain", retain)
    return value


def lanes(owner, *processes):
    pending = list(processes)

    def spawn(argv, **options):
        process = pending.pop(0)
        owner.launched.append((argv, options, process))
        return process

    owner.gate = supervisor.LaunchGate(spawn=spawn)
    http = supervisor.HttpLane(owner, owner.emitted.put, relay)
    history = supervisor.HistoryLane(owner, owner.emitted.put)
    owner.lanes = (http, history)
    return http, history


def request(identity=0, method="GET", path="/global/health", body=b""):
    frames = [{"kind": "http_request", "id": identity, "method": method,
               "path": path, "bytes": len(body)}]
    if body:
        frames.append({"kind": "http_request_chunk", "id": identity, "sequence": 0,
                       "base64": base64.b64encode(body).decode()})
    frames.append({"kind": "http_request_end", "id": identity,
                   "chunks": 1 if body else 0})
    return frames


def next_frame(owner):
    return owner.emitted.get(timeout=3)


def ack(lane, frame):
    lane.deliver({"kind": lane.name + "_ack", "id": frame["id"],
                  "sequence": frame["sequence"]})


def complete_http(owner, http, identity=0, **kwargs):
    for frame in request(identity, **kwargs):
        http.deliver(frame)
    frames = []
    while True:
        frame = next_frame(owner)
        frames.append(frame)
        ack(http, frame)
        if frame["kind"] == "http_end":
            for thread in http.threads:
                thread.join(3)
            return frames


def test_gate_refuses_launch_after_terminal_settlement():
    gate = supervisor.LaunchGate(spawn=lambda *a, **k: pytest.fail("spawned"))
    assert gate.settle_creation() == ()
    with pytest.raises(supervisor.LaunchClosed):
        gate.launch(["x"], root=False)


def test_settlement_waits_for_in_flight_creation_without_holding_the_lock():
    entered, release = threading.Event(), threading.Event()
    process = Process()

    def spawn(argv, **options):
        entered.set()
        assert release.wait(3)
        return process

    gate = supervisor.LaunchGate(spawn=spawn)
    launcher = threading.Thread(target=gate.launch, args=(["relay"],),
                                kwargs={"root": False})
    launcher.start()
    assert entered.wait(3)
    settled = []
    settler = threading.Thread(target=lambda: settled.append(gate.settle_creation()))
    settler.start()
    settler.join(0.2)
    # Creation may still produce a process, so the terminal path must wait.
    assert settler.is_alive() and not settled
    with pytest.raises(supervisor.LaunchClosed):
        gate.launch(["late"], root=False)
    release.set()
    launcher.join(3)
    settler.join(3)
    assert settled == [((process, False),)]


def test_creation_error_after_possible_fork_is_ambiguous(owner):
    def spawn(argv, **options):
        raise OSError("acknowledgement lost after fork")

    owner.gate = supervisor.LaunchGate(spawn=spawn)
    with pytest.raises(OSError):
        owner.gate.launch(["relay"], root=False)
    owner.settle_launches()
    assert owner.failed
    assert json.loads((owner_evidence(owner) / "failure.json").read_bytes())[
        "phase"] == "launch_ambiguous"


def owner_evidence(owner):
    return supervisor.EVIDENCE


def test_reaped_helper_is_never_signalled(owner):
    process = Process()
    gate = supervisor.LaunchGate(spawn=lambda *a, **k: process)
    gate.launch(["relay"], root=False)
    gate.wait(process)
    gate.kill(process)
    assert owner.killed == []
    live = Process(pid=99)
    gate = supervisor.LaunchGate(spawn=lambda *a, **k: live)
    gate.launch(["relay"], root=False)
    gate.kill(live)
    assert owner.killed == [99]


def test_settlement_of_already_reaped_root_helper_is_not_failure(owner, monkeypatch):
    reader = Process(pid=60)
    owner.gate = supervisor.LaunchGate(spawn=lambda *a, **k: reader)
    owner.gate.launch(["capture_history.py"], root=True)
    # The history lane reaped its reader before the terminal fence.
    assert owner.gate.wait(reader) == 0

    def reaped(pid):
        raise ChildProcessError("ECHILD")

    monkeypatch.setattr(supervisor, "wait_exit", reaped)
    monkeypatch.setattr(owner, "collect", lambda: None)
    owner.terminal.set()
    owner.start_cleanup()
    assert owner._collected.wait(3)
    assert not owner.failed and owner.killed == []


def test_failed_launch_settlement_skips_collector_and_retains(owner, monkeypatch):
    process = Process(pid=80)
    owner.gate = supervisor.LaunchGate(spawn=lambda *a, **k: process)
    owner.gate.launch(["capture_history.py"], root=True)

    def lost(pid):
        raise OSError("wait failed")

    monkeypatch.setattr(supervisor, "wait_exit", lost)
    collected = []
    monkeypatch.setattr(owner, "collect", lambda: collected.append(True))
    owner.terminal.set()
    owner.start_cleanup()
    assert owner._collected.wait(3)
    assert owner.failed and collected == []
    assert json.loads((owner_evidence(owner) / "failure.json").read_bytes())[
        "phase"] == "launch_settlement"


def test_launch_evidence_is_never_read_from_reaped_helper(owner):
    process = Process(pid=90)
    owner.gate = supervisor.LaunchGate(spawn=lambda *a, **k: process)
    owner.gate.launch(["relay"], root=False)
    owner.gate.wait(process)
    owner.capture._task = lambda *args: pytest.fail("read a possibly reused PID")
    owner.record_launch("http", 0, process)
    assert owner.failed
    assert not (owner_evidence(owner) / "launch-http-0.json").exists()


def test_echild_without_recorded_reap_still_fails_closed(monkeypatch):
    process = Process(pid=70)
    gate = supervisor.LaunchGate(spawn=lambda *a, **k: process)
    gate.launch(["relay"], root=False)

    def lost(pid):
        raise ChildProcessError("ECHILD")

    monkeypatch.setattr(supervisor, "wait_exit", lost)
    with pytest.raises(ChildProcessError):
        gate.wait(process)


def test_http_relay_runs_unprivileged_and_reframes_output_as_data(owner):
    body = b'{"x":1}'
    process = Process(relay_output(
        {"status": 200, "headers": [["Content-Type", "application/json"]]},
        {"chunk": base64.b64encode(b"ok").decode()}, {"end": True},
    ))
    http, _ = lanes(owner, process)
    frames = complete_http(owner, http, method="POST", path="/session", body=body)
    assert [f["kind"] for f in frames] == ["http_head", "http_chunk", "http_end"]
    assert base64.b64decode(frames[1]["base64"]) == b"ok"
    argv, options, _ = owner.launched[0]
    assert argv == [supervisor.PYTHON, "-I", "-S", "-u", "-B",
                    "/authority/native_http_relay.py"]
    assert (options["user"], options["group"], options["extra_groups"]) == (
        supervisor.UID, supervisor.UID, [])
    assert json.loads(process.stdin.written) == {
        "body": base64.b64encode(body).decode(), "method": "POST",
        "path": "/session"}
    assert process.returncode == 0 and http.active is None
    assert (owner_evidence(owner) / "launch-http-0.json").exists()
    assert not owner.failed


@pytest.mark.parametrize("fault", ["replay", "skip", "overlap", "forbidden",
                                   "short", "get_body"])
def test_http_request_identity_and_shape_fail_closed(owner, fault):
    process = Process(relay_output({"status": 200, "headers": []}, {"end": True}))
    http, _ = lanes(owner, process, Process())
    if fault == "replay":
        complete_http(owner, http)
        frames = request(0)
    elif fault == "skip":
        frames = request(1)
    elif fault == "overlap":
        http.deliver(request(0)[0])
        frames = request(1)
    elif fault == "forbidden":
        frames = request(0, method="DELETE", path="/session")
    elif fault == "short":
        frames = request(0, method="POST", path="/session", body=b"{}")
        frames[0]["bytes"] = 3
    else:
        frames = request(0)
        frames[0]["bytes"] = 2
    with pytest.raises(ValueError):
        for frame in frames:
            http.deliver(frame)


@pytest.mark.parametrize("output", [
    relay_output({"kind": "fence", "run_id": "a" * 32}),
    relay_output({"status": 200, "headers": []},
                 {"kind": "terminal_collection_finished"}),
    relay_output({"status": 200, "headers": []}, {"end": True}) + b"trailing",
    b'{"status":200,"headers":[]}',
])
def test_control_looking_or_malformed_relay_output_fails_run(owner, output):
    http, _ = lanes(owner, Process(output))
    for frame in request(0):
        http.deliver(frame)
    while True:
        try:
            frame = owner.emitted.get(timeout=0.2)
        except queue.Empty:
            break
        assert frame["kind"] in {"http_head"}
        ack(http, frame)
    for thread in http.threads:
        thread.join(3)
    assert owner.failed and owner.terminal.is_set()
    phase = json.loads((owner_evidence(owner) / "failure.json").read_bytes())["phase"]
    assert phase == "http_operation"


@pytest.mark.parametrize("fault", ["stderr", "exit"])
def test_relay_stderr_or_exit_status_is_not_success(owner, fault):
    http, _ = lanes(owner, Process(relay_output(
        {"status": 200, "headers": []}, {"end": True}),
        stderr=b"warning" if fault == "stderr" else b"",
        code=1 if fault == "exit" else 0))
    for frame in request(0):
        http.deliver(frame)
    ack(http, next_frame(owner))
    for thread in http.threads:
        thread.join(3)
    assert owner.failed and owner.emitted.empty()


def test_blocked_response_credit_is_interrupted_by_terminal_fence(owner):
    http, _ = lanes(owner, Process(relay_output(
        {"status": 200, "headers": []}, {"end": True})))
    for frame in request(0):
        http.deliver(frame)
    assert next_frame(owner)["kind"] == "http_head"
    owner.terminal.set()
    for thread in http.threads:
        thread.join(3)
        assert not thread.is_alive()
    # The unacknowledged operation is incomplete, not a reusable rollback.
    assert owner.failed and http.active == 0


def test_history_reader_runs_as_root_and_returns_raw_bytes(owner):
    page = b'{"page":true}' * 5000
    process = Process(page, code=0)
    _, history = lanes(owner, process)
    history.deliver({"kind": "history_request", "id": 0,
                     "request": {"session_id": "ses_a", "after": -1}})
    received = bytearray()
    while True:
        frame = next_frame(owner)
        ack(history, frame)
        if frame["kind"] == "history_end":
            break
        received.extend(base64.b64decode(frame["base64"]))
    for thread in history.threads:
        thread.join(3)
    assert bytes(received) == page
    assert frame["returncode"] == 0 and frame["stderr"] == ""
    argv, options, _ = owner.launched[0]
    assert argv[-1] == "/authority/capture_history.py" and "user" not in options
    assert json.loads(process.stdin.written) == {"after": -1, "session_id": "ses_a"}
    assert history.active is None and not owner.failed


@pytest.mark.parametrize("request_value", [
    {"session_id": "ses_a"}, {"session_id": "ses_a", "after": -1, "path": "/"},
    "not-a-dict",
])
def test_history_request_fields_are_fixed(owner, request_value):
    _, history = lanes(owner)
    with pytest.raises(ValueError):
        history.deliver({"kind": "history_request", "id": 0,
                         "request": request_value})


def test_terminal_settlement_reaps_root_helpers_before_collector(owner, monkeypatch):
    relay_process, reader = Process(pid=50), Process(pid=60)
    owner.gate = supervisor.LaunchGate(spawn=lambda argv, **k: (
        reader if argv[-1].endswith("history.py") else relay_process))
    owner.gate.launch(["relay.py"], root=False)
    owner.gate.launch(["capture_history.py"], root=True)
    order = []
    monkeypatch.setattr(owner, "collect", lambda: order.append(
        (relay_process.returncode, reader.returncode)))
    owner.terminal.set()
    owner.start_cleanup()
    assert owner._collected.wait(3)
    assert sorted(owner.killed) == [50, 60]
    # Root reader reaped before the census; the unprivileged relay only killed.
    assert order == [(None, 0)]


def test_active_operation_at_terminal_is_failure(owner, monkeypatch):
    http, _ = lanes(owner)
    http.deliver(request(0)[0])
    monkeypatch.setattr(owner, "collect", lambda: None)
    owner.terminal.set()
    owner.start_cleanup()
    assert owner._collected.wait(3)
    assert owner.failed


def test_control_loop_routes_helper_frames_but_not_to_model_bridge(owner):
    http, _ = lanes(owner, Process(relay_output(
        {"status": 200, "headers": []}, {"end": True})))
    delivered, controls = [], []
    owner.bridge.deliver = delivered.append
    binding = {"run_id": "a" * 32, "runtime_sha256": "b" * 64}
    finished = threading.Event()

    class Stream(io.BytesIO):
        def readline(self, limit=-1):
            line = super().readline(limit)
            if not line:
                assert finished.wait(3)
            return line

    stream = Stream(b"".join(supervisor.canonical(f) for f in [
        *request(0), {"kind": "model_head", "id": "m"},
    ]))
    reader = threading.Thread(target=owner.control_loop,
                              args=(stream, binding, controls.append), daemon=True)
    reader.start()
    for _ in range(2):
        ack(http, next_frame(owner))
    for thread in http.threads:
        thread.join(3)
    finished.set()
    reader.join(3)
    for thread in http.threads:
        thread.join(3)
    assert delivered == [{"kind": "model_head", "id": "m"}]
    assert http.active is None and len(owner.launched) == 1
    # The following control EOF is the only failure; relay output is not control.
    assert json.loads((owner_evidence(owner) / "failure.json").read_bytes())[
        "phase"] == "control_eof"


def test_readiness_is_connect_only_and_stops_at_terminal(owner, monkeypatch):
    attempts = []

    def connect(address, timeout):
        attempts.append(address)
        if len(attempts) < 3:
            raise ConnectionRefusedError()
        return io.BytesIO()

    monkeypatch.setattr(supervisor.socket, "create_connection", connect)
    monkeypatch.setattr(owner.terminal, "wait", lambda timeout: False)
    emitted = []
    supervisor.await_listening(owner, emitted.append, {"run_id": "r"})
    assert attempts == [("127.0.0.1", 4096)] * 3
    assert emitted == [{"kind": "native_listening", "run_id": "r"}]
    owner.terminal.set()
    supervisor.await_listening(owner, emitted.append, {"run_id": "r"})
    assert len(emitted) == 1
