"""Networkless source fixtures and explicit fake Docker inspection records."""

import base64
from dataclasses import asdict

from recollect.selfmod.containment import (
    CAPABILITIES,
    ENVIRONMENT,
    MASKED_PATHS,
    READONLY_PATHS,
    TMPFS,
    FixtureSpec,
    wire_binding,
)
from recollect.selfmod.contracts import ChangePolicy, File, Snapshot
from recollect.selfmod.development import Binding
from recollect.selfmod.journal import encode

CONTAINER_ID = "c" * 64
SUPERVISOR_SHA = "d" * 64


def spec(code=b"print('fixture')", *, timeout_ms=2000):
    baseline = Snapshot(
        (
            File("fixture.py", code),
            File("editable.py", b"value = 1\n"),
            File("protected.py", b"protected original bytes"),
        )
    )
    policy = ChangePolicy(
        baseline.sha256, modify=("editable.py",), create_under=("generated",)
    )
    binding = Binding(
        "fixture-attempt",
        "fixture-author",
        1,
        "1" * 64,
        baseline.sha256,
        "2" * 64,
        None,
    )
    return FixtureSpec(
        "a" * 32,
        "sha256:" + "b" * 64,
        ("LANG=C.UTF-8",),
        baseline,
        policy,
        "fixture.py",
        timeout_ms,
        binding,
    )


def inspection(root, config):
    return {
        "Id": CONTAINER_ID,
        "Name": "/" + config.name,
        "Image": config.image_id,
        "State": {"Status": "running", "Running": True, "Pid": 1000},
        "HostConfig": {
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
            "CapAdd": list(CAPABILITIES),
            "Tmpfs": dict(TMPFS),
            "SecurityOpt": ["no-new-privileges:true"],
            "LogConfig": {"Type": "none", "Config": {}},
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "AutoRemove": False,
            "Runtime": "runc",
            "OomKillDisable": False,
            "MaskedPaths": list(MASKED_PATHS),
            "ReadonlyPaths": list(READONLY_PATHS),
            "Ulimits": [
                {"Name": "core", "Hard": 0, "Soft": 0},
                {"Name": "nofile", "Hard": 64, "Soft": 64},
            ],
        },
        "Config": {
            "Image": config.image_id,
            "User": "0:0",
            "WorkingDir": "/work",
            "Tty": False,
            "OpenStdin": True,
            "Entrypoint": ["/usr/local/bin/python"],
            "Cmd": ["-I", "-S", "-u", "-B", "/input/supervisor.py"],
            "StopTimeout": 1,
            "Labels": {
                "recollect.selfmod": config.run_id,
                "recollect.spec": config.sha256,
            },
            "Healthcheck": {"Test": ["NONE"]},
            "Env": [
                *config.image_environment,
                *(k + "=" + v for k, v in ENVIRONMENT.items()),
            ],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(root.absolute()),
                "Destination": "/input",
                "RW": False,
                "Propagation": "rprivate",
            }
        ],
    }


def report(config):
    files = list(config.baseline.files)
    entries = [
        {
            "path": f.path,
            "kind": "file",
            "uid": 0,
            "gid": 65532 if f.path in config.policy.modify else 0,
            "mode": 0o664 if f.path in config.policy.modify else 0o444,
            "links": 1,
            "bytes": len(f.content),
        }
        for f in files
    ]
    entries.append(
        {
            "path": "generated",
            "kind": "directory",
            "uid": 0,
            "gid": 65532,
            "mode": 0o775,
            "links": 2,
            "bytes": 40,
        }
    )
    return {
        "kind": "result",
        **wire_binding(config, SUPERVISOR_SHA),
        "binding": asdict(config.binding),
        "reason": "completed",
        "exitcode": 0,
        "quiescent": True,
        "capture_complete": True,
        "errors": [],
        "entries": entries,
        "stdout": base64.b64encode(b"fixture\n").decode(),
        "stderr": "",
        "files": [
            {"path": f.path, "base64": base64.b64encode(f.content).decode()}
            for f in files
        ],
        "snapshot_sha256": config.baseline.sha256,
    }


def report_bytes(config):
    return encode(report(config))
