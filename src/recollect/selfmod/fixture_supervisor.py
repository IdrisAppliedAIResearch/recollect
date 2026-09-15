"""Trusted stdlib-only fixture supervisor, copied into a read-only Docker mount.

Never execute this on the host. The entrypoint requires container namespace PID 1.
Keep imports independent of the worker tree; launch with an absolute Python -I -S.
"""

import base64
import contextlib
import hashlib
import json
import math
import os
import re
import select
import selectors
import signal
import stat
import sys
import time
from pathlib import Path

UID = 65532
LIMITS = {
    "files": 128,
    "file_bytes": 256 * 1024,
    "source_bytes": 2 * 1024 * 1024,
    "log_bytes": 64 * 1024,
    "path_chars": 240,
    "path_depth": 16,
}
MAX_WIRE = 4 * 1024 * 1024


def canonical(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def decode(data):
    if len(data) > MAX_WIRE:
        raise ValueError("Oversized wire record")
    value = json.loads(data, object_pairs_hook=_pairs)
    if not isinstance(value, dict) or canonical(value) != data:
        raise ValueError("Noncanonical wire record")
    return value


def safe_path(path):
    if (
        not isinstance(path, str)
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
        or len(path) > LIMITS["path_chars"]
        or len(path.split("/")) > LIMITS["path_depth"]
    ):
        raise ValueError("Invalid source path")
    for part in path.split("/"):
        stem = part.split(".")[0].upper()
        if (
            part in {"", ".", ".."}
            or part.endswith(".")
            or stem in {"CON", "PRN", "AUX", "NUL"}
            or re.fullmatch(r"(?:COM|LPT)[0-9]", stem)
        ):
            raise ValueError("Nonportable source path")


def snapshot_digest(files):
    inventory = [
        {"path": path, "bytes": len(data), "sha256": digest(data)}
        for path, data in sorted(files.items())
    ]
    return digest(canonical(inventory)[:-1])


def load_spec(raw):
    envelope = decode(raw)
    if set(envelope) != {
        "payload",
        "image_id",
        "image_environment",
        "spec_sha256",
        "supervisor_sha256",
    }:
        raise ValueError("Invalid input envelope")
    payload = envelope["payload"]
    hashed = {k: envelope[k] for k in ("payload", "image_id", "image_environment")}
    if digest(canonical(hashed)) != envelope["spec_sha256"]:
        raise ValueError("Input digest mismatch")
    if (
        payload["version"] != 1
        or payload["limits"] != LIMITS
        or (payload["timeout_ms"] is not None and (
            type(payload["timeout_ms"]) is not int
            or not 50 <= payload["timeout_ms"] <= 60_000
        ))
    ):
        raise ValueError("Unsupported frozen fixture limits")
    files = {}
    for item in payload["files"]:
        safe_path(item["path"])
        if item["path"] in files:
            raise ValueError("Duplicate source")
        data = base64.b64decode(item["base64"], validate=True)
        if len(data) > LIMITS["file_bytes"]:
            raise ValueError("Source file too large")
        files[item["path"]] = data
    if (
        len(files) > LIMITS["files"]
        or sum(map(len, files.values())) > LIMITS["source_bytes"]
        or snapshot_digest(files) != payload["baseline_sha256"]
        or payload["entrypoint"] not in files
        or payload["policy"]["baseline_sha256"] != payload["baseline_sha256"]
        or payload["policy"]["delete"]
        or not set(payload["policy"]["modify"]) <= files.keys()
    ):
        raise ValueError("Invalid baseline or policy")
    roots = payload["policy"]["create_under"]
    for root in roots:
        safe_path(root)
        if any(
            p == root or p.startswith(root + "/") or root.startswith(p + "/")
            for p in files
        ) or any(r != root and root.startswith(r + "/") for r in roots):
            raise ValueError("Creation root overlaps existing source or another root")
    return envelope, files


def status_fields():
    return dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )


def verify_identity(*, worker):
    fields = status_fields()
    mask = "0" if worker else "e1"
    if (
        os.geteuid() != (UID if worker else 0)
        or os.getegid() != (UID if worker else 0)
        or worker
        and os.getgroups()
        or fields["NoNewPrivs"].strip() != "1"
        or fields["Seccomp"].strip() != "2"
        or any(int(fields[k], 16) != int(mask, 16) for k in ("CapEff", "CapPrm"))
        or any(int(fields[k], 16) for k in ("CapInh", "CapAmb"))
    ):
        raise ValueError("Kernel credential/capability/seccomp attestation failed")


def provision(root, files, policy):
    supervisor_only()
    if root != Path("/work"):
        raise ValueError("Fixed work tmpfs required")
    if list(root.iterdir()):
        raise ValueError("Work tmpfs must start empty")
    for path, data in sorted(files.items()):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(data)
        os.chown(target, 0, UID if path in policy["modify"] else 0)
        os.chmod(target, 0o664 if path in policy["modify"] else 0o444)
    for path in policy["create_under"]:
        (root / path).mkdir(parents=True)
    for directory, _, _ in os.walk(root, topdown=False):
        path = Path(directory)
        relative = path.relative_to(root).as_posix()
        writable = relative in policy["create_under"]
        os.chown(path, 0, UID if writable else 0)
        os.chmod(path, 0o775 if writable else 0o555)


def capture(root, baseline, policy):
    files, entries, errors = {}, [], []
    pending = [root]
    seen = 0
    total = 0
    while pending:
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir())
        except OSError as exc:
            errors.append(str(exc)[:400])
            continue
        for path in children:
            seen += 1
            if seen > LIMITS["files"] * LIMITS["path_depth"]:
                return files, entries, [*errors, "entry_count_exhausted"]
            relative = path.relative_to(root).as_posix()
            try:
                safe_path(relative)
                info = path.lstat()
                mode = stat.S_IMODE(info.st_mode)
                entries.append(
                    {
                        "path": relative,
                        "kind": (
                            "directory"
                            if stat.S_ISDIR(info.st_mode)
                            else "file"
                            if stat.S_ISREG(info.st_mode)
                            else "special"
                        ),
                        "mode": mode,
                        "uid": info.st_uid,
                        "gid": info.st_gid,
                        "links": info.st_nlink,
                        "bytes": info.st_size,
                    }
                )
                if stat.S_ISDIR(info.st_mode):
                    expected = (
                        (0, UID, 0o775)
                        if relative in policy["create_under"]
                        else (
                            (UID, UID, 0o755)
                            if any(
                                relative.startswith(p + "/")
                                for p in policy["create_under"]
                            )
                            else (0, 0, 0o555)
                        )
                    )
                    if (info.st_uid, info.st_gid, mode) != expected:
                        raise ValueError("Directory ownership/mode mismatch")
                    pending.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Linked or special source entry")
                expected = (
                    (0, UID, 0o664)
                    if relative in policy["modify"]
                    else (0, 0, 0o444)
                    if relative in baseline
                    else (UID, UID, 0o644)
                )
                if (info.st_uid, info.st_gid, mode) != expected:
                    raise ValueError("Source ownership/mode mismatch")
                if info.st_size > LIMITS["file_bytes"] or len(files) >= LIMITS["files"]:
                    raise ValueError("Source file/count limit")
                with path.open("rb") as source:
                    data = source.read(LIMITS["file_bytes"] + 1)
                total += len(data)
                if len(data) != info.st_size or total > LIMITS["source_bytes"]:
                    raise ValueError("Source size/inventory mismatch")
                if relative in baseline and relative not in policy["modify"]:
                    if data != baseline[relative]:
                        raise ValueError("Protected source drift")
                elif relative not in baseline and not any(
                    relative.startswith(p + "/") for p in policy["create_under"]
                ):
                    raise ValueError("Out-of-scope creation")
                files[relative] = data
            except (ValueError, OSError) as exc:
                errors.append(relative[:240] + ": " + str(exc)[:400])
    if not baseline.keys() <= files.keys():
        errors.append("missing_baseline_files")
    # Host reconstruction additionally validates aliases against the original plan.
    return files, entries, errors


def processes():
    return [
        int(p.name)
        for p in Path("/proc").iterdir()
        if p.name.isdecimal() and int(p.name) not in {1, os.getpid()}
    ]


def quiesce():
    supervisor_only()
    # Only the trusted child of namespace PID 1. Group kill misses detached writers.
    while processes():
        # A namespace containing only zombies still needs reaping.
        with contextlib.suppress(ProcessLookupError):
            os.kill(-1, signal.SIGKILL)
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break
            except ChildProcessError:
                break
        time.sleep(0.005)


def worker(entrypoint, stdout, stderr):
    import resource

    os.dup2(os.open("/dev/null", os.O_RDONLY), 0)
    os.dup2(stdout, 1)
    os.dup2(stderr, 2)
    os.closerange(3, 64)
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMITS["file_bytes"],) * 2)
    resource.setrlimit(resource.RLIMIT_AS, (192 * 1024 * 1024,) * 2)
    os.setgroups([])
    os.setgid(UID)
    os.setuid(UID)
    verify_identity(worker=True)
    os.umask(0o022)
    os.chdir("/work")
    os.execve(
        "/usr/local/bin/python",
        ["python", "-I", "-S", "-u", "-B", "/work/" + entrypoint],
        {
            "HOME": "/tmp",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        },
    )


def retain_log(output, chunk):
    available = LIMITS["log_bytes"] - len(output)
    output.extend(chunk[: max(0, available)])
    return len(chunk) > available


def completed_reason(deadline, exitcode, descendants, *, clock=None):
    clock = clock or time.monotonic
    if deadline is not None and clock() > deadline:
        return "watchdog_timeout"
    if descendants:
        return "descendants_after_exit"
    return "completed" if exitcode == 0 else "worker_exit"


def execute(payload, deadline, settling=lambda: None):
    supervisor_only()
    if deadline is not None and time.monotonic() >= deadline:
        raise ValueError("Budget exhausted before worker fork")
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            worker(payload["entrypoint"], out_w, err_w)
        except BaseException:
            os._exit(125)
    os.close(out_w)
    os.close(err_w)
    streams = {out_r: bytearray(), err_r: bytearray()}
    selector = selectors.DefaultSelector()
    for fd in (*streams, 0):
        os.set_blocking(fd, False)
        selector.register(fd, selectors.EVENT_READ)
    reason, exitcode = "completed", None
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                reason = "watchdog_timeout"
                break
            for key, _ in selector.select(
                0.02 if deadline is None else
                min(0.02, max(0, deadline - time.monotonic()))
            ):
                chunk = os.read(key.fd, 4096)
                if key.fd == 0:
                    reason = "duplicate_release" if chunk else "controller_disconnected"
                    break
                if not chunk:
                    selector.unregister(key.fd)
                else:
                    output = streams[key.fd]
                    if retain_log(output, chunk):
                        reason = "output_limit"
                        break
            if reason != "completed":
                break
            pid, status = os.waitpid(child, os.WNOHANG)
            if pid:
                exitcode = os.waitstatus_to_exitcode(status)
                reason = completed_reason(deadline, exitcode, bool(processes()))
                break
    finally:
        settling()
        quiesce()
        # After namespace quiescence, remaining pipe bytes cannot have new writers.
        for fd, output in streams.items():
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                if retain_log(output, chunk):
                    reason = "output_limit"
            os.close(fd)
        selector.close()
    return reason, exitcode, bytes(streams[out_r]), bytes(streams[err_r])


def container_only():
    if os.name != "posix" or os.getpid() != 1 or not Path("/.dockerenv").exists():
        raise RuntimeError(
            "Docker namespace PID 1 required; host execution is forbidden"
        )


def supervisor_only():
    if (
        os.name != "posix"
        or os.getpid() == 1
        or os.getppid() != 1
        or os.geteuid() != 0
        or not Path("/.dockerenv").exists()
    ):
        raise RuntimeError(
            "Trusted container supervisor required; host execution is forbidden"
        )


def released_deadline(release, binding, timeout_ms):
    release = dict(release)
    if "remaining_ms" not in release:
        raise ValueError("Release must explicitly declare its timing mode")
    remaining = release.pop("remaining_ms")
    if (
        release != {"kind": "release", **binding}
        or (timeout_ms is None and remaining is not None)
        or (timeout_ms is not None and (
            type(remaining) is not int or not 1 <= remaining <= timeout_ms
        ))
    ):
        raise ValueError("Release identity or remaining budget mismatch")
    return time.monotonic() + remaining / 1000 if remaining is not None else None


def supervise(envelope, files, control):
    supervisor_only()
    payload = envelope["payload"]
    binding = {k: envelope[k] for k in ("spec_sha256", "supervisor_sha256")}
    binding["run_id"] = payload["run_id"]
    sys.stdout.buffer.write(canonical({"kind": "ready", **binding}))
    sys.stdout.buffer.flush()
    selector = selectors.DefaultSelector()
    selector.register(0, selectors.EVENT_READ)
    released = bytearray()
    while b"\n" not in released:
        for _, _ in selector.select(0.1):
            chunk = os.read(0, 2049)
            if not chunk:
                raise ValueError("Controller disappeared before release")
            released.extend(chunk)
            if len(released) > 2048:
                raise ValueError("Release too large")
    selector.close()
    release = decode(bytes(released))
    deadline = released_deadline(release, binding, payload["timeout_ms"])
    os.write(control, canonical({
        "deadline": deadline + 3 if deadline is not None else None,
    }))

    def settling():
        os.write(control, canonical({"settling": True}))

    if Path("/dev/shm").is_dir():
        os.chmod("/dev/shm", 0o555)
    provision(Path("/work"), files, payload["policy"])
    reason, exitcode, stdout, stderr = execute(payload, deadline, settling)
    os.close(control)
    captured, entries, errors = capture(Path("/work"), files, payload["policy"])
    report = {
        "kind": "result",
        **binding,
        "binding": payload["binding"],
        "reason": reason,
        "exitcode": exitcode,
        "quiescent": not processes(),
        "capture_complete": not errors,
        "errors": errors,
        "entries": entries,
        "stdout": base64.b64encode(stdout).decode(),
        "stderr": base64.b64encode(stderr).decode(),
        "files": [
            {"path": p, "base64": base64.b64encode(data).decode()}
            for p, data in sorted(captured.items())
        ],
        "snapshot_sha256": snapshot_digest(captured),
    }
    wire = canonical(report)
    if len(wire) > MAX_WIRE:
        raise ValueError("Report limit exhausted")
    sys.stdout.buffer.write(wire)
    sys.stdout.buffer.flush()


def attachment_closed():
    poller = select.poll()
    poller.register(0, select.POLLHUP | select.POLLERR)
    return bool(poller.poll(0))


def watchdog(child, control, timeout_ms):
    """PID 1 does no source traversal, output draining, capture or report writing."""
    container_only()
    deadline = time.monotonic() + 10 if timeout_ms is not None else None
    maximum = deadline + timeout_ms / 1000 + 3 if deadline is not None else None
    selector = selectors.DefaultSelector()
    selector.register(control, selectors.EVENT_READ)
    packet = bytearray()
    released = settling = False
    control_closed = False
    invalid_control = False
    finished = None
    try:
        while deadline is None or time.monotonic() < deadline:
            # Observe disconnection without consuming the supervisor's release.
            if attachment_closed():
                return 125
            wait = (0.02 if deadline is None else
                    min(0.02, max(0, deadline - time.monotonic())))
            for _, _ in selector.select(wait):
                if deadline is not None and time.monotonic() >= deadline:
                    return 124
                chunk = os.read(control, 257)
                if not chunk:
                    invalid_control = not released or not settling or bool(packet)
                    selector.unregister(control)
                    control_closed = True
                    continue
                packet.extend(chunk)
                if len(packet) > 256:
                    return 125
                while b"\n" in packet:
                    end = packet.index(b"\n") + 1
                    value = decode(bytes(packet[:end]))
                    del packet[:end]
                    if released:
                        if (set(value) != {"settling"}
                                or value["settling"] is not True or settling):
                            return 125
                        settling = True
                        # This is armed only by trusted completion/failure handling,
                        # never by elapsed or quiet working time.
                        cleanup = time.monotonic() + 3
                        deadline = (min(deadline, cleanup)
                                    if deadline is not None else cleanup)
                        continue
                    proposed = value.get("deadline")
                    if (
                        set(value) != {"deadline"}
                        or (timeout_ms is None and proposed is not None)
                        or (timeout_ms is not None and (
                            type(proposed) not in (int, float)
                            or not math.isfinite(proposed)
                            or not time.monotonic() < proposed <= maximum
                            or proposed > time.monotonic() + timeout_ms / 1000 + 3
                        ))
                    ):
                        return 125
                    if finished is None:
                        deadline = proposed
                    elif proposed is not None:
                        deadline = min(deadline, proposed)
                    released = True
            # A stream of adopted orphans must not starve deadline enforcement.
            for _ in range(32):
                if finished is not None:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    return 124
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    return 125
                if not pid:
                    break
                if pid == child:
                    result = os.waitstatus_to_exitcode(status)
                    if result:
                        return result if result >= 0 else 128 - result
                    finished = result
                    # Drain final control bytes through EOF before accepting exit.
                    # Exit is an observed terminal event, not an age-based cutoff.
                    cleanup = time.monotonic() + 3
                    deadline = (min(deadline, cleanup)
                                if deadline is not None else cleanup)
                    break
            if invalid_control:
                return 125
            if finished is not None and control_closed:
                if not released or not settling or packet:
                    return 125
                # Exit zero cannot conceal namespace peers killed in finally.
                try:
                    os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    return finished
                return 125
        return 124
    finally:
        selector.close()
        os.close(control)
        with contextlib.suppress(ProcessLookupError):
            os.kill(-1, signal.SIGKILL)


def main():
    container_only()
    verify_identity(worker=False)
    raw = Path("/input/spec.json").read_bytes()
    envelope, files = load_spec(raw)
    if digest(Path(__file__).read_bytes()) != envelope["supervisor_sha256"]:
        raise ValueError("Supervisor source identity mismatch")
    control_r, control_w = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(control_r)
        try:
            supervise(envelope, files, control_w)
        except BaseException:
            os._exit(125)
        os._exit(0)
    os.close(control_w)
    # PID 1 observes stdin hangup without reading release bytes. It stays
    # independent even if supervisor Python stalls; only the child writes reports.
    os.close(1)
    os.close(2)
    try:
        code = watchdog(child, control_r, envelope["payload"]["timeout_ms"])
    except BaseException:
        code = 125
    os._exit(code)


if __name__ == "__main__":
    main()
