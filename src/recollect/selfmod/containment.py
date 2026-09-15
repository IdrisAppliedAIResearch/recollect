"""Frozen, networkless Docker fixture profile; never a host-process fallback.

Only trusted host code may construct specs or supply image configuration. This
module does not grant experiment eligibility or implement model/provider access.
"""

import base64
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .contracts import (
    ChangePolicy,
    File,
    Snapshot,
    require_digest,
    require_path,
    require_tuple,
    unique_paths,
)
from .development import Binding
from .journal import IntegrityError, decode, encode, regular, root_path, sha256

WORKER_UID = 65532
MAX_FILES = 128
MAX_FILE_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_LOG_BYTES = 64 * 1024
MAX_WIRE_BYTES = 4 * 1024 * 1024
CAPABILITIES = ("CHOWN", "KILL", "SETGID", "SETUID")
MASKED_PATHS = (
    "/proc/acpi",
    "/proc/asound",
    "/proc/interrupts",
    "/proc/kcore",
    "/proc/keys",
    "/proc/latency_stats",
    "/proc/sched_debug",
    "/proc/scsi",
    "/proc/timer_list",
    "/proc/timer_stats",
    "/sys/devices/virtual/powercap",
    "/sys/firmware",
)
READONLY_PATHS = (
    "/proc/bus",
    "/proc/fs",
    "/proc/irq",
    "/proc/sys",
    "/proc/sysrq-trigger",
)
TMPFS = {
    "/work": "rw,noexec,nosuid,nodev,size=32m,nr_inodes=1024,mode=0755",
    "/tmp": (
        "rw,noexec,nosuid,nodev,size=8m,nr_inodes=256,mode=0700,uid=65532,gid=65532"
    ),
}
ENVIRONMENT = {
    "HOME": "/tmp",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}


@dataclass(frozen=True)
class FixtureSpec:
    run_id: str
    image_id: str
    image_environment: tuple[str, ...]
    baseline: Snapshot
    policy: ChangePolicy
    entrypoint: str
    timeout_ms: int | None
    binding: Binding

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{32}", self.run_id):
            raise ValueError("Host-assigned unique run identity required")
        if not self.image_id.startswith("sha256:"):
            raise ValueError("Pin a local immutable image ID")
        require_digest(self.image_id[7:])
        require_tuple(self.image_environment)
        _environment(self.image_environment)
        require_path(self.entrypoint)
        if self.timeout_ms is not None and (
            type(self.timeout_ms) is not int or not 50 <= self.timeout_ms <= 60_000
        ):
            raise ValueError("Freeze a 50ms-60s offline fixture budget")
        files = {f.path: f.content for f in self.baseline.files}
        if self.entrypoint not in files or not self.entrypoint.endswith(".py"):
            raise ValueError("Fixture entrypoint must be a captured Python source")
        if (
            len(files) > MAX_FILES
            or any(len(content) > MAX_FILE_BYTES for content in files.values())
            or sum(map(len, files.values())) > MAX_SOURCE_BYTES
        ):
            raise ValueError("Fixture source exceeds frozen bounds")
        if self.baseline.sha256 != self.policy.baseline_sha256:
            raise ValueError("Policy baseline identity mismatch")
        if (
            self.binding.baseline_sha256 != self.baseline.sha256
            or type(self.binding.revision) is not int
            or self.binding.revision < 1
            or not self.binding.attempt_id
            or not self.binding.instance_id
        ):
            raise ValueError("Bind the fixture to an assigned development revision")
        for value in (self.binding.contract_sha256, self.binding.plan_sha256):
            require_digest(value)
        if self.binding.artifact_sha256 is not None:
            require_digest(self.binding.artifact_sha256)
        if any(
            len(p) > 240 or len(p.split("/")) > 16
            for p in (*files, *self.policy.create_under)
        ):
            raise ValueError("Source path exceeds frozen length/depth bounds")
        if self.policy.delete or not set(self.policy.modify) <= files.keys():
            raise ValueError("This profile supports existing-file edits, not deletion")
        unique_paths(tuple(sorted(set(files) | set(self.policy.create_under))))
        for root in self.policy.create_under:
            if any(
                p == root or p.startswith(root + "/") or root.startswith(p + "/")
                for p in files
            ):
                raise ValueError("Creation subtree must be entirely new")
            if any(
                other != root and root.startswith(other + "/")
                for other in self.policy.create_under
            ):
                raise ValueError("Overlapping creation roots are unsupported")

    @property
    def name(self) -> str:
        return "recollect-selfmod-fixture-" + self.run_id

    @property
    def payload(self) -> dict:
        return {
            "version": 1,
            "run_id": self.run_id,
            "policy": asdict(self.policy),
            "baseline_sha256": self.baseline.sha256,
            "entrypoint": self.entrypoint,
            "timeout_ms": self.timeout_ms,
            "binding": asdict(self.binding),
            "files": [
                {"path": f.path, "base64": base64.b64encode(f.content).decode()}
                for f in sorted(self.baseline.files, key=lambda f: f.path)
            ],
            "limits": {
                "files": MAX_FILES,
                "file_bytes": MAX_FILE_BYTES,
                "source_bytes": MAX_SOURCE_BYTES,
                "log_bytes": MAX_LOG_BYTES,
                "path_chars": 240,
                "path_depth": 16,
            },
        }

    @property
    def sha256(self) -> str:
        return sha256(
            encode(
                {
                    "payload": self.payload,
                    "image_id": self.image_id,
                    "image_environment": self.image_environment,
                }
            )
        )


def _environment(values: tuple[str, ...] | list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        if not isinstance(value, str) or "=" not in value or "\0" in value:
            raise ValueError("Invalid image environment")
        name, content = value.split("=", 1)
        if not name or name in result:
            raise ValueError("Duplicate image environment")
        result[name] = content
    return result


def create_arguments(spec: FixtureSpec, input_dir: Path) -> list[str]:
    source = str(root_path(input_dir))
    if "," in source:
        raise ValueError("Docker bind paths containing commas are unsupported")
    args = [
        "create",
        "--pull=never",
        "--runtime",
        "runc",
        "--oom-kill-disable=false",
        "--name",
        spec.name,
        "--label",
        "recollect.selfmod=" + spec.run_id,
        "--label",
        "recollect.spec=" + spec.sha256,
        "--interactive",
        "--user",
        "0:0",
        "--workdir",
        "/work",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--network",
        "none",
        "--ipc",
        "none",
        "--cgroupns",
        "private",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--cpus",
        "1",
        "--pids-limit",
        "32",
        "--ulimit",
        "nofile=64:64",
        "--ulimit",
        "core=0:0",
        "--log-driver",
        "none",
        "--restart",
        "no",
        "--stop-timeout",
        "1",
        "--no-healthcheck",
        "--entrypoint",
        "/usr/local/bin/python",
        "--mount",
        f"type=bind,source={source},target=/input,readonly,bind-propagation=rprivate",
    ]
    for capability in CAPABILITIES:
        args.extend(("--cap-add", capability))
    for path, options in TMPFS.items():
        args.extend(("--tmpfs", path + ":" + options))
    for name, value in ENVIRONMENT.items():
        args.extend(("--env", name + "=" + value))
    return [*args, spec.image_id, "-I", "-S", "-u", "-B", "/input/supervisor.py"]


def attest(inspection: dict, spec: FixtureSpec, input_dir: Path, container_id: str):
    """Check the stopped-or-waiting container before any worker release."""
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise IntegrityError("Invalid container identity")
    host, config = inspection.get("HostConfig", {}), inspection.get("Config", {})
    expected = {
        "Privileged": False,
        "ReadonlyRootfs": True,
        "NetworkMode": "none",
        "IpcMode": "none",
        "PidMode": "",
        "UTSMode": "",
        "CgroupnsMode": "private",
        "Memory": 256 * 1024 * 1024,
        "MemorySwap": 256 * 1024 * 1024,
        "NanoCpus": 1_000_000_000,
        "PidsLimit": 32,
        "CapDrop": ["ALL"],
        "Tmpfs": TMPFS,
        "Runtime": "runc",
    }
    if any(host.get(key) != value for key, value in expected.items()):
        raise IntegrityError("Container resource/namespace policy mismatch")
    # Docker discards this optional flag on cgroup v2, reporting JSON null after
    # start. Neither null (unset) nor false disables OOM killing; true does.
    # Require the field and exact types, rather than accepting arbitrary falsy data.
    if "OomKillDisable" not in host or (
        host["OomKillDisable"] is not False and host["OomKillDisable"] is not None
    ):
        raise IntegrityError("Container OOM-killer policy mismatch")
    masked, readonly = host.get("MaskedPaths", []), host.get("ReadonlyPaths", [])
    if (
        len(masked) != len(set(masked))
        or not set(MASKED_PATHS) <= set(masked)
        or any(
            p not in MASKED_PATHS
            and not re.fullmatch(
                r"/sys/devices/system/cpu/cpu[0-9]+/thermal_throttle", p
            )
            for p in masked
        )
        or sorted(readonly) != sorted(READONLY_PATHS)
    ):
        raise IntegrityError("Kernel pseudo-filesystem mask policy mismatch")
    caps = [c.removeprefix("CAP_") for c in host.get("CapAdd", [])]
    if sorted(caps) != list(CAPABILITIES):
        raise IntegrityError("Supervisor capability policy mismatch")
    if host.get("SecurityOpt") not in (
        ["no-new-privileges:true"],
        ["no-new-privileges=true"],
        ["no-new-privileges"],
    ):
        raise IntegrityError("Container security options mismatch")
    if (
        any(
            host.get(k)
            for k in (
                "Devices",
                "DeviceRequests",
                "Binds",
                "VolumesFrom",
                "PortBindings",
                "ExtraHosts",
                "GroupAdd",
                "Dns",
                "Links",
                "Sysctls",
                "StorageOpt",
            )
        )
        or host.get("UsernsMode") == "host"
    ):
        raise IntegrityError("Unexpected container access or mount configuration")
    if (
        host.get("LogConfig") != {"Type": "none", "Config": {}}
        or host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or sorted(host.get("Ulimits", []), key=lambda v: v["Name"])
        != [
            {"Name": "core", "Hard": 0, "Soft": 0},
            {"Name": "nofile", "Hard": 64, "Soft": 64},
        ]
    ):
        raise IntegrityError("Container lifecycle/output policy mismatch")
    if (
        inspection.get("Id") != container_id
        or inspection.get("Image") != spec.image_id
        or inspection.get("Name") != "/" + spec.name
        or config.get("Image") != spec.image_id
        or config.get("User") != "0:0"
        or config.get("WorkingDir") != "/work"
        or config.get("Tty") is not False
        or config.get("OpenStdin") is not True
        or config.get("Entrypoint") != ["/usr/local/bin/python"]
        or config.get("Cmd") != ["-I", "-S", "-u", "-B", "/input/supervisor.py"]
        or config.get("Healthcheck", {}).get("Test") != ["NONE"]
        or config.get("Volumes")
        or config.get("StopTimeout") != 1
        or host.get("Init") not in (None, False)
        or host.get("AutoRemove") is not False
        or config.get("Labels", {}).get("recollect.selfmod") != spec.run_id
        or config.get("Labels", {}).get("recollect.spec") != spec.sha256
        or _environment(config.get("Env", []))
        != {
            **_environment(spec.image_environment),
            **ENVIRONMENT,
        }
    ):
        raise IntegrityError("Container execution identity mismatch")
    binds = [m for m in inspection.get("Mounts", []) if m.get("Type") != "tmpfs"]
    if len(binds) != 1 or (
        binds[0].get("Type") != "bind"
        or binds[0].get("Destination") != "/input"
        or binds[0].get("RW") is not False
        or binds[0].get("Propagation") != "rprivate"
        or Path(binds[0].get("Source", "")).absolute() != input_dir.absolute()
    ):
        raise IntegrityError("Unexpected host mounts")
    for mount in inspection.get("Mounts", []):
        if mount.get("Type") == "tmpfs" and mount.get("Destination") not in TMPFS:
            raise IntegrityError("Unexpected writable tmpfs")
    destinations = [m.get("Destination") for m in inspection.get("Mounts", [])]
    if len(destinations) != len(set(destinations)):
        raise IntegrityError("Duplicate effective mount")


def frozen_input(spec: FixtureSpec) -> Snapshot:
    path = Path(__file__).with_name("fixture_supervisor.py")
    regular(path)
    supervisor = path.read_bytes()
    envelope = {
        "payload": spec.payload,
        "image_id": spec.image_id,
        "image_environment": spec.image_environment,
        "spec_sha256": spec.sha256,
        "supervisor_sha256": sha256(supervisor),
    }
    return Snapshot(
        (File("spec.json", encode(envelope)), File("supervisor.py", supervisor))
    )


def wire_binding(spec: FixtureSpec, supervisor_sha256: str) -> dict:
    require_digest(supervisor_sha256)
    return {
        "run_id": spec.run_id,
        "spec_sha256": spec.sha256,
        "supervisor_sha256": supervisor_sha256,
    }


def verify_ready(data: bytes, spec: FixtureSpec, supervisor_sha256: str):
    if len(data) > 2048 or decode(data) != {
        "kind": "ready",
        **wire_binding(spec, supervisor_sha256),
    }:
        raise IntegrityError("Supervisor readiness identity mismatch")


def release_record(
    spec: FixtureSpec, supervisor_sha256: str, remaining_ms: int | None
) -> bytes:
    """Called only after attestation; remaining time must include host setup cost."""
    if (spec.timeout_ms is None and remaining_ms is not None) or (
        spec.timeout_ms is not None and (
            type(remaining_ms) is not int or not 1 <= remaining_ms <= spec.timeout_ms
        )
    ):
        raise IntegrityError("Release cannot renew the frozen execution budget")
    return encode(
        {
            "kind": "release",
            **wire_binding(spec, supervisor_sha256),
            "remaining_ms": remaining_ms,
        }
    )


def verified_snapshot(
    data: bytes, spec: FixtureSpec, supervisor_sha256: str
) -> Snapshot:
    """Failure reports stay raw evidence, never usable candidate snapshots.

    The caller must independently attest process termination and persist the raw
    report before invoking development/plan reconstruction; this is not submission.
    """
    if len(data) > MAX_WIRE_BYTES:
        raise IntegrityError("Oversized supervisor report")
    result = decode(data)
    fields = {
        "kind",
        "run_id",
        "spec_sha256",
        "supervisor_sha256",
        "binding",
        "reason",
        "exitcode",
        "quiescent",
        "capture_complete",
        "errors",
        "entries",
        "stdout",
        "stderr",
        "files",
        "snapshot_sha256",
    }
    if (
        set(result) != fields
        or result["kind"] != "result"
        or any(result[k] != v for k, v in wire_binding(spec, supervisor_sha256).items())
        or encode(result["binding"]) != encode(asdict(spec.binding))
        or result["reason"] != "completed"
        or type(result["exitcode"]) is not int
        or result["exitcode"] != 0
        or result["quiescent"] is not True
        or result["capture_complete"] is not True
        or result["errors"] != []
    ):
        raise IntegrityError("Unusable or mismatched fixture result")
    for stream in ("stdout", "stderr"):
        if len(base64.b64decode(result[stream], validate=True)) > MAX_LOG_BYTES:
            raise IntegrityError("Output exceeds retained log limit")
    if not isinstance(result["files"], list) or len(result["files"]) > MAX_FILES:
        raise IntegrityError("Invalid captured file inventory")
    files = []
    for item in result["files"]:
        if set(item) != {"path", "base64"}:
            raise IntegrityError("Invalid captured file")
        content = base64.b64decode(item["base64"], validate=True)
        if len(content) > MAX_FILE_BYTES:
            raise IntegrityError("Captured file exceeds limit")
        files.append(File(item["path"], content))
    snapshot = Snapshot(tuple(files))
    if (
        snapshot.sha256 != result["snapshot_sha256"]
        or sum(len(f.content) for f in files) > MAX_SOURCE_BYTES
    ):
        raise IntegrityError("Captured snapshot digest/size mismatch")
    old = {f.path: f.content for f in spec.baseline.files}
    new = {f.path: f.content for f in files}
    unique_paths(tuple(sorted(old.keys() | new.keys())))
    if not old.keys() <= new.keys():
        raise IntegrityError("Captured baseline file was removed")
    for path, content in new.items():
        if (
            path in old
            and old[path] != content
            and path not in spec.policy.modify
            or path not in old
            and not spec.policy.permits(path, "create")
        ):
            raise IntegrityError("Captured source violates change policy")
    _verify_entries(result["entries"], spec, new)
    return snapshot


def _verify_entries(entries, spec, files):
    if not isinstance(entries, list) or len(entries) > MAX_FILES * 16:
        raise IntegrityError("Invalid entry metadata inventory")
    paths, captured_files = [], set()
    baseline_dirs = {
        "/".join(f.path.split("/")[:i])
        for f in spec.baseline.files
        for i in range(1, len(f.path.split("/")))
    }
    root_parents = {
        "/".join(p.split("/")[:i])
        for p in spec.policy.create_under
        for i in range(1, len(p.split("/")))
    }
    dirs = set()
    baseline_files = {f.path for f in spec.baseline.files}
    for entry in entries:
        if set(entry) != {"path", "kind", "mode", "uid", "gid", "links", "bytes"}:
            raise IntegrityError("Invalid entry metadata fields")
        path = entry["path"]
        require_path(path)
        if len(path) > 240 or len(path.split("/")) > 16:
            raise IntegrityError("Captured path exceeds bounds")
        paths.append(path)
        if any(
            type(entry[k]) is not int or entry[k] < 0
            for k in ("mode", "uid", "gid", "links", "bytes")
        ):
            raise IntegrityError("Invalid entry metadata types")
        if entry["kind"] == "file" and path in files:
            captured_files.add(path)
            expected = (
                (0, WORKER_UID, 0o664)
                if path in spec.policy.modify
                else (0, 0, 0o444)
                if path in baseline_files
                else (WORKER_UID, WORKER_UID, 0o644)
            )
            if entry["links"] != 1 or entry["bytes"] != len(files[path]):
                raise IntegrityError("Captured file link/size mismatch")
        elif entry["kind"] == "directory":
            dirs.add(path)
            if path in spec.policy.create_under:
                expected = (0, WORKER_UID, 0o775)
            elif path in baseline_dirs | root_parents:
                expected = (0, 0, 0o555)
            elif spec.policy.permits(path, "create"):
                expected = (WORKER_UID, WORKER_UID, 0o755)
            else:
                raise IntegrityError("Unapproved captured directory")
        else:
            raise IntegrityError("Special or missing captured entry")
        if (entry["uid"], entry["gid"], entry["mode"]) != expected:
            raise IntegrityError("Captured mode/ownership mismatch")
    unique_paths(tuple(paths))
    required_dirs = (
        baseline_dirs
        | root_parents
        | set(spec.policy.create_under)
        | {
            "/".join(p.split("/")[:i])
            for p in paths
            for i in range(1, len(p.split("/")))
        }
    )
    if captured_files != files.keys() or not required_dirs <= dirs:
        raise IntegrityError("Incomplete captured entry inventory")
