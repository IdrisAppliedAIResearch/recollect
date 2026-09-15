"""Trusted native PID 1; never run this helper on the host or from worker source.

The host independently attests the namespace before releasing this supervisor.
Only fixed read-only helpers are imported. EOF fences model traffic and attempts
terminal collection, retaining the namespace for host evidence recovery.
"""

import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import queue
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

AUTHORITY = Path("/authority")
EVIDENCE = Path("/evidence")
WORK = Path("/work")
MAX_FRAME = 4 * 1024 * 1024
UID = 65532
HELPERS = {"native_supervisor.py", "native_model_proxy.py", "native_http_relay.py",
           "capture_worker.py", "capture_history.py"}
PYTHON = "/usr/local/bin/python"
HELPER_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
IPC_CHUNK_BYTES = 32768
HISTORY_BYTES = 1024 * 1024
HELPER_STDERR_BYTES = 4096
HISTORY_FIELDS = {"session_id", "after", "through", "last_event_sha256", "offset",
                  "row_sha256"}


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_frame(stream):
    raw = stream.readline(MAX_FRAME + 1)
    if not raw:
        return None
    value = json.loads(raw)
    if (len(raw) > MAX_FRAME or type(value) is not dict
            or canonical(value) != raw):
        raise ValueError("Invalid supervisor frame")
    return value


def trusted_bytes(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (info.st_uid, info.st_gid) != (0, 0)):
        raise ValueError("Untrusted supervisor input")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        opened = os.fstat(source.fileno())
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Supervisor input replaced")
        raw = source.read(MAX_FRAME + 1)
    if len(raw) > MAX_FRAME:
        raise ValueError("Supervisor input exceeds bound")
    return raw


def load_helper(name, expected):
    path = AUTHORITY / name
    if name not in HELPERS or digest(trusted_bytes(path)) != expected:
        raise ValueError("Supervisor helper identity mismatch")
    spec = importlib.util.spec_from_file_location("_native_" + name[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_manifest(raw):
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {"payload", "sha256"}
            or canonical(value) != raw or type(value["payload"]) is not dict
            or digest(canonical(value["payload"])) != value["sha256"]):
        raise ValueError("Invalid supervisor manifest")
    payload = value["payload"]
    if (set(payload) != {"version", "run_id", "capture_spec_sha256",
                         "native_binary_sha256", "helpers", "image_id",
                         "image_environment", "isolation"}
            or type(payload["version"]) is not int or payload["version"] != 1
            or type(payload["run_id"]) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", payload["run_id"])
            or type(payload["helpers"]) is not dict
            or set(payload["helpers"]) != HELPERS):
        raise ValueError("Invalid supervisor manifest fields")
    for expected in (payload["capture_spec_sha256"], payload["native_binary_sha256"],
                     *payload["helpers"].values()):
        if type(expected) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Invalid supervisor digest")
    return value


def provision(files, policy):
    if list(WORK.iterdir()):
        raise ValueError("Native work directory is not empty")
    for name, content in sorted(files.items()):
        target = WORK / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(content)
        writable = name in policy["modify"]
        os.chown(target, 0, UID if writable else 0)
        os.chmod(target, 0o664 if writable else 0o444)
    for name in policy["create_under"]:
        (WORK / name).mkdir(parents=True)
    for directory, _, _ in os.walk(WORK, topdown=False):
        path = Path(directory)
        writable = path.relative_to(WORK).as_posix() in policy["create_under"]
        os.chown(path, 0, UID if writable else 0)
        os.chmod(path, 0o775 if writable else 0o555)


def supervisor_only():
    if (sys.platform != "linux" or os.getpid() != 1 or os.geteuid() != 0
            or os.getegid() != 0 or not sys.flags.isolated or not sys.flags.no_site
            or Path(__file__) != AUTHORITY / "native_supervisor.py"):
        raise ValueError("Fixed isolated native namespace PID 1 required")
    for path in (AUTHORITY, EVIDENCE, WORK):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid) != (0, 0)
                or info.st_mode & 0o022):
            raise ValueError("Root-owned supervisor directories required")
    if not os.statvfs(AUTHORITY).f_flag & os.ST_RDONLY:
        raise ValueError("Supervisor authority must be read-only")


def drain_log(stream, path, fail):
    retained = 0
    truncated = False
    output = None
    healthy = True
    try:
        try:
            output = path.open("xb")
        except OSError as error:
            healthy = False
            fail("log_open", error)
        while chunk := stream.read(8192):
            prefix = chunk[:max(0, 65536 - retained)]
            if healthy:
                try:
                    output.write(prefix)
                    retained += len(prefix)
                except OSError as error:
                    healthy = False
                    fail("log_write", error)
            truncated |= len(prefix) != len(chunk)
        if output is not None:
            output.close()
            output = None
        path.with_suffix(path.suffix + ".json").write_bytes(canonical({
            "bytes_retained": retained, "truncated": truncated,
            "complete": healthy,
        }))
    except BaseException as error:
        fail("log_drain", error)
    finally:
        for owned in (output, stream):
            if owned is not None:
                try:
                    owned.close()
                except BaseException as error:
                    fail("log_close", error)


def wait_exit(pid):
    # Observe exit without reaping, so a concurrent kill never hits a reused PID.
    os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)


def send_kill(pid):
    os.kill(pid, signal.SIGKILL)


class LaunchClosed(ValueError):
    """The irreversible terminal gate won; no new helper may be created."""


class LaunchGate:
    """PID 1's only helper-creation path; there are no caller-selected commands.

    Registration precedes spawning, and no lock is held across process creation
    or pipe writes. Children are reaped only under the kill lock, so a signal
    never reaches a recycled PID. A creation error may follow a fork: it latches
    ambiguity, which fails the run and leaves reconciliation to the census.
    """

    def __init__(self, spawn=subprocess.Popen):
        self._spawn = spawn
        self._state = threading.Condition()
        self._reap = threading.Lock()
        self._closed = False
        self._pending = 0
        self._children = []
        self.ambiguous = False

    def launch(self, argv, *, root, **options):
        with self._state:
            if self._closed:
                raise LaunchClosed("Helper launch gate is closed")
            self._pending += 1
        process = None
        try:
            process = self._spawn(argv, **options)
        except BaseException:
            with self._state:
                self.ambiguous = True
            raise
        finally:
            with self._state:
                if process is not None:
                    self._children.append((process, root))
                self._pending -= 1
                self._state.notify_all()
        return process

    def settle_creation(self):
        """Close admission, then wait without deadline for in-flight creation."""
        with self._state:
            self._closed = True
            while self._pending:
                self._state.wait()
            return tuple(self._children)

    def kill(self, process):
        with self._reap:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    send_kill(process.pid)

    def observe(self, process, read):
        """Read helper process state only while it is provably unreaped."""
        with self._reap:
            if process.returncode is not None:
                raise LaunchClosed("Helper was reaped before observation")
            return read(process.pid)

    def wait(self, process):
        # Both a lane thread and terminal settlement may wait for one helper.
        with self._reap:
            if process.returncode is not None:
                return process.returncode
        try:
            wait_exit(process.pid)
        except ChildProcessError:
            # Only this lock reaps; ECHILD is benign only once its status is recorded.
            with self._reap:
                if process.returncode is None:
                    raise
                return process.returncode
        with self._reap:
            return process.wait()


class HelperLane:
    """One host-identified operation at a time; helper output is data only.

    Identities must increase by exactly one, so a replayed request is rejected
    without retaining a growing set. Every emitted data frame waits for host
    credit while polling only the terminal fence; there is no work deadline.
    """

    name = "helper"

    def __init__(self, owner, emit):
        self.owner, self._emit = owner, emit
        self._state = threading.Condition()
        self._next = 0
        self.active = None
        self._receiving = False
        self._unacknowledged = None
        self._final = False
        self.threads = []

    def _begin(self, identity):
        if (type(identity) is not int or identity != self._next
                or self.active is not None):
            raise ValueError("Stale, replayed or overlapping helper operation")
        self.active = identity

    def _send(self, frame, *, final=False):
        with self._state:
            if self.active != frame["id"] or self._unacknowledged is not None:
                raise ValueError("Helper output without owned credit")
            self._unacknowledged, self._final = frame["sequence"], final
        self._emit(frame)
        with self._state:
            while self._unacknowledged is not None:
                if self.owner.terminal.is_set():
                    raise LaunchClosed("Terminal fence interrupted helper output")
                self._state.wait(0.1)

    def acknowledge(self, frame):
        with self._state:
            if (set(frame) != {"kind", "id", "sequence"}
                    or self.active is None or frame["id"] != self.active
                    or type(frame["sequence"]) is not int
                    or frame["sequence"] != self._unacknowledged):
                raise ValueError("Invalid helper acknowledgement")
            self._unacknowledged = None
            if self._final:
                # The host observes completion only by acknowledging the final
                # frame, so idleness changes atomically with that acknowledgement.
                self.active = None
                self._next += 1
            self._state.notify_all()

    def _start(self, target, *args):
        thread = threading.Thread(target=self._guarded, args=(target, *args),
                                  daemon=True)
        # Register only started threads, so terminal collection can always join.
        thread.start()
        self.threads.append(thread)

    def _guarded(self, target, *args):
        try:
            target(*args)
        except BaseException as error:
            # An incomplete operation, including one interrupted by the fence,
            # fails the run: there is no reusable rollback of native effects.
            self.owner.fail(self.name + "_operation", error)

    def _stderr(self, stream, retained):
        try:
            # Any byte is retained, so an empty prefix proves empty stderr.
            while chunk := stream.read(8192):
                retained.extend(chunk[:max(0, HELPER_STDERR_BYTES - len(retained))])
        except BaseException as error:
            self.owner.fail(self.name + "_stderr", error)

    def _drain_stderr(self, process):
        retained = bytearray()
        thread = threading.Thread(target=self._stderr, args=(process.stderr, retained),
                                  daemon=True)
        thread.start()
        return thread, retained


class HttpLane(HelperLane):
    """Launch the fixed unprivileged relay for one validated native HTTP request."""

    name = "http"

    def __init__(self, owner, emit, relay):
        super().__init__(owner, emit)
        self.relay = relay
        self._request = None

    def deliver(self, frame):
        kind = frame.get("kind")
        if kind == "http_ack":
            return self.acknowledge(frame)
        with self._state:
            if kind == "http_request":
                if set(frame) != {"kind", "id", "method", "path", "bytes"}:
                    raise ValueError("Invalid native HTTP request fields")
                self.relay.validate_target(frame["method"], frame["path"])
                size = frame["bytes"]
                if (type(size) is not int or not 0 <= size <= self.relay.BODY_BYTES
                        or frame["method"] == "GET" and size):
                    raise ValueError("Invalid native HTTP request size")
                self._begin(frame["id"])
                self._receiving = True
                self._request = (frame["method"], frame["path"], size, bytearray(), [0])
                return None
            if not self._receiving or frame.get("id") != self.active:
                raise ValueError("Native HTTP frame outside owned request")
            method, path, size, body, chunks = self._request
            if kind == "http_request_chunk":
                if (set(frame) != {"kind", "id", "sequence", "base64"}
                        or frame["sequence"] != chunks[0]
                        or type(frame["base64"]) is not str
                        or len(frame["base64"]) > 4 * ((IPC_CHUNK_BYTES + 2) // 3)):
                    raise ValueError("Invalid native HTTP request chunk")
                data = base64.b64decode(frame["base64"], validate=True)
                if not data or len(body) + len(data) > size:
                    raise ValueError("Native HTTP request chunk exceeds declared size")
                body.extend(data)
                chunks[0] += 1
                return None
            if (kind != "http_request_end" or set(frame) != {"kind", "id", "chunks"}
                    or frame["chunks"] != chunks[0] or len(body) != size):
                raise ValueError("Invalid native HTTP request terminator")
            self._receiving, self._request = False, None
        self._start(self._run, self.active, method, path, bytes(body))
        return None

    def _frame(self, stream):
        raw = stream.readline(self.relay.FRAME_BYTES + 1)
        if len(raw) > self.relay.FRAME_BYTES or not raw.endswith(b"\n"):
            raise ValueError("Unterminated or oversized relay frame")
        value = json.loads(raw, object_pairs_hook=self.relay.pairs)
        if type(value) is not dict:
            raise ValueError("Relay frame is not an object")
        return value

    def _run(self, identity, method, path, body):
        request = (json.dumps({"body": base64.b64encode(body).decode(),
                               "method": method, "path": path},
                              sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False) + "\n").encode()
        process = self.owner.gate.launch(
            [PYTHON, "-I", "-S", "-u", "-B",
             AUTHORITY.as_posix() + "/native_http_relay.py"],
            root=False, cwd="/", user=UID, group=UID, extra_groups=[],
            env=dict(HELPER_ENV), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.owner.record_launch("http", identity, process)
        stderr, retained = self._drain_stderr(process)
        process.stdin.write(request)
        process.stdin.close()
        header = self._frame(process.stdout)
        if (set(header) != {"status", "headers"} or type(header["status"]) is not int
                or not 100 <= header["status"] <= 599
                or type(header["headers"]) is not list
                or any(type(pair) is not list or len(pair) != 2
                       or any(type(v) is not str or not v.isascii() for v in pair)
                       for pair in header["headers"])):
            raise ValueError("Invalid relay response header")
        self._send({"kind": "http_head", "id": identity, "sequence": 0,
                    "status": header["status"], "headers": header["headers"]})
        sequence, total = 1, 0
        while True:
            frame = self._frame(process.stdout)
            if frame == {"end": True}:
                if process.stdout.read(1):
                    raise ValueError("Trailing relay output")
                code = self.owner.gate.wait(process)
                stderr.join()
                if code != 0 or retained:
                    raise ValueError("Relay did not finish cleanly")
                self._send({"kind": "http_end", "id": identity, "sequence": sequence},
                           final=True)
                return
            if set(frame) != {"chunk"} or type(frame["chunk"]) is not str:
                raise ValueError("Invalid relay body frame")
            data = base64.b64decode(frame["chunk"], validate=True)
            total += len(data)
            if not 0 < len(data) <= self.relay.CHUNK_BYTES or (
                    total > self.relay.BODY_BYTES):
                raise ValueError("Relay body exceeds bound")
            self._send({"kind": "http_chunk", "id": identity, "sequence": sequence,
                        "base64": frame["chunk"]})
            sequence += 1


class HistoryLane(HelperLane):
    """Launch the fixed root SQLite reader; its bytes are validated by the host."""

    name = "history"

    def deliver(self, frame):
        kind = frame.get("kind")
        if kind == "history_ack":
            return self.acknowledge(frame)
        request = frame.get("request")
        if (kind != "history_request" or set(frame) != {"kind", "id", "request"}
                or type(request) is not dict
                or not {"session_id", "after"} <= request.keys() <= HISTORY_FIELDS
                or len(canonical(request)) > 4096):
            raise ValueError("Invalid native history request")
        with self._state:
            self._begin(frame["id"])
        self._start(self._run, frame["id"], canonical(request))
        return None

    def _run(self, identity, request):
        process = self.owner.gate.launch(
            [PYTHON, "-I", "-S", "-u", "-B",
             AUTHORITY.as_posix() + "/capture_history.py"],
            root=True, cwd="/", env=dict(HELPER_ENV), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.owner.record_launch("history", identity, process)
        stderr, retained = self._drain_stderr(process)
        process.stdin.write(request)
        process.stdin.close()
        sequence, total = 0, 0
        while chunk := process.stdout.read(IPC_CHUNK_BYTES):
            total += len(chunk)
            if total > HISTORY_BYTES:
                raise ValueError("Native history page exceeds bound")
            self._send({"kind": "history_chunk", "id": identity, "sequence": sequence,
                        "base64": base64.b64encode(chunk).decode()})
            sequence += 1
        code = self.owner.gate.wait(process)
        stderr.join()
        self._send({"kind": "history_end", "id": identity, "sequence": sequence,
                    "returncode": code,
                    "stderr": base64.b64encode(bytes(retained)).decode()}, final=True)


def await_listening(owner, emit, binding, port=4096):
    """Connect-only readiness; no request, helper launch or native work deadline."""
    try:
        while not owner.terminal.is_set():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    pass
            except OSError:
                owner.terminal.wait(0.1)
                continue
            emit({"kind": "native_listening", **binding})
            return
    except BaseException as error:
        owner.fail("native_listening", error)


class NativeLifecycle:
    """Own post-release failures even when control input or evidence IO breaks."""

    def __init__(self, bridge, capture, spec_sha256):
        self.bridge = bridge
        self.capture = capture
        self.spec_sha256 = spec_sha256
        self.gate = LaunchGate()
        self.lanes = ()
        self.terminal = threading.Event()
        self._failure_lock = threading.Lock()
        self.failed = False
        self.process = None
        self.identity = None
        self.drainers = []
        self._cleanup_lock = threading.Lock()
        self._cleanup_thread = None
        self._collected = threading.Event()
        self._children = ()
        self.collector_returncode = None
        self.on_collected = lambda: None

    def fail(self, phase, error):
        # Wake ownership independently of stdout and evidence-disk availability.
        with self._failure_lock:
            first = not self.failed
            self.failed = True
        self.terminal.set()
        with contextlib.suppress(BaseException):
            self.bridge.close()
        if first:
            with contextlib.suppress(OSError):
                (EVIDENCE / "failure.json").write_bytes(canonical({
                    "phase": phase, "error_type": type(error).__name__,
                }))

    def observe_identity(self):
        native = self.capture._task(Path("/proc"), self.process.pid, self.process.pid)
        self.identity = {"pid": self.process.pid, "start": native["start"]}

    def record_launch(self, lane, identity, process):
        try:
            # Held under the reap lock, so credentials cannot belong to a reused PID.
            task = self.gate.observe(process, lambda pid: self.capture._task(
                Path("/proc"), pid, pid))
            (EVIDENCE / f"launch-{lane}-{identity}.json").write_bytes(canonical(task))
        except BaseException as error:
            self.fail("launch_evidence", error)

    def settle_launches(self):
        """Creation settlement precedes collection; root helpers must be reaped.

        Unprivileged relays are killed here and reaped only after the collector,
        so their exit is never a precondition for stopping terminal workers.
        """
        children = self.gate.settle_creation()
        if self.gate.ambiguous:
            self.fail("launch_ambiguous", ChildProcessError())
        if any(lane.active is not None for lane in self.lanes):
            self.fail("helper_incomplete", ChildProcessError())
        for process, _ in children:
            self.gate.kill(process)
        for process, root in children:
            if root:
                self.gate.wait(process)
        return children

    def watch_exit(self):
        try:
            # Preserve the zombie/start identity for the independent collector.
            exited = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOWAIT)
            if not self.terminal.is_set():
                self.fail("native_exit", ChildProcessError())
            (EVIDENCE / "native-exit.json").write_bytes(canonical({
                "pid": exited.si_pid, "code": exited.si_code,
                "status": exited.si_status,
            }))
        except BaseException as error:
            self.fail("native_exit_observation", error)

    def controls(self, stream):
        inbox = queue.Queue(maxsize=1)

        def read():
            try:
                while not self.terminal.is_set():
                    frame = read_frame(stream)
                    while not self.terminal.is_set():
                        try:
                            inbox.put(frame, timeout=0.1)
                            break
                        except queue.Full:
                            continue
                    if frame is None:
                        return
            except BaseException as error:
                self.fail("control_read", error)

        threading.Thread(target=read, daemon=True).start()
        while not self.terminal.is_set():
            if self.bridge.failed.is_set():
                self.fail("model_exchange", ValueError())
                break
            try:
                frame = inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if self.bridge.failed.is_set():
                self.fail("model_exchange", ValueError())
                break
            if frame is None:
                self.fail("control_eof", EOFError())
                break
            yield frame

    def collect(self):
        if self.process is None:
            return
        if self.identity is None:
            self.observe_identity()
        outputs = []
        try:
            for name in ("disconnect-stop.json", "disconnect-stop.stderr"):
                try:
                    outputs.append((EVIDENCE / name).open("xb"))
                except OSError as error:
                    self.fail("collector_evidence", error)
                    outputs.append(subprocess.DEVNULL)
            result = subprocess.run(
                ["/usr/local/bin/python", "-I", "-S", "-u", "-B",
                 "/authority/capture_worker.py"],
                input=canonical({"kind": "stop", "spec_sha256": self.spec_sha256,
                                 "native": self.identity}),
                stdout=outputs[0], stderr=outputs[1], check=False,
            )
            self.collector_returncode = result.returncode
            try:
                (EVIDENCE / "collector-result.json").write_bytes(canonical({
                    "returncode": result.returncode,
                }))
            except BaseException as error:
                self.fail("collector_result", error)
            if result.returncode != 0:
                self.fail("collector_exit", ChildProcessError())
            else:
                # No work deadline: join only after the collector proves all
                # workers stopped. A failed collector leaves settlement unknown.
                for thread in self.drainers:
                    thread.join()
                for process, root in self._children:
                    if not root:
                        self.gate.wait(process)
                for lane in self.lanes:
                    for thread in lane.threads:
                        thread.join()
        finally:
            for output in outputs:
                if output != subprocess.DEVNULL:
                    try:
                        output.close()
                    except BaseException as error:
                        self.fail("collector_output_close", error)

    def retain(self):
        threading.Event().wait()

    def _cleanup(self):
        self.terminal.wait()
        try:
            if self.bridge.failed.is_set():
                self.fail("model_exchange", ValueError())
            try:
                self.bridge.close()
            except BaseException as error:
                self.fail("bridge_close", error)
            settled = False
            try:
                self._children = self.settle_launches()
                settled = True
            except BaseException as error:
                self.fail("launch_settlement", error)
            if settled:
                # An unsettled root helper would fail the census; retain instead.
                try:
                    self.collect()
                except BaseException as error:
                    self.fail("collector", error)
        finally:
            if self.bridge.failed.is_set():
                self.fail("model_exchange", ValueError())
            self._collected.set()
            # A notification is useful to avoid probing with extra root exec
            # peers during the collector's exclusive process census. Its delivery
            # is not required for collection or retained-namespace ownership.
            try:
                self.on_collected()
            except BaseException as error:
                self.fail("collection_notification", error)

    def start_cleanup(self):
        with self._cleanup_lock:
            if self._cleanup_thread is None:
                thread = threading.Thread(target=self._cleanup, daemon=True)
                thread.start()
                self._cleanup_thread = thread

    def finish(self):
        self.terminal.set()
        try:
            self.start_cleanup()
            self._collected.wait()
        finally:
            # Even failed PID observation, disk IO or collector launch must not
            # destroy tmpfs evidence by allowing PID 1 to exit.
            self.retain()

    def control_loop(self, stream, binding, emit):
        try:
            for frame in self.controls(stream):
                if frame == {"kind": "fence", **binding}:
                    self.terminal.set()
                    if self.bridge.failed.is_set():
                        self.fail("model_exchange", ValueError())
                    self.bridge.close()
                    # The independent collector does not await this output.
                    emit({"kind": "fenced", **binding})
                    return
                kind = frame.get("kind")
                # Supervisor actions come only from host control input. Helper
                # stdout is re-framed as bounded data and never parsed as control.
                lane = next((lane for lane in self.lanes if type(kind) is str
                             and kind.startswith(lane.name + "_")), None)
                if lane is not None:
                    lane.deliver(frame)
                else:
                    self.bridge.deliver(frame)
        except BaseException as error:
            self.fail("native_control", error)


def run_native(proxy, capture, envelope, binding, emit, stream, relay=None):
    bridge = proxy.ModelBridge(emit)
    owner = NativeLifecycle(bridge, capture, envelope["spec_sha256"])
    if relay is not None:
        owner.lanes = (HttpLane(owner, emit, relay), HistoryLane(owner, emit))
    owner.on_collected = lambda: emit({
        "kind": "terminal_collection_finished", **binding,
        "collector_returncode": owner.collector_returncode, "failed": owner.failed,
    })
    try:
        server = proxy.listener(bridge)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        owner.process = subprocess.Popen(
            ["/usr/local/bin/opencode", "serve", "--hostname", "127.0.0.1",
             "--port", "4096"], cwd=WORK, user=UID, group=UID, extra_groups=[],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/state/home",
                 "XDG_DATA_HOME": "/state/data", "XDG_CACHE_HOME": "/state/cache",
                 "XDG_CONFIG_HOME": "/state/config",
                 "OPENCODE_CONFIG": "/authority/opencode.json",
                 "OPENCODE_DISABLE_MODELS_FETCH": "true"},
        )
        owner.observe_identity()
        (EVIDENCE / "native.json").write_bytes(canonical(owner.identity))
        for output, name in ((owner.process.stdout, "native.stdout"),
                             (owner.process.stderr, "native.stderr")):
            thread = threading.Thread(target=drain_log,
                                      args=(output, EVIDENCE / name, owner.fail),
                                      daemon=True)
            thread.start()
            owner.drainers.append(thread)
        threading.Thread(target=owner.watch_exit, daemon=True).start()
        # Both terminal control and collection remain independent of blocked
        # stdout. There is only one collector, even if several failures race.
        owner.start_cleanup()
        threading.Thread(target=owner.control_loop, args=(stream, binding, emit),
                         daemon=True).start()
        emit({"kind": "native_started", **binding, "native": owner.identity})
        threading.Thread(target=await_listening, args=(owner, emit, binding),
                         daemon=True).start()
        owner.terminal.wait()
    except BaseException as error:
        owner.fail("native_lifecycle", error)
    finally:
        owner.finish()


def main():
    supervisor_only()
    manifest = load_manifest(trusted_bytes(AUTHORITY / "runtime-spec.json"))
    payload = manifest["payload"]
    for name, expected in payload["helpers"].items():
        if digest(trusted_bytes(AUTHORITY / name)) != expected:
            raise ValueError("Pinned supervisor helpers changed")
    capture = load_helper("capture_worker.py", payload["helpers"]["capture_worker.py"])
    envelope, files = capture.load_spec(trusted_bytes(AUTHORITY / "capture-spec.json"))
    if (envelope["spec_sha256"] != payload["capture_spec_sha256"]
            or envelope["payload"]["run"]["run_id"] != payload["run_id"]):
        raise ValueError("Supervisor capture/run binding mismatch")
    binary_hash = hashlib.sha256()
    with open("/usr/local/bin/opencode", "rb") as binary:
        while chunk := binary.read(1024 * 1024):
            binary_hash.update(chunk)
    if binary_hash.hexdigest() != payload["native_binary_sha256"]:
        raise ValueError("Native binary identity mismatch")
    policy = envelope["payload"]["policy"]
    provision(files, policy)
    capture.secure_capture(WORK, files, policy)
    write_lock = threading.Lock()

    def emit(value):
        raw = canonical(value)
        if len(raw) > MAX_FRAME:
            raise ValueError("Supervisor output frame exceeds bound")
        with write_lock:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()

    binding = {"run_id": payload["run_id"], "runtime_sha256": manifest["sha256"]}
    emit({"kind": "ready", **binding})
    if read_frame(sys.stdin.buffer) != {"kind": "release", **binding}:
        raise ValueError("Unbound supervisor release")
    proxy = load_helper("native_model_proxy.py",
                        payload["helpers"]["native_model_proxy.py"])
    relay = load_helper("native_http_relay.py",
                        payload["helpers"]["native_http_relay.py"])
    for name in ("native_http_relay.py", "capture_history.py"):
        # Helpers run from the read-only authority mount; the relay user must read it.
        if not (AUTHORITY / name).lstat().st_mode & stat.S_IROTH:
            raise ValueError("Fixed helper is not readable by its launch identity")
    run_native(proxy, capture, envelope, binding, emit, sys.stdin.buffer, relay)


if __name__ == "__main__":
    main()
