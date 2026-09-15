"""Bound native source capture evidence, never a controller execution receipt.

The owning adapter must fence dispatch/respawn, attest the container and retain
all raw responses in host journals. Stop is requested only after a terminal
event. These parsers cannot establish ownership, upstream stop or deployment
authority. Blocking construction/verification belong off the event loop.
"""

import base64
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .containment import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_SOURCE_BYTES,
    MAX_WIRE_BYTES,
    _verify_entries,
)
from .contracts import File, Snapshot, require_digest, unique_paths
from .journal import IntegrityError, decode, encode, regular, sha256
from .native import NativeSettings
from .native_admission import NativeRun
from .native_history import NativeHistory

LIMITS = {"files": MAX_FILES, "file_bytes": MAX_FILE_BYTES,
          "source_bytes": MAX_SOURCE_BYTES, "path_chars": 240, "path_depth": 16}


@dataclass(frozen=True)
class NativeCaptureSpec:
    run: NativeRun
    settings: NativeSettings
    baseline: Snapshot
    _helpers: Snapshot = field(init=False, repr=False)
    _limits: tuple = field(init=False, repr=False)

    def __post_init__(self):
        if (type(self.run) is not NativeRun
                or type(self.settings) is not NativeSettings
                or type(self.baseline) is not Snapshot
                or not re.fullmatch(r"[0-9a-f]{32}", self.run.run_id)):
            raise ValueError("Freeze host native run, settings and baseline")
        if (self.baseline.sha256 != self.settings.policy.baseline_sha256
                or self.run.binding.baseline_sha256 != self.baseline.sha256):
            raise ValueError("Native capture baseline binding mismatch")
        authority = decode(self.settings.authority)
        if any(encode(authority.get(key)) != encode(value) for key, value in {
            "run": asdict(self.run), "policy": asdict(self.settings.policy),
            "baseline_sha256": self.baseline.sha256,
        }.items()):
            raise ValueError("Native capture authority binding mismatch")
        files = {f.path: f.content for f in self.baseline.files}
        policy = self.settings.policy
        if (not files or len(files) > MAX_FILES
                or any(len(data) > MAX_FILE_BYTES for data in files.values())
                or sum(map(len, files.values())) > MAX_SOURCE_BYTES
                or policy.delete or not set(policy.modify) <= files.keys()):
            raise ValueError("Unsupported native capture source/policy")
        paths = tuple(sorted(set(files) | set(policy.create_under)))
        unique_paths(paths)
        if any(len(p) > 240 or len(p.split("/")) > 16 for p in paths):
            raise ValueError("Native capture path exceeds bounds")
        for root in policy.create_under:
            if any(p == root or p.startswith(root + "/") or root.startswith(p + "/")
                   for p in files) or any(
                       p != root and root.startswith(p + "/")
                       for p in policy.create_under):
                raise ValueError("Native creation root overlaps source")
        helpers = []
        for name, source in (("capture_worker.py", "native_capture_worker.py"),
                             ("capture_history.py", "native_history_reader.py")):
            path = Path(__file__).with_name(source)
            regular(path)
            helpers.append(File(name, path.read_bytes()))
        object.__setattr__(self, "_helpers", Snapshot(tuple(helpers)))
        object.__setattr__(self, "_limits", tuple(LIMITS.items()))
        if len(encode(self.envelope)) > MAX_WIRE_BYTES:
            raise ValueError("Native capture manifest exceeds wire bound")

    @property
    def policy(self):
        return self.settings.policy

    @property
    def payload(self):
        return {
            "version": 1, "kind": "native_capture_spec", "run": asdict(self.run),
            "settings_sha256": self.settings.identity,
            "baseline_sha256": self.baseline.sha256, "policy": asdict(self.policy),
            "authority_sha256": sha256(self.settings.authority),
            "config_sha256": sha256(encode(self.settings.config)),
            "helpers": {f.path: sha256(f.content) for f in self._helpers.files},
            "limits": dict(self._limits),
            "files": [{"path": f.path, "base64": base64.b64encode(f.content).decode()}
                      for f in self.baseline.files],
        }

    @property
    def sha256(self):
        return sha256(encode(self.payload))

    @property
    def envelope(self):
        return {"payload": self.payload, "spec_sha256": self.sha256}

    @property
    def inputs(self):
        return Snapshot((*self._helpers.files,
                         File("capture-spec.json", encode(self.envelope)),
                         File("task.json", self.settings.authority),
                         File("opencode.json", encode(self.settings.config))))

    def stop_request(self, native):
        _native(native)
        return encode({"kind": "stop", "spec_sha256": self.sha256,
                       "native": native})


def _native(value):
    if (type(value) is not dict or set(value) != {"pid", "start"}
            or type(value["pid"]) is not int or value["pid"] <= 1
            or type(value["start"]) is not int or value["start"] < 1):
        raise IntegrityError("Expected observed native PID/start identity")


def _record(raw):
    if type(raw) is not bytes or len(raw) > MAX_WIRE_BYTES:
        raise IntegrityError("Native capture response exceeds bound")
    value = decode(raw)
    if encode(value) != raw:
        raise IntegrityError("Noncanonical native capture response")
    return value


def verify_stop(raw, spec, native):
    """Validate a trusted collector response; not independent namespace attestation."""
    _native(native)
    value = _record(raw)
    if (set(value) != {"kind", "spec_sha256", "native", "before", "after"}
            or value["kind"] != "native_stopped"
            or encode(value["native"]) != encode(native)
            or value["spec_sha256"] != spec.sha256
            or type(value["before"]) is not list or not value["before"]
            or type(value["after"]) is not list
            or len(value["before"]) > 4096 or len(value["after"]) > 4096):
        raise IntegrityError("Native stop binding mismatch")
    _native(value["native"])
    before = _tasks(value["before"], quiet=False)
    _tasks(value["after"], quiet=True)
    if not any(t["pid"] == t["tid"] == native["pid"]
               and t["start"] == native["start"] and t["uids"] == [65532] * 4
               and t["state"] != "Z" for t in before):
        raise IntegrityError("Expected native process absent from stop inventory")
    roots = {t["pid"] for t in before if t["uids"] == [0] * 4}
    if 1 not in roots or len(roots) != 2:
        raise IntegrityError("Stop inventory has unexpected trusted root peers")
    return value


def _tasks(tasks, *, quiet):
    seen, leaders = set(), set()
    for task in tasks:
        if (type(task) is not dict or set(task) != {
            "pid", "tid", "start", "state", "uids", "gids", "groups", "nnp",
            "seccomp", "caps",
        } or any(type(task[k]) is not int or task[k] < 1
                 for k in ("pid", "tid", "start"))
                or type(task["state"]) is not str
                or task["state"] not in set("RSDTtZXxIKWP")
                or type(task["nnp"]) is not int or task["nnp"] != 1
                or type(task["seccomp"]) is not int or task["seccomp"] != 2):
            raise IntegrityError("Malformed native task inventory")
        for key in ("uids", "gids", "groups"):
            if (type(task[key]) is not list
                    or any(type(v) is not int for v in task[key])):
                raise IntegrityError("Malformed native task credentials")
        root = task["uids"] == [0] * 4
        uid, mask = (0, 0xE5) if root else (65532, 0)
        if (task["uids"] != [uid] * 4 or task["gids"] != [uid] * 4
                or task["groups"] not in ([[], [0]] if root else [[]])
                or type(task["caps"]) is not dict
                or any(type(v) is not int for v in task["caps"].values())
                or task["caps"] != {"CapEff": mask, "CapPrm": mask,
                                    "CapInh": 0, "CapAmb": 0, "CapBnd": 0xE5}
                or quiet and (root or task["state"] != "Z")):
            raise IntegrityError("Native writer credentials or termination mismatch")
        identity = (task["pid"], task["tid"])
        if identity in seen:
            raise IntegrityError("Duplicate native task identity")
        seen.add(identity)
        if task["pid"] == task["tid"]:
            leaders.add(task["pid"])
    if any(pid not in leaders for pid, _ in seen):
        raise IntegrityError("Native task leader missing")
    return tasks


def capture_request(spec, stop_raw, native, history):
    verify_stop(stop_raw, spec, native)
    metadata = _history(history)
    return encode({"kind": "capture", "spec_sha256": spec.sha256,
                   "stop_sha256": sha256(stop_raw),
                   "session_id": history.session_id,
                   "head": metadata["durable_sequence"],
                   "head_sha256": metadata["last_event_sha256"]})


def _history(history):
    if type(history) is not NativeHistory:
        raise IntegrityError("Use the owned native history, not claimed finality JSON")
    metadata = history.final_metadata
    records = history.journal.verify()
    if (not metadata["final"] or not metadata["complete"]
            or metadata["verification_scope"] != "full_prefix"
            or not records or records[-1].value["kind"] != "native_history_final"
            or records[-1].value["data"] != {
                "native_session_id": history.session_id, **metadata,
            }):
        raise IntegrityError("Native history is not durably finalized")
    return metadata


def verify_capture(raw, spec, stop_raw, native, history):
    """Return immutable source bytes only; admission/stop ownership stays external."""
    expected = decode(capture_request(spec, stop_raw, native, history))
    value = _record(raw)
    if (set(value) != {"kind", "request", "files", "entries", "identities",
                       "snapshot_sha256"}
            or value["kind"] != "native_capture"
            or encode(value["request"]) != encode(expected)):
        raise IntegrityError("Native capture binding/history mismatch")
    if type(value["files"]) is not list or len(value["files"]) > MAX_FILES:
        raise IntegrityError("Native captured file inventory invalid")
    files = []
    for item in value["files"]:
        if type(item) is not dict or set(item) != {"path", "base64"}:
            raise IntegrityError("Native captured file fields invalid")
        data = base64.b64decode(item["base64"], validate=True)
        if len(data) > MAX_FILE_BYTES:
            raise IntegrityError("Native captured file too large")
        files.append(File(item["path"], data))
    snapshot = Snapshot(tuple(files))
    require_digest(value["snapshot_sha256"])
    if (snapshot.sha256 != value["snapshot_sha256"]
            or sum(len(f.content) for f in files) > MAX_SOURCE_BYTES):
        raise IntegrityError("Native capture digest/size mismatch")
    old = {f.path: f.content for f in spec.baseline.files}
    new = {f.path: f.content for f in snapshot.files}
    unique_paths(tuple(sorted(old.keys() | new.keys())))
    if not old.keys() <= new.keys() or any(
        (path in old and data != old[path] and path not in spec.policy.modify)
        or (path not in old and not spec.policy.permits(path, "create"))
        for path, data in new.items()
    ):
        raise IntegrityError("Native captured source violates frozen policy")
    _verify_entries(value["entries"], spec, new)
    identities = value["identities"]
    if (type(identities) is not list
            or any(type(i) is not dict for i in identities)
            or len(identities) != len(value["entries"]) + 1
            or {i.get("path") for i in identities} != {
                ".", *(e["path"] for e in value["entries"]),
            }):
        raise IntegrityError("Incomplete native inode continuity inventory")
    for item in identities:
        if (set(item) != {"path", "device", "inode"}
                or any(type(item[k]) is not int or item[k] < 0
                       for k in ("device", "inode"))):
            raise IntegrityError("Invalid native inode identity")
    if len({(i["device"], i["inode"]) for i in identities}) != len(identities):
        raise IntegrityError("Aliased native inode identities")
    return snapshot
