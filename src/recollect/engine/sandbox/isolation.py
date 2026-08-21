"""Fail-closed container boundary for the OpenCode backend.

The generated OpenCode permissions remain useful defense in depth, but
they are not a security boundary: a host process still has the user's OS
token. Production launches therefore go through this fixed container
profile. No caller-provided extra arguments, host network, Docker socket,
repository mount, privilege, or Linux capability can enter the command.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class IsolationError(RuntimeError):
    """The required containment boundary is unavailable or misconfigured."""


@dataclass(frozen=True)
class ContainerLaunch:
    runtime: str
    name: str
    argv: list[str]


def container_model_url(base_url: str) -> str:
    """Route a host-loopback model URL through Docker's host gateway."""
    parts = urlsplit(base_url)
    if parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return base_url
    port = f":{parts.port}" if parts.port is not None else ""
    return urlunsplit(
        (
            parts.scheme,
            f"host.docker.internal{port}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def _runtime(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise IsolationError(
            f"container runtime {name!r} was not found; install Docker, build "
            "the pinned sandbox image, or use the legacy backend"
        )
    return resolved


def _mount(source: Path, destination: str, *, readonly: bool) -> str:
    resolved = str(source.resolve())
    if "," in resolved:
        raise IsolationError("sandbox paths containing commas are unsupported")
    mode = ",readonly" if readonly else ""
    return f"type=bind,source={resolved},target={destination}{mode}"


def build_container_launch(
    *,
    runtime: str,
    image: str,
    name: str,
    host_port: int,
    workspace: Path,
    config_dir: Path,
    password: str,
    memory_mb: int,
    pids: int,
    cpus: float,
) -> ContainerLaunch:
    """Build the complete, non-extensible container command."""
    runtime_path = _runtime(runtime)
    if not image.strip() or any(char.isspace() for char in image):
        raise IsolationError("sandbox container image must be one token")
    user = (
        f"{os.getuid()}:{os.getgid()}"
        if os.name != "nt" and hasattr(os, "getuid")
        else "65532:65532"
    )
    argv = [
        runtime_path,
        "run",
        "--rm",
        "--name",
        name,
        "--hostname",
        "recollect-subagent",
        "--user",
        user,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--network",
        "bridge",
        "--ipc",
        "none",
        "--pids-limit",
        str(pids),
        "--memory",
        f"{memory_mb}m",
        "--memory-swap",
        f"{memory_mb}m",
        "--cpus",
        str(cpus),
        "--ulimit",
        "nofile=1024:1024",
        "--stop-timeout",
        "5",
        "--publish",
        f"127.0.0.1:{host_port}:4096",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--mount",
        _mount(workspace, "/workspace", readonly=False),
        "--mount",
        _mount(config_dir, "/config", readonly=True),
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs",
        "/state:rw,noexec,nosuid,nodev,size=256m",
        "--env",
        "HOME=/state/home",
        "--env",
        "XDG_DATA_HOME=/state/data",
        "--env",
        "XDG_CACHE_HOME=/state/cache",
        "--env",
        "OPENCODE_CONFIG=/config/opencode.json",
        "--env",
        f"OPENCODE_SERVER_PASSWORD={password}",
        image,
        "opencode",
        "serve",
        "--port",
        "4096",
        "--hostname",
        "0.0.0.0",
    ]
    return ContainerLaunch(runtime=runtime_path, name=name, argv=argv)


def attest_container(
    inspection: Any,
    *,
    name: str,
    image: str,
    workspace: Path,
    config_dir: Path,
    host_port: int,
    memory_mb: int,
    pids: int,
    cpus: float,
) -> None:
    """Reject a running container whose effective policy drifted."""
    if not isinstance(inspection, list) or len(inspection) != 1:
        raise IsolationError("container inspection returned an unexpected shape")
    item = inspection[0]
    host = item.get("HostConfig") or {}
    config = item.get("Config") or {}
    state = item.get("State") or {}
    if item.get("Name", "").lstrip("/") != name or not state.get("Running"):
        raise IsolationError("sandbox container identity or running state mismatched")
    if config.get("Image") != image:
        raise IsolationError("sandbox container image mismatched")
    if not config.get("User") or config.get("User") in {"0", "0:0", "root"}:
        raise IsolationError("sandbox container must not run as root")
    if host.get("Privileged") or not host.get("ReadonlyRootfs"):
        raise IsolationError("sandbox container privilege/rootfs policy mismatched")
    if "ALL" not in (host.get("CapDrop") or []):
        raise IsolationError("sandbox container did not drop all capabilities")
    security = " ".join(host.get("SecurityOpt") or [])
    if "no-new-privileges" not in security:
        raise IsolationError("sandbox container lacks no-new-privileges")
    if host.get("NetworkMode") != "bridge" or host.get("IpcMode") != "none":
        raise IsolationError("sandbox container uses a host namespace")
    if host.get("PidMode") or host.get("UTSMode") or host.get("UsernsMode") == "host":
        raise IsolationError("sandbox container has an unexpected namespace mode")
    if host.get("CgroupnsMode") == "host":
        raise IsolationError("sandbox container uses the host cgroup namespace")
    if int(host.get("PidsLimit") or 0) != pids:
        raise IsolationError("sandbox container PID limit mismatched")
    if int(host.get("Memory") or 0) < memory_mb * 1_000_000:
        raise IsolationError("sandbox container memory limit mismatched")
    if int(host.get("MemorySwap") or 0) != memory_mb * 1024 * 1024:
        raise IsolationError("sandbox container swap limit mismatched")
    if int(host.get("NanoCpus") or 0) != round(cpus * 1_000_000_000):
        raise IsolationError("sandbox container CPU limit mismatched")
    ulimits = host.get("Ulimits") or []
    if ulimits != [{"Name": "nofile", "Hard": 1024, "Soft": 1024}]:
        raise IsolationError("sandbox container file limit mismatched")
    if host.get("Devices") or host.get("DeviceRequests"):
        raise IsolationError("sandbox container has host device access")
    if "unconfined" in security.lower():
        raise IsolationError("sandbox container disables a security profile")

    expected_ports = {
        "4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(host_port)}]
    }
    if host.get("PortBindings") != expected_ports:
        raise IsolationError("sandbox container port binding mismatched")

    expected = {
        "/workspace": (str(workspace.resolve()), True),
        "/config": (str(config_dir.resolve()), False),
    }
    actual: dict[str, tuple[str, bool]] = {}
    for mount in item.get("Mounts") or []:
        if mount.get("Type") != "bind":
            raise IsolationError("sandbox container has an unexpected mount")
        actual[str(mount.get("Destination"))] = (
            str(Path(str(mount.get("Source"))).resolve()),
            bool(mount.get("RW")),
        )
    if actual != expected:
        raise IsolationError("sandbox container bind mounts mismatched")
