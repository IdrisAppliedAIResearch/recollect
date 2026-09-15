"""Portable fault tests; no Docker, services, models, or host process signals."""

import base64
import copy
import os
import posixpath
import signal
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from recollect.selfmod import native_capture_worker as worker


def policy(**changes):
    return {"modify": ["edit.py"], "delete": [], "create_under": ["new"], **changes}


BASELINE = {"edit.py": b"old", "protected.py": b"fixed"}


class Tree:
    """Descriptor-based fake so Windows exercises the actual traversal logic."""

    def __init__(self):
        self.nodes, self.handles, self.offsets, self.opens = {}, {}, {}, []
        self.add("/work", directory=True)
        self.add("/work/edit.py", b"changed", mode=0o664, gid=worker.UID)
        self.add("/work/protected.py", b"fixed", mode=0o444)
        self.add("/work/new", directory=True, mode=0o775, gid=worker.UID)
        self.add(
            "/work/new/sub", directory=True, mode=0o755, uid=worker.UID, gid=worker.UID
        )
        self.add(
            "/work/new/sub/file.py",
            b"created",
            mode=0o644,
            uid=worker.UID,
            gid=worker.UID,
        )

    def add(self, path, data=b"", *, directory=False, mode=0o555, uid=0, gid=0):
        self.nodes[path] = SimpleNamespace(
            st_dev=1,
            st_ino=len(self.nodes) + 1,
            st_mode=(stat.S_IFDIR if directory else stat.S_IFREG) | mode,
            st_uid=uid,
            st_gid=gid,
            st_nlink=2 if directory else 1,
            st_size=0 if directory else len(data),
            st_mtime_ns=1,
            st_ctime_ns=1,
            data=data,
        )

    def path(self, path, dir_fd=None):
        return str(path) if dir_fd is None else self.handles[dir_fd] + "/" + path

    def lstat(self, path):
        return copy.copy(self.nodes[str(path)])

    def info(self, path, *, dir_fd=None, follow_symlinks=False):
        assert follow_symlinks is False
        return self.lstat(self.path(path, dir_fd))

    def open(self, path, flags, *, dir_fd=None):
        target = self.path(path, dir_fd)
        self.opens.append((target, flags, dir_fd))
        fd = len(self.opens) + 100
        self.handles[fd], self.offsets[fd] = target, 0
        return fd

    def fstat(self, fd):
        return self.lstat(self.handles[fd])

    def listdir(self, fd):
        parent = self.handles[fd]
        return [
            posixpath.basename(p) for p in self.nodes if posixpath.dirname(p) == parent
        ]

    def read(self, fd, size):
        data = self.nodes[self.handles[fd]].data
        offset = self.offsets[fd]
        self.offsets[fd] += size
        return data[offset : offset + size]

    def close(self, fd):
        del self.handles[fd]

    def install(self, monkeypatch):
        proxy = SimpleNamespace(**vars(os))
        for flag, value in (
            ("O_NOFOLLOW", 0x100000),
            ("O_NONBLOCK", 0x200000),
            ("O_CLOEXEC", 0x400000),
            ("O_DIRECTORY", 0x800000),
        ):
            setattr(proxy, flag, getattr(os, flag, value))
        for name in ("open", "lstat", "fstat", "listdir", "read", "close"):
            setattr(proxy, name, getattr(self, name))
        proxy.stat = self.info
        monkeypatch.setattr(worker, "os", proxy)


@pytest.fixture
def tree(monkeypatch):
    tree = Tree()
    tree.install(monkeypatch)
    return tree


def test_capture_descriptor_binding_and_exact_metadata(tree):
    files, entries, identities = worker.secure_capture("/work", BASELINE, policy())
    assert files == {
        "edit.py": b"changed",
        "protected.py": b"fixed",
        "new/sub/file.py": b"created",
    }
    assert len(entries) == 5 and len(identities) == 6
    assert identities[0] == {"path": ".", "device": 1, "inode": 1}
    assert all(
        set(e) == {"path", "kind", "mode", "uid", "gid", "links", "bytes"}
        for e in entries
    )
    assert all(set(i) == {"path", "device", "inode"} for i in identities)
    required = worker.os.O_NOFOLLOW | worker.os.O_NONBLOCK | worker.os.O_CLOEXEC
    assert all(flags & required == required for _, flags, _ in tree.opens)
    assert all(parent is not None for _, _, parent in tree.opens[1:])
    assert not tree.handles


@pytest.mark.parametrize(
    "path",
    [
        "",
        "../x",
        "/x",
        "x//y",
        "x/./y",
        "a\\b",
        "a:",
        "NUL.py",
        "com1",
        "x.",
        "bad name",
        "a" * 241,
        "/".join(["a"] * 17),
        "é",
        1,
    ],
)
def test_bad_paths(path):
    with pytest.raises(worker.CaptureError):
        worker.safe_path(path)


@pytest.mark.parametrize(
    "files,changes",
    [
        ({"A/x": b"", "a/y": b""}, {"modify": []}),
        ({"a": b"", "a/b": b""}, {"modify": []}),
        (BASELINE, {"modify": ["missing"]}),
        (BASELINE, {"delete": ["edit.py"]}),
        (BASELINE, {"create_under": ["new", "new/sub"]}),
        (BASELINE, {"create_under": ["edit.py/sub"]}),
        (BASELINE, {"create_under": ["new", "new"]}),
        (BASELINE, {"modify": "edit.py"}),
        ({"edit.py": "text"}, {}),
        ({"edit.py": b"x" * (256 * 1024 + 1)}, {}),
        ({str(i): b"" for i in range(129)}, {"modify": []}),
        ({str(i): b"x" * (256 * 1024) for i in range(9)}, {"modify": []}),
    ],
)
def test_invalid_source_policy(files, changes):
    with pytest.raises(worker.CaptureError):
        worker.validate_source(files, policy(**changes))


@pytest.mark.parametrize(
    "path,field,value",
    [
        ("edit.py", "st_mode", stat.S_IFREG | 0o666),
        ("edit.py", "st_uid", worker.UID),
        ("protected.py", "st_gid", worker.UID),
        ("new", "st_mode", stat.S_IFDIR | 0o777),
        ("new/sub", "st_uid", 0),
        ("new/sub/file.py", "st_mode", stat.S_IFREG | 0o664),
        ("edit.py", "st_nlink", 2),
        ("edit.py", "st_mode", stat.S_IFLNK | 0o664),
        ("edit.py", "st_mode", stat.S_IFIFO | 0o664),
        ("edit.py", "st_dev", 2),
        ("edit.py", "st_ino", 1),
        ("edit.py", "st_size", 256 * 1024 + 1),
        ("edit.py", "st_size", 1),
        ("protected.py", "data", b"drift"),
    ],
)
def test_reject_source_metadata(tree, path, field, value):
    setattr(tree.nodes["/work/" + path], field, value)
    with pytest.raises(worker.CaptureError):
        worker.secure_capture("/work", BASELINE, policy())
    assert not tree.handles


@pytest.mark.parametrize(
    "field,value",
    [
        ("st_mode", stat.S_IFDIR | 0o755),
        ("st_uid", worker.UID),
        ("st_gid", worker.UID),
        ("st_mode", stat.S_IFLNK | 0o555),
    ],
)
def test_root_metadata(tree, field, value):
    setattr(tree.nodes["/work"], field, value)
    with pytest.raises(worker.CaptureError):
        worker.secure_capture("/work", BASELINE, policy())


@pytest.mark.parametrize("path", ["edit.py", "new/sub/file.py"])
def test_missing_baseline_or_empty_create_root(tree, path):
    if path == "edit.py":
        del tree.nodes["/work/edit.py"]
    else:
        for name in list(tree.nodes):
            if name.startswith("/work/new"):
                del tree.nodes[name]
    with pytest.raises(worker.CaptureError, match="Deleted"):
        worker.secure_capture("/work", BASELINE, policy())


@pytest.mark.parametrize("path", ["extra", "new/sub/FILE.py", "new/NUL", "New"])
def test_extra_alias_or_bad_path(tree, path):
    tree.add(
        "/work/" + path, directory=True, mode=0o755, uid=worker.UID, gid=worker.UID
    )
    with pytest.raises(worker.CaptureError):
        worker.secure_capture("/work", BASELINE, policy())


@pytest.mark.parametrize("moment", ["open", "read", "directory_end", "inventory"])
def test_racing_source_identity(tree, monkeypatch, moment):
    if moment == "open":
        original = tree.open

        def changed(*args, **kwargs):
            fd = original(*args, **kwargs)
            tree.nodes[tree.handles[fd]].st_ino += 100
            return fd

        monkeypatch.setattr(worker.os, "open", changed)
    elif moment == "read":
        original = tree.read

        def changed(fd, size):
            result = original(fd, size)
            tree.nodes[tree.handles[fd]].st_ctime_ns += 1
            return result

        monkeypatch.setattr(worker.os, "read", changed)
    else:
        original = tree.listdir
        calls = 0

        def changed(fd):
            nonlocal calls
            result = original(fd)
            if tree.handles[fd] == "/work":
                calls += 1
                if calls == 2:
                    if moment == "inventory":
                        return [*result, "new-entry"]
                    tree.nodes["/work/edit.py"].st_ino += 100
            return result

        monkeypatch.setattr(worker.os, "listdir", changed)
    with pytest.raises(worker.CaptureError, match="changed"):
        worker.secure_capture("/work", BASELINE, policy())
    assert not tree.handles


@pytest.mark.parametrize("limit,value", [("files", 1), ("source_bytes", 5)])
def test_capture_limits(tree, monkeypatch, limit, value):
    baseline = {"edit.py": b"x"}
    del tree.nodes["/work/protected.py"]
    monkeypatch.setitem(worker.LIMITS, limit, value)
    with pytest.raises(worker.CaptureError):
        worker.secure_capture("/work", baseline, policy())


def test_metadata_limit(tree, monkeypatch):
    monkeypatch.setattr(worker, "MAX_ENTRIES", 4)
    with pytest.raises(worker.CaptureError, match="metadata"):
        worker.secure_capture("/work", BASELINE, policy())


def test_read_error_fails_without_partial_result(tree, monkeypatch):
    def fail(*args):
        raise OSError("read failure")

    monkeypatch.setattr(worker.os, "read", fail)
    with pytest.raises(OSError, match="read failure"):
        worker.secure_capture("/work", BASELINE, policy())
    assert not tree.handles


@pytest.mark.skipif(os.name != "posix", reason="Real descriptor-relative POSIX opens")
def test_real_posix_capture(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    (root / "f").write_bytes(b"ok")
    (root / "f").chmod(0o444)
    root.chmod(0o555)
    original_stat, original_fstat, original_lstat = os.stat, os.fstat, os.lstat

    def owned(info):
        # Ownership only is mocked so an ordinary POSIX test user can run this.
        values = {
            "st_" + k: getattr(info, "st_" + k)
            for k in (
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
        }
        return SimpleNamespace(**{**values, "st_uid": 0, "st_gid": 0})

    proxy = SimpleNamespace(**vars(os))
    proxy.stat = lambda *a, **kw: owned(original_stat(*a, **kw))
    proxy.fstat = lambda *a: owned(original_fstat(*a))
    proxy.lstat = lambda *a: owned(original_lstat(*a))
    monkeypatch.setattr(worker, "os", proxy)
    try:
        files, _, _ = worker.secure_capture(
            root, {"f": b"ok"}, policy(modify=[], create_under=[])
        )
        assert files == {"f": b"ok"}
    finally:
        root.chmod(0o755)
        (root / "f").chmod(0o644)


def task(pid, *, tid=None, start=10, state="S", root=False):
    uid, mask = (0, worker.ROOT_CAPABILITIES) if root else (worker.UID, 0)
    return {
        "pid": pid,
        "tid": pid if tid is None else tid,
        "start": start,
        "state": state,
        "uids": [uid] * 4,
        "gids": [uid] * 4,
        "groups": [],
        "nnp": 1,
        "seccomp": 2,
        "caps": {
            "CapInh": 0,
            "CapPrm": mask,
            "CapEff": mask,
            "CapBnd": worker.ROOT_CAPABILITIES,
            "CapAmb": 0,
        },
    }


@pytest.fixture
def census(monkeypatch):
    monkeypatch.setattr(worker.os, "getpid", lambda: 20)
    return [task(1, root=True), task(20, root=True), task(30), task(30, tid=31)]


@pytest.mark.parametrize("state", ["R", "S", "D", "T", "t", "I", "X"])
def test_quiet_rejects_every_live_or_stopped_task(census, state):
    census[2]["state"] = state
    census[3]["state"] = "Z"
    with pytest.raises(worker.CaptureError, match="Runnable"):
        worker.verify_processes(census, (30, 10), {1: 10, 20: 10}, require_quiet=True)


def test_quiet_accepts_zombies(census):
    for entry in census[2:]:
        entry["state"] = "Z"
    worker.verify_processes(census, (30, 10), {1: 10, 20: 10}, require_quiet=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("uids", [0] * 4),
        ("uids", [worker.UID, worker.UID, 0, worker.UID]),
        ("gids", [1] * 4),
        ("groups", [worker.UID]),
        ("nnp", 0),
        ("seccomp", 0),
        *[(key, 1) for key in ("CapEff", "CapPrm", "CapInh", "CapAmb", "CapBnd")],
    ],
)
def test_all_thread_credentials_checked(census, field, value):
    target = census[-1]["caps"] if field.startswith("Cap") else census[-1]
    target[field] = value
    with pytest.raises(worker.CaptureError, match="credential"):
        worker.verify_processes(census, (30, 10), {1: 10, 20: 10})


@pytest.mark.parametrize(
    "change", ["newroot", "owner", "native", "rootcaps", "missing"]
)
def test_process_pins_and_root_peers(census, change):
    if change == "newroot":
        census.append(task(50, root=True))
    elif change == "owner":
        census[0]["start"] = 11
    elif change == "native":
        census[2]["start"] = 11
    elif change == "rootcaps":
        census[0]["caps"]["CapEff"] = 0
    else:
        census = census[:2]
    with pytest.raises(worker.CaptureError):
        worker.verify_processes(census, (30, 10), {1: 10, 20: 10}, require_native=True)


def test_terminal_stop_kills_detached_and_new_workers(census, monkeypatch):
    census.extend([task(50), task(50, tid=51, state="T")])
    live = copy.deepcopy(census)
    killed, closed = [], []

    def read():
        return copy.deepcopy(live)

    def kill(fd, sig):
        assert sig == signal.SIGKILL
        killed.append(fd)
        for entry in live:
            if entry["pid"] == fd:
                entry["state"] = "Z"
        if fd == 30 and not any(t["pid"] == 60 for t in live):
            live.append(task(60))

    monkeypatch.setattr(worker, "process_census", read)
    monkeypatch.setattr(worker.os, "pidfd_open", lambda pid: pid, raising=False)
    monkeypatch.setattr(worker.os, "close", closed.append)
    monkeypatch.setattr(worker.signal, "pidfd_send_signal", kill, raising=False)
    monkeypatch.setattr(worker.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    before, after = worker.stop_workers((30, 10), {1: 10, 20: 10})
    assert before == census
    assert {30, 50, 60} <= set(killed) and set(killed).isdisjoint({1, 20})
    assert closed == killed
    assert all(t["pid"] in {1, 20} or t["state"] == "Z" for t in after)


def test_wrong_native_never_signalled(census, monkeypatch):
    monkeypatch.setattr(worker, "process_census", lambda: census)
    monkeypatch.setattr(
        worker.os, "pidfd_open", lambda _: pytest.fail("signal setup"), raising=False
    )
    with pytest.raises(worker.CaptureError, match="native"):
        worker.stop_workers((30, 11), {1: 10, 20: 10})


def test_capture_binds_process_inventory(census, monkeypatch):
    quiet = census[:2]
    after = [*quiet, task(50)]
    calls = iter([quiet, after])
    monkeypatch.setattr(worker, "process_census", lambda: next(calls))
    monkeypatch.setattr(worker, "secure_capture", lambda *args: ({}, [], []))
    with pytest.raises(worker.CaptureError, match="Runnable"):
        worker.capture_quiescent("/work", BASELINE, policy(), (30, 10), {1: 10, 20: 10})


def test_capture_allows_owned_root_threads_with_verified_credentials(
    census, monkeypatch
):
    quiet = census[:2]
    calls = iter([quiet, [*quiet, task(1, tid=2, root=True)]])
    monkeypatch.setattr(worker, "process_census", lambda: next(calls))
    monkeypatch.setattr(worker, "secure_capture", lambda *args: ({}, [], []))
    result, _, _ = worker.capture_quiescent(
        "/work", BASELINE, policy(), (30, 10), {1: 10, 20: 10}
    )
    assert result == ({}, [], [])


def proc_text(pid, tid, start=42):
    fields = ["S", *(["0"] * 18), str(start)]
    process_stat = str(tid) + " (name ) with spaces) " + " ".join(fields)
    status = f"Tgid:\t{pid}\nPid:\t{tid}\nUid:\t65532 65532 65532 65532\n"
    status += "Gid:\t65532 65532 65532 65532\nGroups:\nNoNewPrivs:\t1\nSeccomp:\t2\n"
    status += "".join(
        k + ":\t00000000\n" for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    )
    return process_stat, status


def test_census_reads_every_task(tmp_path):
    for pid, tid in [(30, 30), (30, 31), (50, 50)]:
        directory = tmp_path / str(pid) / "task" / str(tid)
        directory.mkdir(parents=True)
        process_stat, status = proc_text(pid, tid)
        (directory / "stat").write_text(process_stat)
        (directory / "status").write_text(status)
    tasks = worker.process_census(tmp_path)
    assert [(t["pid"], t["tid"], t["start"]) for t in tasks] == [
        (30, 30, 42),
        (30, 31, 42),
        (50, 50, 42),
    ]


def test_task_start_drift(monkeypatch):
    process_stat, status = proc_text(30, 31)
    reads = iter([process_stat, status, proc_text(30, 31, start=43)[0]])
    monkeypatch.setattr(Path, "read_text", lambda *args: next(reads))
    with pytest.raises(worker.CaptureError, match="identity changed"):
        worker._task(Path("/proc"), 30, 31)


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
@pytest.mark.parametrize("read_index", [0, 1, 2])
def test_disappearing_proc_task_restarts_complete_census(
    tmp_path, monkeypatch, error, read_index
):
    for tid in (30, 31):
        directory = tmp_path / "30" / "task" / str(tid)
        directory.mkdir(parents=True)
        process_stat, status = proc_text(30, tid)
        (directory / "stat").write_text(process_stat)
        (directory / "status").write_text(status)
    original = Path.read_text
    reads, leader_reads = 0, 0

    def read(path, *args, **kwargs):
        nonlocal reads, leader_reads
        if path.parent.name == "30":
            leader_reads += 1
        elif path.parent.name == "31":
            index, reads = reads, reads + 1
            if index == read_index:
                for child in path.parent.iterdir():
                    child.unlink()
                path.parent.rmdir()
                raise error("exited during read")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    tasks = worker.process_census(tmp_path)
    assert [(t["pid"], t["tid"]) for t in tasks] == [(30, 30)]
    assert leader_reads == 6  # No partially read inventory survives the retry.


@pytest.mark.parametrize(
    "error", [FileNotFoundError, ProcessLookupError, PermissionError, OSError]
)
def test_proc_read_error_with_existing_task_fails_closed(tmp_path, monkeypatch, error):
    (tmp_path / "30" / "task" / "30").mkdir(parents=True)

    def read(*args, **kwargs):
        raise error("unreadable task")

    monkeypatch.setattr(Path, "read_text", read)
    expected = (worker.CaptureError if error in
                (FileNotFoundError, ProcessLookupError) else error)
    with pytest.raises(expected):
        worker.process_census(tmp_path)


@pytest.fixture
def manifest(monkeypatch):
    baseline_sha = worker.snapshot_digest(BASELINE)
    frozen_policy = policy(baseline_sha256=baseline_sha)
    run = {"run_id": "a" * 32, "binding": {"baseline_sha256": baseline_sha}}
    task_data = {"run": run, "policy": frozen_policy, "baseline_sha256": baseline_sha}
    assets = {
        "capture_worker.py": b"worker",
        "capture_history.py": b"history",
        "task.json": worker.canonical(task_data),
        "opencode.json": b"{}\n",
    }
    settings = {
        "version": "1.18.18",
        "config": {},
        "authority_sha256": worker.digest(assets["task.json"]),
        "policy_sha256": worker.digest(worker.canonical(frozen_policy)[:-1]),
    }
    payload = {
        "version": 1,
        "kind": "native_capture_spec",
        "run": run,
        "settings_sha256": worker.digest(worker.canonical(settings)),
        "baseline_sha256": baseline_sha,
        "policy": frozen_policy,
        "authority_sha256": worker.digest(assets["task.json"]),
        "config_sha256": worker.digest(assets["opencode.json"]),
        "helpers": {
            p: worker.digest(assets[p])
            for p in ("capture_worker.py", "capture_history.py")
        },
        "limits": dict(worker.LIMITS),
        "files": [
            {"path": p, "base64": base64.b64encode(b).decode()}
            for p, b in BASELINE.items()
        ],
    }
    envelope = {
        "payload": payload,
        "spec_sha256": worker.digest(worker.canonical(payload)),
    }
    monkeypatch.setattr(worker, "_trusted_bytes", lambda path, **kw: assets[path.name])
    return envelope, assets


def test_manifest_matches_host_shape(manifest):
    envelope, _ = manifest
    loaded, baseline = worker.load_spec(worker.canonical(envelope))
    assert loaded == envelope and baseline == BASELINE
    assert "log_bytes" not in worker.LIMITS
    assert worker.snapshot_digest({}) == worker.digest(b"[]")


@pytest.mark.parametrize(
    "asset", ["capture_worker.py", "capture_history.py", "task.json", "opencode.json"]
)
def test_manifest_rejects_authority_or_helper_drift(manifest, asset):
    envelope, assets = manifest
    assets[asset] += b"drift"
    with pytest.raises(worker.CaptureError, match="digest"):
        worker.load_spec(worker.canonical(envelope))


@pytest.mark.parametrize(
    "raw",
    [b'{"a":1,"a":2}\n', b'{ "a":1}\n', b"[]\n", b'{"a":NaN}\n', b"{}", b"{}\n{}\n"],
)
def test_wire_rejects_noncanonical_or_duplicate(raw):
    with pytest.raises(ValueError):
        worker.decode(raw)


@pytest.mark.parametrize(
    "native",
    [
        {"pid": 1, "start": 1},
        {"pid": True, "start": 1},
        {"pid": 30, "start": 0},
        {"pid": 30, "start": True},
        {"pid": 30, "start": 1, "other": 1},
    ],
)
def test_invalid_native_request(native):
    with pytest.raises(worker.CaptureError):
        worker.validate_request(
            {"kind": "stop", "spec_sha256": "a" * 64, "native": native}
        )


def test_history_load_is_pinned_and_exact(manifest, monkeypatch):
    envelope, assets = manifest
    request = {"session_id": "ses_test", "head": 2, "head_sha256": "a" * 64}
    calls = []

    def read(path, session, **kwargs):
        calls.append((path, session, kwargs))
        return {
            "head": 2,
            "last_event_sha256": "a" * 64,
            "session_id": session,
            "after": 2,
            "watermark": 2,
            "complete": True,
            "rows": [],
            "fragment": None,
        }

    def load(module):
        module.read_terminal_page = read

    def spec(name, path):
        assert path == "/authority/capture_history.py".replace("/", os.sep)
        return SimpleNamespace(loader=SimpleNamespace(exec_module=load))

    monkeypatch.setattr(worker.importlib.util, "spec_from_file_location", spec)
    monkeypatch.setattr(
        worker.importlib.util, "module_from_spec", lambda _: SimpleNamespace()
    )
    sha = envelope["payload"]["helpers"]["capture_history.py"]
    worker._history_boundary(request, sha)
    assert calls == [
        (
            "/state/data/opencode/opencode.db",
            "ses_test",
            {"after": 2, "through": 2, "last_event_sha256": "a" * 64,
             "scratch_dir": worker.STOP_PATH.parent},
        )
    ]
    assets["capture_history.py"] += b"x"
    with pytest.raises(worker.CaptureError, match="helper changed"):
        worker._history_boundary(request, sha)


def test_main_rejects_host_before_reading(monkeypatch, capsys):
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace())
    assert worker.main() == 1
    assert "Fixed isolated namespace collector required" in capsys.readouterr().err


def test_manifest_settings_digest_matches_actual_host_contract(manifest):
    from recollect.selfmod.contracts import ChangePolicy
    from recollect.selfmod.native import NativeSettings

    envelope, assets = manifest
    frozen = envelope["payload"]
    settings = NativeSettings(
        "test",
        32000,
        4096,
        ChangePolicy(
            frozen["baseline_sha256"], modify=("edit.py",), create_under=("new",)
        ),
        assets["task.json"],
    )
    assets["opencode.json"] = worker.canonical(settings.config)
    frozen["config_sha256"] = worker.digest(assets["opencode.json"])
    frozen["settings_sha256"] = settings.identity
    envelope["spec_sha256"] = worker.digest(worker.canonical(frozen))
    assert worker.load_spec(worker.canonical(envelope))[1] == BASELINE


def test_incomplete_proc_task_fails_closed(tmp_path):
    (tmp_path / "30" / "task" / "30").mkdir(parents=True)
    with pytest.raises(worker.CaptureError, match="Incomplete"):
        worker.process_census(tmp_path)


def test_stop_cli_persists_exclusive_exact_record(manifest, census, monkeypatch):
    import io

    envelope, assets = manifest
    assets["capture-spec.json"] = worker.canonical(envelope)
    after = [*census[:2], task(30, state="Z")]
    monkeypatch.setattr(worker, "process_census", lambda: census)
    monkeypatch.setattr(worker, "stop_workers", lambda *args: (census, after))
    output, operations = io.BytesIO(), []

    class Target:
        def __enter__(self):
            return output

        def __exit__(self, *args):
            return False

    proxy = SimpleNamespace(**vars(os))
    proxy.O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0x100000)
    proxy.open = lambda path, flags, mode: operations.append((path, flags, mode)) or 999
    proxy.fdopen = lambda *args: Target()
    proxy.fchmod = lambda fd, mode: operations.append((fd, mode))
    proxy.fsync = lambda fd: None
    monkeypatch.setattr(output, "fileno", lambda: 999)
    monkeypatch.setattr(worker, "os", proxy)
    request = {
        "kind": "stop",
        "spec_sha256": envelope["spec_sha256"],
        "native": {"pid": 30, "start": 10},
    }
    response = worker.handle_request(request)
    assert set(response) == {"kind", "spec_sha256", "native", "before", "after"}
    assert response["before"] == census
    assert response["after"] == [task(30, state="Z")]
    assert output.getvalue() == worker.canonical(response)
    assert operations[0][0] == worker.STOP_PATH
    assert operations[0][1] & os.O_EXCL and operations[0][2] == 0o600
    assert operations[1] == (999, 0o600)


@pytest.mark.parametrize("fault", [None, "stop_sha", "owner", "live", "history"])
def test_capture_cli_binds_stop_owner_history_and_fixed_paths(
    manifest, census, monkeypatch, fault
):
    envelope, assets = manifest
    assets["capture-spec.json"] = worker.canonical(envelope)
    stopped = {
        "kind": "native_stopped",
        "spec_sha256": envelope["spec_sha256"],
        "native": {"pid": 30, "start": 10},
        "before": census,
        "after": [],
    }
    assets["native-stop.json"] = worker.canonical(stopped)
    request = {
        "kind": "capture",
        "spec_sha256": envelope["spec_sha256"],
        "stop_sha256": worker.digest(assets["native-stop.json"]),
        "session_id": "ses_test",
        "head": 2,
        "head_sha256": "a" * 64,
    }
    current = copy.deepcopy(census[:2])
    if fault == "stop_sha":
        request["stop_sha256"] = "b" * 64
    elif fault == "owner":
        current[0]["start"] += 1
    elif fault == "live":
        current.append(task(50, state="T"))
    monkeypatch.setattr(worker, "process_census", lambda: current)
    operations = []

    def history(req, sha):
        assert req == request
        assert sha == envelope["payload"]["helpers"]["capture_history.py"]
        operations.append("history")
        if fault == "history":
            raise worker.CaptureError("History changed")

    def capture(root, baseline, frozen_policy):
        assert root == Path("/work") and baseline == BASELINE
        operations.append("source")
        return BASELINE, [], []

    monkeypatch.setattr(worker, "_history_boundary", history)
    monkeypatch.setattr(worker, "secure_capture", capture)
    if fault:
        with pytest.raises(worker.CaptureError):
            worker.handle_request(request)
        assert "source" not in operations
    else:
        response = worker.handle_request(request)
        assert operations == ["history", "source", "history"]
        assert set(response) == {
            "kind",
            "request",
            "files",
            "entries",
            "identities",
            "snapshot_sha256",
        }
        assert response["snapshot_sha256"] == worker.snapshot_digest(BASELINE)
        assert response["request"] == request


def test_pidfd_recycled_pid_is_not_signalled(census, monkeypatch):
    census.append(task(50))
    recycled = [*census[:2], task(50, start=99)]
    quiet = census[:2]
    reads = iter([census, quiet, recycled, quiet])
    monkeypatch.setattr(worker, "process_census", lambda: next(reads))
    opened, closed, signals = [], [], []
    monkeypatch.setattr(
        worker.os, "pidfd_open", lambda pid: opened.append(pid) or pid, raising=False
    )
    monkeypatch.setattr(worker.os, "close", closed.append)
    monkeypatch.setattr(
        worker.signal,
        "pidfd_send_signal",
        lambda *args: signals.append(args),
        raising=False,
    )
    worker.stop_workers((30, 10), {1: 10, 20: 10})
    assert opened == closed == [30, 50] and signals == []


def test_actual_host_spec_inputs_load_in_standalone_worker(monkeypatch):
    from dataclasses import asdict, replace

    from recollect.selfmod.native_admission import NativeRun
    from recollect.selfmod.native_capture import NativeCaptureSpec
    from tests.selfmod_containment_helpers import spec as fixture_spec
    from tests.test_selfmod_native import settings

    fixture = fixture_spec()
    run = NativeRun(
        "a" * 32,
        "controller",
        "cycle",
        "grant",
        "author",
        1,
        "implement",
        fixture.binding,
    )
    original = settings()
    authority = worker.canonical(
        {
            "run": asdict(run),
            "policy": asdict(fixture.policy),
            "baseline_sha256": fixture.baseline.sha256,
            "original_context": original.authority.decode("utf-8"),
        }
    )
    host = NativeCaptureSpec(
        run,
        replace(original, policy=fixture.policy, authority=authority),
        fixture.baseline,
    )
    assets = {f.path: f.content for f in host.inputs.files}

    def trusted(path, **kwargs):
        assert path.parent == Path("/authority")
        return assets[path.name]

    monkeypatch.setattr(worker, "_trusted_bytes", trusted)
    envelope, baseline = worker.load_spec(assets["capture-spec.json"])
    assert envelope["spec_sha256"] == host.sha256
    assert baseline == {f.path: f.content for f in fixture.baseline.files}
    assert worker.snapshot_digest(baseline) == host.baseline.sha256
