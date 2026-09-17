"""Opt-in: the pinned OpenCode binary keeps a slow bundle tool alive via the tool host.

A scripted host provider makes the real sandbox agent call a non-target probe
tool that runs well past the MCP per-request timeout. Through the bundle's tool
host the call completes; the negative control serves the same tool server without
the host and must time out. No model, calendar or provider credential is used.
"""

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from recollect.config import RecollectConfig
from recollect.engine import toolhost
from recollect.engine.sandbox import configgen
from recollect.engine.sandbox.manager import SandboxDeployment, SandboxManager
from recollect.selfmod import subagent_tree
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.deployment import (
    BundleImages,
    SubagentBundle,
    materialize_skills,
)
from recollect.selfmod.docker import DockerCLI

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("RECOLLECT_RUN_SELFMOD_DOCKER_TESTS") != "1",
        reason="opt in to real selfmod Docker qualification",
    ),
]

REPO = Path(__file__).resolve().parents[1]
TOOL = "recollect_research_slow_probe"
TIMEOUT_MS = 25_000
SLOW_SECONDS = 60
PROBE = b'''

@mcp.tool()
async def slow_probe(seconds: float) -> str:
    """Non-target qualification tool: wait quietly, then answer."""
    import asyncio

    await asyncio.sleep(seconds)
    return "slow probe done"
'''


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        server = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with server.lock:
            server.requests.append(body)
        tools = {t["function"]["name"] for t in body.get("tools", [])}
        messages = body.get("messages", [])
        call = None
        if TOOL in tools and not any(m.get("role") == "tool" for m in messages):
            call = (TOOL, {"seconds": SLOW_SECONDS})
        delta = {"role": "assistant",
                 "content": None if call else "Probe turn finished."}
        if call:
            delta["tool_calls"] = [{
                "index": 0, "id": "call_" + str(time.time_ns()), "type": "function",
                "function": {"name": call[0], "arguments": json.dumps(call[1])}}]
        base = {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                "model": "fixture-model"}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for value in (
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {},
                                  "finish_reason": "tool_calls" if call else "stop"}],
             "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                       "total_tokens": 110}},
        ):
            self.wfile.write(("data: " + json.dumps(value) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def provider():
    server = ThreadingHTTPServer(("0.0.0.0", 0), Provider)
    server.lock, server.requests = threading.Lock(), []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
def bundle_image():
    executable = shutil.which("docker")
    environment = {k: v for k, v in os.environ.items()
                   if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH",
                                    "PATHEXT"}}
    share = Path(os.environ["LOCALAPPDATA"]) / "recollect" / "sandboxes"
    cli_root = share / ("selfmod-toolhost-cli-" + uuid.uuid4().hex)
    cli_root.mkdir(parents=True)
    (cli_root / "config.json").write_bytes(b'{"auths":{}}\n')
    endpoint = subprocess.run(
        [executable, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        env=environment, capture_output=True, check=True, text=True).stdout.strip()
    argv = (executable, "--config", str(cli_root), "--host", endpoint)
    base = subprocess.run([*argv, "image", "inspect", "--format", "{{.Id}}",
                           "recollect-opencode-sandbox:1.18.18"], env=environment,
                          capture_output=True, check=True, text=True).stdout.strip()
    tree = subagent_tree.baseline(REPO)
    # Define the probe before the module's __main__ block, so the direct-run
    # control registers it too; appending it would never execute under -m.
    marker = b"\n\ndef main() -> None:"
    files = tuple(File(f.path, f.content.replace(marker, PROBE + marker, 1))
                  if f.path == "recollect/engine/mcp_research.py" else f
                  for f in tree.files)
    assert PROBE in files[[f.path for f in files].index(
        "recollect/engine/mcp_research.py")].content
    bundle = SubagentBundle(Snapshot(files), base, subagent_tree.LAUNCH)
    images = BundleImages(DockerCLI(argv, tuple(environment.items())))
    image = asyncio.run(images.build(bundle))
    try:
        yield bundle, image, share
    finally:
        subprocess.run([*argv, "image", "rm", image], env=environment,
                       capture_output=True, check=False)
        shutil.rmtree(cli_root, ignore_errors=True)


async def run_probe(bundle, image, share, provider):
    root = share / ("selfmod-toolhost-" + uuid.uuid4().hex)
    skills = materialize_skills(bundle, root.with_name(root.name + "-skills"))
    config = RecollectConfig(
        embedding_model_path=share / "unused.gguf", data_dir=root / "var",
        sandbox_root=share, subagent_backend="opencode",
        subagent_continuous_enabled=True, experiment_unbounded=True,
        generator_model="fixture-model")
    manager = SandboxManager(config, deployment=SandboxDeployment(
        image, skills, root, subagent_tree.BUNDLE_PYTHONPATH))
    manager.configure_model(f"http://127.0.0.1:{provider.server_port}/v1", "token")
    invocation = await manager.begin_invocation("toolhost-qualification",
                                                continuous=True)
    try:
        client, session = invocation.handle.client, invocation.oc_session_id
        started = time.monotonic()
        await client.post(f"/session/{session}/message", timeout=None, json={
            "agent": "build",
            "parts": [{"type": "text", "text": "Run the slow probe tool once."}]})
        elapsed = time.monotonic() - started
        history = (await client.get(f"/session/{session}/message")).json()
        parts = [part for message in history for part in message.get("parts", [])
                 if part.get("type") == "tool" and part.get("tool") == TOOL]
        offered = sorted({t["function"]["name"] for r in provider.requests
                          for t in r.get("tools", [])})
        texts = [part.get("text") for message in history
                 for part in message.get("parts", []) if part.get("type") == "text"]
        status = await client.get("/mcp")
        diagnostics = {"offered_tools": offered, "texts": texts,
                       "mcp_status": status.status_code,
                       "mcp": status.json() if status.is_success else status.text}
        (REPO / ".agent" / ("toolhost-live-diagnostics-" + uuid.uuid4().hex
                            + ".json")).write_text(json.dumps(diagnostics, indent=1))
        return parts, elapsed, diagnostics
    finally:
        await manager.finish_invocation(invocation)
        await manager.close_all()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(skills, ignore_errors=True)


async def test_tool_host_keeps_a_slow_call_alive_past_the_mcp_timeout(
    bundle_image, provider, monkeypatch,
):
    bundle, image, share = bundle_image
    monkeypatch.setattr(configgen, "MCP_TIMEOUT_MS", TIMEOUT_MS)
    assert toolhost.KEEPALIVE_SECONDS * 1000 < TIMEOUT_MS < SLOW_SECONDS * 1000
    parts, elapsed, diagnostics = await run_probe(bundle, image, share, provider)
    assert len(parts) == 1, diagnostics
    state = parts[0]["state"]
    assert state["status"] == "completed", state
    assert "slow probe done" in state["output"]
    assert elapsed >= SLOW_SECONDS


async def test_same_tool_without_the_host_times_out(bundle_image, provider,
                                                   monkeypatch):
    bundle, image, share = bundle_image
    monkeypatch.setattr(configgen, "MCP_TIMEOUT_MS", TIMEOUT_MS)
    monkeypatch.setattr(configgen, "TOOL_HOST_MODULE",
                        "recollect.engine.mcp_research")
    parts, _, diagnostics = await run_probe(bundle, image, share, provider)
    assert len(parts) == 1, diagnostics
    state = parts[0]["state"]
    assert state["status"] == "error", state
    assert "timed out" in json.dumps(state).lower()

