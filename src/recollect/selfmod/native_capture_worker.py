"""Trusted, stdlib-only native source capture; never an execution receipt.

The owner must pin namespace PID 1 and prevent it from respawning workers.
Process stopping is a terminal operation, never an agent-work time limit.
"""

import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import re
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
    "path_chars": 240,
    "path_depth": 16,
}
MAX_ENTRIES = 2048
MAX_WIRE = 4 * 1024 * 1024
AUTHORITY = Path("/authority")
STOP_PATH = Path("/evidence/native-stop.json")
ROOT_CAPABILITIES = 0xE5  # CHOWN, DAC_READ_SEARCH, KILL, SETGID, SETUID.


class CaptureError(ValueError):
    pass


def safe_path(path):
    if (
        type(path) is not str
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
        or len(path) > LIMITS["path_chars"]
        or len(path.split("/")) > LIMITS["path_depth"]
    ):
        raise CaptureError("Invalid source path")
    for part in path.split("/"):
        stem = part.split(".")[0].upper()
        if (
            part in {"", ".", ".."}
            or part.endswith(".")
            or stem in {"CON", "PRN", "AUX", "NUL"}
            or re.fullmatch(r"(?:COM|LPT)[0-9]", stem)
        ):
            raise CaptureError("Nonportable source path")


def validate_source(baseline, policy):
    if (
        type(baseline) is not dict
        or type(policy) is not dict
        or not {"modify", "delete", "create_under"} <= policy.keys()
        or policy["delete"] != []
    ):
        raise CaptureError("Invalid baseline or policy")
    if (
        len(baseline) > LIMITS["files"]
        or any(
            type(data) is not bytes or len(data) > LIMITS["file_bytes"]
            for data in baseline.values()
        )
        or sum(map(len, baseline.values())) > LIMITS["source_bytes"]
    ):
        raise CaptureError("Baseline size limit")
    for key in ("modify", "create_under"):
        values = policy[key]
        if type(values) is not list or len(values) > MAX_ENTRIES:
            raise CaptureError("Invalid policy paths")
        for path in values:
            safe_path(path)
        if len(set(values)) != len(values):
            raise CaptureError("Duplicate policy path")
    if not set(policy["modify"]) <= baseline.keys():
        raise CaptureError("Modification outside baseline")
    directories, aliases = set(), {}
    for path in [*baseline, *policy["create_under"]]:
        safe_path(path)
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            prior = aliases.setdefault(prefix.casefold(), prefix)
            if prior != prefix:
                raise CaptureError("Source path alias")
            if i < len(parts):
                directories.add(prefix)
    if directories & baseline.keys():
        raise CaptureError("Source file/directory collision")
    roots = policy["create_under"]
    for root in roots:
        if any(
            p == root or p.startswith(root + "/") or root.startswith(p + "/")
            for p in baseline
        ) or any(r != root and root.startswith(r + "/") for r in roots):
            raise CaptureError("Creation root overlap")
    directories.update(roots)
    if len(directories) + len(baseline) > MAX_ENTRIES:
        raise CaptureError("Source metadata limit")
    return directories


def _identity(info):
    # atime can change because of our own reads; ctime binds metadata changes.
    return tuple(
        getattr(info, "st_" + key)
        for key in (
            "dev",
            "ino",
            "mode",
            "uid",
            "gid",
            "nlink",
            "size",
            "mtime_ns",
            "ctime_ns",
        )
    )


def _same(before, after):
    if _identity(before) != _identity(after):
        raise CaptureError("Source identity or metadata changed during capture")


def secure_capture(root, baseline, policy):
    """Capture a quiescent tree using pinned directory descriptors (POSIX only).

    Callers must bind process inventories around this operation. No partial
    snapshot is returned on filesystem, identity, policy, or limit errors.
    """
    required_dirs = validate_source(baseline, policy)
    files, entries, identities = {}, [], []
    aliases, inodes, found_dirs = {}, set(), set()
    total = 0
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    root = os.fspath(root)
    root_info = os.lstat(root)

    def visit(parent, name, relative, before):
        nonlocal total
        is_dir = stat.S_ISDIR(before.st_mode)
        if not is_dir and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
            raise CaptureError("Linked or special source entry")
        inode = (before.st_dev, before.st_ino)
        if inode in inodes or before.st_dev != root_info.st_dev:
            raise CaptureError("Source inode alias or foreign device")
        inodes.add(inode)
        created = any(relative.startswith(p + "/") for p in policy["create_under"])
        if is_dir:
            if relative and relative not in required_dirs and not created:
                raise CaptureError("Out-of-scope directory")
            expected = (
                (0, UID, 0o775)
                if relative in policy["create_under"]
                else (UID, UID, 0o755)
                if created
                else (0, 0, 0o555)
            )
            found_dirs.add(relative)
        else:
            if relative not in baseline and not created:
                raise CaptureError("Out-of-scope creation")
            expected = (
                (0, UID, 0o664)
                if relative in policy["modify"]
                else (0, 0, 0o444)
                if relative in baseline
                else (UID, UID, 0o644)
            )
        if (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) != expected:
            raise CaptureError("Source ownership/mode mismatch")
        fd = os.open(name, flags | (os.O_DIRECTORY if is_dir else 0), dir_fd=parent)
        try:
            _same(before, os.fstat(fd))
            if is_dir:
                children = sorted(os.listdir(fd))
                if len(children) + len(entries) > MAX_ENTRIES:
                    raise CaptureError("Source metadata limit")
                inventory = {}
                for child in children:
                    path = relative + "/" + child if relative else child
                    safe_path(path)
                    if path.casefold() in aliases:
                        raise CaptureError("Source path alias")
                    aliases[path.casefold()] = path
                    if len(entries) >= MAX_ENTRIES:
                        raise CaptureError("Source metadata limit")
                    info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                    inventory[child] = info
                    entries.append(
                        {
                            "path": path,
                            "kind": "directory"
                            if stat.S_ISDIR(info.st_mode)
                            else "file",
                            "mode": stat.S_IMODE(info.st_mode),
                            "uid": info.st_uid,
                            "gid": info.st_gid,
                            "links": info.st_nlink,
                            "bytes": info.st_size,
                        }
                    )
                    visit(fd, child, path, info)
                if children != sorted(os.listdir(fd)):
                    raise CaptureError("Directory inventory changed during capture")
                for child, info in inventory.items():
                    _same(info, os.stat(child, dir_fd=fd, follow_symlinks=False))
            else:
                if (
                    not 0 <= before.st_size <= LIMITS["file_bytes"]
                    or len(files) >= LIMITS["files"]
                ):
                    raise CaptureError("Source file/count limit")
                chunks, size = [], 0
                while True:
                    data = os.read(fd, min(65536, LIMITS["file_bytes"] + 1 - size))
                    if not data:
                        break
                    chunks.append(data)
                    size += len(data)
                    if size > LIMITS["file_bytes"]:
                        raise CaptureError("Source file limit")
                data = b"".join(chunks)
                total += size
                if size != before.st_size or total > LIMITS["source_bytes"]:
                    raise CaptureError("Source size/inventory mismatch")
                if (
                    relative in baseline
                    and relative not in policy["modify"]
                    and data != baseline[relative]
                ):
                    raise CaptureError("Protected source drift")
                files[relative] = data
            _same(before, os.fstat(fd))
            _same(before, os.stat(name, dir_fd=parent, follow_symlinks=False))
            identities.append(
                {
                    "path": relative or ".",
                    "device": before.st_dev,
                    "inode": before.st_ino,
                }
            )
        finally:
            os.close(fd)

    if not stat.S_ISDIR(root_info.st_mode):
        raise CaptureError("Source root must be a directory")
    visit(None, root, "", root_info)
    if not baseline.keys() <= files.keys() or not required_dirs <= found_dirs:
        raise CaptureError("Deleted baseline source or creation directory")
    return (
        files,
        sorted(entries, key=lambda item: item["path"]),
        sorted(identities, key=lambda item: item["path"]),
    )


def _stat_identity(text, tid):
    head, separator, tail = text.rpartition(") ")
    fields = tail.split()
    if (
        not separator
        or not head.startswith(str(tid) + " (")
        or len(fields) < 20
        or len(fields[0]) != 1
        or fields[0] not in "RSDZTWtXxKPI"
        or not fields[19].isdecimal()
    ):
        raise CaptureError("Malformed process stat")
    return int(fields[19]), fields[0]


def _task(proc, pid, tid):
    directory = proc / str(pid) / "task" / str(tid)
    first = _stat_identity((directory / "stat").read_text(), tid)
    fields = {}
    for line in (directory / "status").read_text().splitlines():
        key, separator, value = line.partition(":")
        if separator:
            if key in fields:
                raise CaptureError("Duplicate process status field")
            fields[key] = value.strip()
    last = _stat_identity((directory / "stat").read_text(), tid)
    if first[0] != last[0]:
        raise CaptureError("Process identity changed during census")
    try:
        if int(fields["Tgid"]) != pid or int(fields["Pid"]) != tid:
            raise CaptureError("Process/thread identity mismatch")
        result = {
            "pid": pid,
            "tid": tid,
            "start": first[0],
            "state": last[1],
            "uids": [int(v) for v in fields["Uid"].split()],
            "gids": [int(v) for v in fields["Gid"].split()],
            "groups": [int(v) for v in fields["Groups"].split()],
            "nnp": int(fields["NoNewPrivs"]),
            "seccomp": int(fields["Seccomp"]),
            "caps": {
                key: int(fields[key], 16)
                for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
            },
        }
    except (KeyError, ValueError) as exc:
        raise CaptureError("Malformed process credentials") from exc
    return result


def process_census(proc=Path("/proc")):
    """Read every thread; disappearance is retried by the next complete census."""
    proc = Path(proc)
    while True:
        try:
            pids = sorted(int(p.name) for p in proc.iterdir() if p.name.isdecimal())
            tasks = []
            for pid in pids:
                directory = proc / str(pid) / "task"
                tids = sorted(
                    int(p.name) for p in directory.iterdir() if p.name.isdecimal()
                )
                if pid not in tids:
                    raise CaptureError("Missing process leader")
                for tid in tids:
                    try:
                        tasks.append(_task(proc, pid, tid))
                    except (FileNotFoundError, ProcessLookupError) as exc:
                        if (directory / str(tid)).exists():
                            raise CaptureError("Incomplete process task") from None
                        # procfs may return ESRCH for an already-open task file.
                        # Retry the whole inventory only after confirming absence.
                        raise FileNotFoundError("Process task disappeared") from exc
                if tids != sorted(
                    int(p.name) for p in directory.iterdir() if p.name.isdecimal()
                ):
                    break
            else:
                if pids == sorted(
                    int(p.name) for p in proc.iterdir() if p.name.isdecimal()
                ):
                    return tasks
        except FileNotFoundError:
            # Exiting tasks are normal during terminal stop. Other IO fails closed.
            if not proc.is_dir():
                raise
            continue


def verify_processes(
    tasks, expected_native, trusted_roots, *, require_native=False, require_quiet=False
):
    """Root pins map PID to starttime; only PID 1 and this collector are trusted."""
    if set(trusted_roots) != {1, os.getpid()} or os.getpid() == 1:
        raise CaptureError("Invalid trusted root peers")
    for pid, start in [*trusted_roots.items(), expected_native]:
        if type(pid) is not int or pid < 1 or type(start) is not int or start < 1:
            raise CaptureError("Invalid pinned process identity")
    if expected_native[0] in trusted_roots:
        raise CaptureError("Native process cannot be a trusted root")
    leaders, seen = {}, set()
    for task in tasks:
        pid, tid = task["pid"], task["tid"]
        if (pid, tid) in seen:
            raise CaptureError("Duplicate process thread")
        seen.add((pid, tid))
        root = pid in trusted_roots
        expected_uid = 0 if root else UID
        caps = task["caps"]
        mask = ROOT_CAPABILITIES if root else 0
        if (
            task["uids"] != [expected_uid] * 4
            or task["gids"] != [expected_uid] * 4
            or task["groups"] not in ([[], [0]] if root else [[]])
            or task["nnp"] != 1
            or task["seccomp"] != 2
            or set(caps) != {"CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"}
            or any(caps[k] != mask for k in ("CapPrm", "CapEff"))
            or caps["CapBnd"] != ROOT_CAPABILITIES
            or caps["CapInh"] != 0
            or caps["CapAmb"] != 0
        ):
            raise CaptureError("Process credential/capability/seccomp mismatch")
        if root and task["state"] in {"Z", "X", "x"}:
            raise CaptureError("Trusted root peer exited")
        if not root and require_quiet and task["state"] != "Z":
            raise CaptureError("Runnable worker remains (stopped is not quiescent)")
        if pid == tid:
            leaders[pid] = task["start"]
    if any(leaders.get(pid) != start for pid, start in trusted_roots.items()):
        raise CaptureError("Trusted root process identity mismatch")
    if any(pid not in leaders for pid, _ in seen):
        raise CaptureError("Missing process leader")
    pid, start = expected_native
    if (pid in leaders and leaders[pid] != start) or (
        require_native and pid not in leaders
    ):
        raise CaptureError("Expected native process identity mismatch")


def stop_workers(expected_native, trusted_roots):
    """Kill every unprivileged process until only trusted roots/zombies remain.

    PID 1 must be owned and must not respawn. There is deliberately no time or
    iteration budget. pidfds prevent a recycled PID from receiving our signal.
    """
    before = process_census()
    verify_processes(before, expected_native, trusted_roots, require_native=True)
    current = before
    while True:
        verify_processes(current, expected_native, trusted_roots)
        workers = {
            t["pid"]: t["start"]
            for t in current
            if t["pid"] not in trusted_roots and t["pid"] == t["tid"]
        }
        for pid, start in workers.items():
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            try:
                fresh = process_census()
                verify_processes(fresh, expected_native, trusted_roots)
                if not any(
                    t["pid"] == t["tid"] == pid and t["start"] == start for t in fresh
                ):
                    continue
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
        current = process_census()
        verify_processes(current, expected_native, trusted_roots)
        if all(t["pid"] in trusted_roots or t["state"] == "Z" for t in current):
            verify_processes(
                current, expected_native, trusted_roots, require_quiet=True
            )
            return before, current
        time.sleep(0.005)


def capture_quiescent(root, baseline, policy, expected_native, trusted_roots):
    before = process_census()
    verify_processes(before, expected_native, trusted_roots, require_quiet=True)
    captured = secure_capture(root, baseline, policy)
    after = process_census()
    verify_processes(after, expected_native, trusted_roots, require_quiet=True)
    return captured, before, after


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
    ).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def snapshot_digest(files):
    return digest(
        canonical(
            [
                {"path": path, "bytes": len(data), "sha256": digest(data)}
                for path, data in sorted(files.items())
            ]
        )[:-1]
    )


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise CaptureError("Duplicate JSON key")
        value[key] = item
    return value


def decode(raw):
    if type(raw) is not bytes or len(raw) > MAX_WIRE:
        raise CaptureError("Oversized wire record")
    value = json.loads(raw, object_pairs_hook=_pairs)
    if type(value) is not dict or canonical(value) != raw:
        raise CaptureError("Noncanonical wire record")
    return value


def _sha(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CaptureError("Invalid digest")


def _native(value):
    if (
        type(value) is not dict
        or set(value) != {"pid", "start"}
        or type(value["pid"]) is not int
        or value["pid"] <= 1
        or type(value["start"]) is not int
        or value["start"] < 1
    ):
        raise CaptureError("Invalid expected native PID/start identity")
    return value["pid"], value["start"]


def validate_request(request):
    if type(request) is not dict:
        raise CaptureError("Invalid capture request")
    kind = request.get("kind")
    keys = {"kind", "spec_sha256"}
    if kind == "stop":
        keys.add("native")
    elif kind == "capture":
        keys.update(("stop_sha256", "session_id", "head", "head_sha256"))
    else:
        raise CaptureError("Unknown capture operation")
    if set(request) != keys:
        raise CaptureError("Invalid capture request fields")
    _sha(request["spec_sha256"])
    if kind == "stop":
        _native(request["native"])
    else:
        _sha(request["stop_sha256"])
        _sha(request["head_sha256"])
        if (
            type(request["session_id"]) is not str
            or not re.fullmatch(r"ses_[A-Za-z0-9]+", request["session_id"])
            or type(request["head"]) is not int
            or not 0 <= request["head"] <= 2**63 - 1
        ):
            raise CaptureError("Invalid history boundary")


def _trusted_bytes(path, *, stop=False):
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (before.st_uid, before.st_gid) != (0, 0)
        or (stop and stat.S_IMODE(before.st_mode) != 0o600)
        or not 0 <= before.st_size <= MAX_WIRE
    ):
        raise CaptureError("Untrusted authority/evidence file")
    if not stop and not os.statvfs(path).f_flag & os.ST_RDONLY:
        raise CaptureError("Authority file is not on a read-only mount")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        _same(before, os.fstat(fd))
        with os.fdopen(os.dup(fd), "rb") as source:
            raw = source.read(MAX_WIRE + 1)
        _same(before, os.fstat(fd))
        _same(before, path.lstat())
        if len(raw) != before.st_size:
            raise CaptureError("Authority/evidence size drift")
        return raw
    finally:
        os.close(fd)


def load_spec(raw):
    envelope = decode(raw)
    if set(envelope) != {"payload", "spec_sha256"}:
        raise CaptureError("Invalid manifest envelope")
    payload = envelope["payload"]
    if (
        type(payload) is not dict
        or set(payload)
        != {
            "version",
            "kind",
            "run",
            "settings_sha256",
            "baseline_sha256",
            "policy",
            "authority_sha256",
            "config_sha256",
            "helpers",
            "limits",
            "files",
        }
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or payload["kind"] != "native_capture_spec"
        or payload["limits"] != LIMITS
        or digest(canonical(payload)) != envelope["spec_sha256"]
    ):
        raise CaptureError("Invalid manifest payload or digest")
    for key in (
        "settings_sha256",
        "baseline_sha256",
        "authority_sha256",
        "config_sha256",
    ):
        _sha(payload[key])
    if type(payload["helpers"]) is not dict or set(payload["helpers"]) != {
        "capture_worker.py",
        "capture_history.py",
    }:
        raise CaptureError("Invalid helper manifest")
    for name, expected in payload["helpers"].items():
        _sha(expected)
        if digest(_trusted_bytes(AUTHORITY / name)) != expected:
            raise CaptureError("Pinned helper digest mismatch")
    authority = _trusted_bytes(AUTHORITY / "task.json")
    config = _trusted_bytes(AUTHORITY / "opencode.json")
    if (
        digest(authority) != payload["authority_sha256"]
        or digest(config) != payload["config_sha256"]
    ):
        raise CaptureError("Task/config authority digest mismatch")
    task = decode(authority)
    if (
        task.get("run") != payload["run"]
        or task.get("policy") != payload["policy"]
        or task.get("baseline_sha256") != payload["baseline_sha256"]
        or payload["run"]["binding"]["baseline_sha256"] != payload["baseline_sha256"]
    ):
        raise CaptureError("Task/run/baseline binding mismatch")
    settings = {
        "version": "1.18.18",
        "config": decode(config),
        "authority_sha256": digest(authority),
        "policy_sha256": digest(canonical(payload["policy"])[:-1]),
    }
    if digest(canonical(settings)) != payload["settings_sha256"]:
        raise CaptureError("Native settings identity mismatch")
    if type(payload["files"]) is not list or len(payload["files"]) > LIMITS["files"]:
        raise CaptureError("Invalid baseline inventory")
    baseline = {}
    for item in payload["files"]:
        if type(item) is not dict or set(item) != {"path", "base64"}:
            raise CaptureError("Invalid baseline entry")
        safe_path(item["path"])
        if item["path"] in baseline or type(item["base64"]) is not str:
            raise CaptureError("Duplicate or malformed baseline entry")
        baseline[item["path"]] = base64.b64decode(item["base64"], validate=True)
    validate_source(baseline, payload["policy"])
    if (
        not baseline
        or snapshot_digest(baseline) != payload["baseline_sha256"]
        or payload["policy"].get("baseline_sha256") != payload["baseline_sha256"]
    ):
        raise CaptureError("Baseline digest mismatch")
    return envelope, baseline


def _root_pins(tasks, *, owner_start=None):
    pins = {
        t["pid"]: t["start"]
        for t in tasks
        if t["pid"] == t["tid"] and t["pid"] in {1, os.getpid()}
    }
    if owner_start is not None and pins.get(1) != owner_start:
        raise CaptureError("Owned PID 1 was replaced")
    return pins


def _history_boundary(request, helper_sha):
    path = AUTHORITY / "capture_history.py"
    if digest(_trusted_bytes(path)) != helper_sha:
        raise CaptureError("History helper changed")
    spec = importlib.util.spec_from_file_location("_capture_history", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    page = module.read_terminal_page(
        "/state/data/opencode/opencode.db",
        request["session_id"],
        after=request["head"],
        through=request["head"],
        last_event_sha256=request["head_sha256"],
        scratch_dir=STOP_PATH.parent,
    )
    if (
        page["head"] != request["head"]
        or page["last_event_sha256"] != request["head_sha256"]
        or page["session_id"] != request["session_id"]
        or page["after"] != request["head"]
        or page["watermark"] != request["head"]
        or page["complete"] is not True
        or page["rows"] != []
        or page["fragment"] is not None
    ):
        raise CaptureError("Native history advanced or changed after finalization")


def handle_request(request):
    validate_request(request)
    envelope, baseline = load_spec(_trusted_bytes(AUTHORITY / "capture-spec.json"))
    if request["spec_sha256"] != envelope["spec_sha256"]:
        raise CaptureError("Foreign capture spec")
    payload = envelope["payload"]
    if request["kind"] == "stop":
        native = _native(request["native"])
        pins = _root_pins(process_census())
        before, after = stop_workers(native, pins)
        response = {
            "kind": "native_stopped",
            "spec_sha256": request["spec_sha256"],
            "native": request["native"],
            "before": before,
            "after": [t for t in after if t["pid"] not in pins],
        }
        raw = canonical(response)
        if len(raw) > MAX_WIRE:
            raise CaptureError("Stop record exceeds wire bound")
        fd = os.open(
            STOP_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        return response
    stop_raw = _trusted_bytes(STOP_PATH, stop=True)
    if digest(stop_raw) != request["stop_sha256"]:
        raise CaptureError("Stop record digest mismatch")
    stopped = decode(stop_raw)
    if (
        set(stopped) != {"kind", "spec_sha256", "native", "before", "after"}
        or stopped["kind"] != "native_stopped"
        or stopped["spec_sha256"] != request["spec_sha256"]
        or not stopped["before"]
        or any(t["state"] != "Z" for t in stopped["after"])
    ):
        raise CaptureError("Invalid bound stop record")
    owners = [t for t in stopped["before"] if t["pid"] == t["tid"] == 1]
    if len(owners) != 1:
        raise CaptureError("Missing owned PID 1 stop identity")
    native = _native(stopped["native"])
    pins = _root_pins(process_census(), owner_start=owners[0]["start"])
    verify_processes(process_census(), native, pins, require_quiet=True)
    _history_boundary(request, payload["helpers"]["capture_history.py"])
    (files, entries, identities), _, _ = capture_quiescent(
        Path("/work"), baseline, payload["policy"], native, pins
    )
    _history_boundary(request, payload["helpers"]["capture_history.py"])
    verify_processes(process_census(), native, pins, require_quiet=True)
    return {
        "kind": "native_capture",
        "request": request,
        "files": [
            {"path": path, "base64": base64.b64encode(data).decode("ascii")}
            for path, data in sorted(files.items())
        ],
        "entries": entries,
        "identities": identities,
        "snapshot_sha256": snapshot_digest(files),
    }


def collector_only():
    if (
        sys.platform != "linux"
        or os.geteuid() != 0
        or os.getegid() != 0
        or os.getpid() == 1
        or not sys.flags.isolated
        or not sys.flags.no_site
        or Path(__file__) != AUTHORITY / "capture_worker.py"
    ):
        raise CaptureError("Fixed isolated namespace collector required")
    info = AUTHORITY.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or (info.st_uid, info.st_gid) != (0, 0)
        or not os.statvfs(AUTHORITY).f_flag & os.ST_RDONLY
    ):
        raise CaptureError("Read-only root-owned authority mount required")
    evidence = STOP_PATH.parent.lstat()
    if (
        not stat.S_ISDIR(evidence.st_mode)
        or (evidence.st_uid, evidence.st_gid) != (0, 0)
        or evidence.st_mode & 0o022
    ):
        raise CaptureError("Root-owned evidence directory required")
    # NSpid can show one PID when procfs is mounted inside the namespace. Mount
    # ownership/namespace attestation is the host's responsibility, not a receipt.
    init = _task(Path("/proc"), 1, 1)
    if init["uids"] != [0] * 4:
        raise CaptureError("Root namespace owner required")


def main():
    try:
        collector_only()
        request = decode(sys.stdin.buffer.read(MAX_WIRE + 1))
        response = canonical(handle_request(request))
        if len(response) > MAX_WIRE:
            raise CaptureError("Capture response exceeds wire bound")
        sys.stdout.buffer.write(response)
        sys.stdout.buffer.flush()
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc)[:400], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
