"""Regression checks for policy drift and stalled event consumers."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from recollect.engine.sandbox import runner as runner_module
from recollect.engine.sandbox.isolation import IsolationError, attest_container
from recollect.engine.sandbox.runner import OpenCodeRunner
from recollect.engine.subagent import SubagentStep


def _inspection(workspace, config):
    return [{
        "Name": "/research-test", "State": {"Running": True},
        "Config": {"Image": "sandbox:1", "User": "65532:65532"},
        "HostConfig": {
            "Privileged": False, "ReadonlyRootfs": True,
            "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
            "NetworkMode": "bridge", "IpcMode": "none", "PidsLimit": 256,
            "Memory": 1024**3, "MemorySwap": 1024**3, "NanoCpus": 2_000_000_000,
            "Ulimits": [{"Name": "nofile", "Hard": 1024, "Soft": 1024}],
            "PortBindings": {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9"}]},
        },
        "Mounts": [
            {"Type": "bind", "Source": str(workspace.resolve()),
             "Destination": "/workspace", "RW": True},
            {"Type": "bind", "Source": str(config.resolve()),
             "Destination": "/config", "RW": False},
        ],
    }]


def _attest(document, workspace, config):
    attest_container(
        document, name="research-test", image="sandbox:1", workspace=workspace,
        config_dir=config, host_port=9, memory_mb=1024, pids=256, cpus=2.0,
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("CapAdd", ["SYS_ADMIN"]),
        ("CapAdd", ["NET_ADMIN"]),
        ("CapDrop", ["ALL", "NET_RAW"]),
        ("SecurityOpt", ["no-new-privileges:false"]),
        ("SecurityOpt", ["no-new-privileges=false"]),
        ("SecurityOpt", ["anything-no-new-privileges"]),
        ("SecurityOpt", ["no-new-privileges:true", "seccomp=unconfined"]),
        ("SecurityOpt", ["no-new-privileges:true", "apparmor=unconfined"]),
        ("SecurityOpt", ["no-new-privileges:true", "no-new-privileges:false"]),
        ("Memory", 2 * 1024**3),
        ("Memory", 1024 * 1_000_000),
    ],
)
def test_attestation_rejects_capability_security_and_memory_drift(tmp_path, key, value):
    workspace, config = tmp_path / "workspace", tmp_path / "config"
    document = _inspection(workspace, config)
    document[0]["HostConfig"][key] = value
    with pytest.raises(IsolationError):
        _attest(document, workspace, config)


@pytest.mark.parametrize(
    "spelling",
    ["no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"],
)
def test_attestation_accepts_docker_true_normalizations(tmp_path, spelling):
    workspace, config = tmp_path / "workspace", tmp_path / "config"
    document = _inspection(workspace, config)
    document[0]["HostConfig"]["SecurityOpt"] = [spelling]
    _attest(document, workspace, config)


async def test_paused_research_consumer_backpressures_and_closes_event_stream():
    reads = 0
    closed = asyncio.Event()
    filled = asyncio.Event()
    post_cancelled = asyncio.Event()

    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal reads
            for index in range(5000):
                reads += 1
                event = {
                    "type": "message.part.updated",
                    "properties": {
                        "part": {
                            "type": "tool", "sessionID": "oc1",
                            "callID": str(index), "tool": "read",
                            "state": {"status": "completed", "output": "source"},
                        }
                    },
                }
                if reads == runner_module._EVENT_QUEUE_SIZE + 2:
                    filled.set()
                yield f"data: {json.dumps(event)}\n\n".encode()

        async def aclose(self):
            closed.set()

    async def respond(request):
        if request.url.path == "/event":
            return httpx.Response(200, stream=Events())
        try:
            await asyncio.Event().wait()
        finally:
            post_cancelled.set()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://sandbox.invalid"
    ) as client:
        handle = SimpleNamespace(client=client, last_used=0)
        invocation = SimpleNamespace(handle=handle, oc_session_id="oc1")
        config = SimpleNamespace(subagent_observation_chars=4000)
        runner = OpenCodeRunner(None, config)
        stream = runner._run_invocation(invocation, "task", "task", 0)
        try:
            first = await asyncio.wait_for(anext(stream), 1)
            assert isinstance(first, SubagentStep)
            await asyncio.wait_for(filled.wait(), 1)
            assert reads == runner_module._EVENT_QUEUE_SIZE + 2
        finally:
            await stream.aclose()
    assert closed.is_set()
    assert post_cancelled.is_set()
