"""Frozen native supervisor profile, independently checked before host release."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .containment import MASKED_PATHS, READONLY_PATHS, _environment
from .contracts import File, Snapshot, require_digest
from .journal import IntegrityError, decode, encode, regular, root_path, sha256
from .native_capture import NativeCaptureSpec

CAPABILITIES = ("CHOWN", "DAC_READ_SEARCH", "KILL", "SETGID", "SETUID")
TMPFS = {
    "/work": "rw,noexec,nosuid,nodev,size=32m,mode=0755",
    "/state": "rw,noexec,nosuid,nodev,size=256m,mode=0700,uid=65532,gid=65532",
    "/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777",
    "/evidence": "rw,noexec,nosuid,nodev,size=256m,mode=0700",
}
ENVIRONMENT = {"HOME": "/evidence", "PATH": "/usr/local/bin:/usr/bin:/bin",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
ENTRYPOINT = ["-I", "-S", "-u", "-B", "/authority/native_supervisor.py"]


def _isolation():
    return {"capabilities": CAPABILITIES, "tmpfs": dict(TMPFS),
            "environment": dict(ENVIRONMENT), "memory_bytes": 2**30,
            "pids": 256, "nofile": 1024}


def _require_profile(spec):
    if encode(spec.payload["isolation"]) != encode(_isolation()):
        raise IntegrityError("Native isolation profile changed after freezing")


@dataclass(frozen=True)
class NativeRuntimeSpec:
    capture: NativeCaptureSpec
    image_id: str
    image_environment: tuple[str, ...]
    native_binary_sha256: str
    _helpers: Snapshot = field(init=False, repr=False)
    _payload: bytes = field(init=False, repr=False)

    def __post_init__(self):
        if type(self.capture) is not NativeCaptureSpec:
            raise ValueError("Owned native capture spec required")
        if not self.image_id.startswith("sha256:"):
            raise ValueError("Immutable local native image required")
        require_digest(self.image_id[7:])
        require_digest(self.native_binary_sha256)
        if type(self.image_environment) is not tuple:
            raise ValueError("Freeze native image environment")
        _environment(self.image_environment)
        helpers = []
        for name in ("native_supervisor.py", "native_model_proxy.py",
                     "native_http_relay.py"):
            path = Path(__file__).with_name(name)
            regular(path)
            helpers.append(File(name, path.read_bytes()))
        object.__setattr__(self, "_helpers", Snapshot(tuple(helpers)))
        object.__setattr__(self, "_payload", encode(self._build_payload()))

    @property
    def run_id(self):
        return self.capture.run.run_id

    @property
    def name(self):
        return "recollect-selfmod-native-" + self.run_id

    def _build_payload(self):
        helpers = (*self._helpers.files, *(f for f in self.capture.inputs.files
                                         if f.path.endswith(".py")))
        return {
            "version": 1, "run_id": self.run_id,
            "capture_spec_sha256": self.capture.sha256,
            "native_binary_sha256": self.native_binary_sha256,
            "helpers": {f.path: sha256(f.content) for f in helpers},
            "image_id": self.image_id, "image_environment": self.image_environment,
            "isolation": _isolation(),
        }

    @property
    def payload(self):
        return decode(self._payload)

    @property
    def sha256(self):
        return sha256(encode(self.payload))

    @property
    def inputs(self):
        return Snapshot((*self.capture.inputs.files, *self._helpers.files,
                         File("runtime-spec.json", encode({
                             "payload": self.payload, "sha256": self.sha256}))))

    def control(self, kind):
        if kind not in {"release", "fence"}:
            raise ValueError("Unknown native supervisor control")
        return encode({"kind": kind, "run_id": self.run_id,
                       "runtime_sha256": self.sha256})


def create_arguments(spec, input_dir):
    _require_profile(spec)
    source = str(root_path(input_dir))
    if "," in source:
        raise ValueError("Comma in native bind path")
    args = [
        "create", "--pull=never", "--runtime", "runc", "--oom-kill-disable=false",
        "--name", spec.name, "--label", "recollect.selfmod=" + spec.run_id,
        "--label", "recollect.spec=" + spec.sha256, "--interactive", "--user", "0:0",
        "--workdir", "/work", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--network", "none",
        "--ipc", "none", "--cgroupns", "private", "--memory", "1024m",
        "--memory-swap", "1024m", "--cpus", "1", "--pids-limit", "256",
        "--ulimit", "nofile=1024:1024", "--ulimit", "core=0:0",
        "--log-driver", "none", "--restart", "no", "--stop-timeout", "1",
        "--no-healthcheck", "--entrypoint", "/usr/local/bin/python", "--mount",
        f"type=bind,source={source},target=/authority,readonly,bind-propagation=rprivate",
    ]
    for capability in CAPABILITIES:
        args.extend(("--cap-add", capability))
    for path, options in TMPFS.items():
        args.extend(("--tmpfs", path + ":" + options))
    for key, value in ENVIRONMENT.items():
        args.extend(("--env", key + "=" + value))
    return [*args, spec.image_id, *ENTRYPOINT]


def attest(value, spec, input_dir, container_id):
    _require_profile(spec)
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise IntegrityError("Invalid native container identity")
    host, config = value.get("HostConfig", {}), value.get("Config", {})
    expected = {
        "Privileged": False, "ReadonlyRootfs": True, "NetworkMode": "none",
        "IpcMode": "none", "PidMode": "", "UTSMode": "", "CgroupnsMode": "private",
        "Memory": 2**30, "MemorySwap": 2**30, "NanoCpus": 1_000_000_000,
        "PidsLimit": 256, "CapDrop": ["ALL"], "Tmpfs": TMPFS, "Runtime": "runc",
        "LogConfig": {"Type": "none", "Config": {}},
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        "AutoRemove": False,
    }
    if any(key not in host or encode(host[key]) != encode(wanted)
           for key, wanted in expected.items()):
        raise IntegrityError("Native resource/namespace policy mismatch")
    if ("OomKillDisable" not in host or host["OomKillDisable"] is not None
            and host["OomKillDisable"] is not False):
        raise IntegrityError("Native OOM policy mismatch")
    if (host.get("Init") is not None and host.get("Init") is not False
            or host.get("UsernsMode") == "host"
            or any(host.get(k) for k in (
                "Devices", "DeviceRequests", "Binds", "VolumesFrom", "PortBindings",
                "ExtraHosts", "GroupAdd", "Dns", "Links", "Sysctls", "StorageOpt"))):
        raise IntegrityError("Unexpected native host access")
    if sorted(c.removeprefix("CAP_") for c in host.get("CapAdd", [])) != list(
        CAPABILITIES
    ) or host.get("SecurityOpt") not in (
        ["no-new-privileges:true"], ["no-new-privileges=true"], ["no-new-privileges"],
    ):
        raise IntegrityError("Native capabilities/security mismatch")
    masked = host.get("MaskedPaths", [])
    if (len(masked) != len(set(masked)) or not set(MASKED_PATHS) <= set(masked)
            or any(p not in MASKED_PATHS and not re.fullmatch(
                r"/sys/devices/system/cpu/cpu[0-9]+/thermal_throttle", p)
                   for p in masked)
            or sorted(host.get("ReadonlyPaths", [])) != sorted(READONLY_PATHS)):
        raise IntegrityError("Native kernel mount policy mismatch")
    if sorted(host.get("Ulimits", []), key=lambda v: v["Name"]) != [
        {"Name": "core", "Hard": 0, "Soft": 0},
        {"Name": "nofile", "Hard": 1024, "Soft": 1024},
    ]:
        raise IntegrityError("Native file descriptor policy mismatch")
    identity = {
        "Image": spec.image_id, "User": "0:0", "WorkingDir": "/work",
        "Tty": False, "OpenStdin": True, "Entrypoint": ["/usr/local/bin/python"],
        "Cmd": ENTRYPOINT, "StopTimeout": 1,
    }
    if (value.get("Id") != container_id or value.get("Image") != spec.image_id
            or value.get("Name") != "/" + spec.name
            or any(key not in config or encode(config[key]) != encode(wanted)
                   for key, wanted in identity.items())
            or config.get("Healthcheck", {}).get("Test") != ["NONE"]
            or config.get("Volumes")
            or config.get("Labels", {}).get("recollect.selfmod") != spec.run_id
            or config.get("Labels", {}).get("recollect.spec") != spec.sha256
            or _environment(config.get("Env", [])) != {
                **_environment(spec.image_environment), **ENVIRONMENT}):
        raise IntegrityError("Native execution identity mismatch")
    mounts = value.get("Mounts", [])
    binds = [m for m in mounts if m.get("Type") != "tmpfs"]
    if (len(binds) != 1 or binds[0].get("Type") != "bind"
            or binds[0].get("Destination") != "/authority"
            or binds[0].get("RW") is not False
            or binds[0].get("Propagation") != "rprivate"
            or Path(binds[0].get("Source", "")).absolute() != input_dir.absolute()
            or any(m.get("Type") == "tmpfs" and m.get("Destination") not in TMPFS
                   for m in mounts)
            or len({m.get("Destination") for m in mounts}) != len(mounts)):
        raise IntegrityError("Native effective mount mismatch")
