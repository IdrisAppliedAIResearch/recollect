"""The sandbox manager against a stub opencode server.

The stub is a plain stdlib HTTP server that speaks just enough of the
opencode API (health + fresh session create/delete) for the manager's lifecycle
code. The real opencode binary is never touched here.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from recollect.config import RecollectConfig
from recollect.engine.sandbox.manager import (
    SandboxHandle,
    SandboxManager,
    SandboxStartError,
)

_STUB = textwrap.dedent(
    """
    import http.server
    import json
    import sys

    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1])
    workdir = args[args.index("--workdir") + 1]
    session_counter = 0

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/global/health":
                self._send(200, {"healthy": True, "version": "stub"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            global session_counter
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if self.path == "/session":
                session_counter += 1
                self._send(200, {"id": f"ses_{session_counter}", "title": "stub"})
            else:
                self._send(404, {"error": "not found"})

        def do_DELETE(self):
            if self.path.startswith("/session/ses_"):
                self._send(200, True)
            else:
                self._send(404, {"error": "not found"})

    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    """
)


def _config(tmp_path) -> RecollectConfig:
    return RecollectConfig(
        embedding_model_path=tmp_path / "embedding.gguf",
        data_dir=tmp_path / "var",
        sandbox_root=tmp_path / "sandboxes",
    )


def _factory_for(script: Path):
    def factory(port: int, workdir: Path, password: str) -> list[str]:
        return [
            sys.executable,
            str(script),
            "--port",
            str(port),
            "--workdir",
            str(workdir),
        ]

    return factory


async def test_warm_server_gets_fresh_sessions_and_scrubbed_scratch(tmp_path):
    script = tmp_path / "stub_opencode.py"
    script.write_text(_STUB, encoding="utf-8")
    manager = SandboxManager(_config(tmp_path), command_factory=_factory_for(script))
    handle = await manager.ensure()
    try:
        assert handle.oc_session_id is None
        assert handle.workdir == tmp_path / "sandboxes" / "shared" / "workspace"
        assert handle.workdir.is_absolute()
        assert (handle.config_dir / "opencode.json").is_file()
        assert list(handle.config_dir.iterdir()) == [
            handle.config_dir / "opencode.json"
        ]
        assert handle.process.returncode is None
        again = await manager.ensure()
        assert again is handle  # no respawn while alive

        (handle.workdir / "stale.txt").write_text("old call", encoding="utf-8")
        first = await manager.begin_invocation("s1")
        assert first.oc_session_id == "ses_1"
        assert first.process_reused is True
        assert not (handle.workdir / "stale.txt").exists()
        (handle.workdir / "notes.md").write_text("secret", encoding="utf-8")
        await manager.finish_invocation(first)
        assert handle.oc_session_id is None
        assert list(handle.workdir.iterdir()) == []

        second = await manager.begin_invocation("s2")
        assert second.oc_session_id == "ses_2"
        assert second.oc_session_id != first.oc_session_id
        await manager.finish_invocation(second)
    finally:
        await manager.teardown()
    assert handle.process.returncode is not None


async def test_dead_handle_is_restarted(tmp_path):
    script = tmp_path / "stub_opencode.py"
    script.write_text(_STUB, encoding="utf-8")
    manager = SandboxManager(_config(tmp_path), command_factory=_factory_for(script))
    handle = await manager.ensure()
    handle.process.kill()
    await handle.process.wait()
    second = await manager.ensure()
    try:
        assert second is not handle
        assert second.oc_session_id is None
        assert second.process.returncode is None
    finally:
        await manager.teardown()


async def test_failed_session_deletion_keeps_server_but_not_context(tmp_path):
    created = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created
        if (request.method, request.url.path) == ("POST", "/session"):
            created += 1
            return httpx.Response(200, json={"id": f"ses_{created}"})
        if (request.method, request.url.path) == ("DELETE", "/session/ses_1"):
            return httpx.Response(500, json={"error": "cannot delete"})
        if (request.method, request.url.path) == ("DELETE", "/session/ses_2"):
            return httpx.Response(200, json=True)
        return httpx.Response(404)

    config = _config(tmp_path)
    manager = SandboxManager(config)
    workdir = Path(config.sandbox_root) / "shared" / "workspace"
    workdir.mkdir(parents=True)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:9",
    )
    handle = SandboxHandle(
        workdir=workdir,
        port=9,
        password="pw",
        process=None,
        client=client,
    )
    manager._handle = handle

    first = await manager.begin_invocation("s1")
    await manager.finish_invocation(first)
    assert manager._handle is handle
    assert handle.busy is False

    second = await manager.begin_invocation("s1")
    assert second.oc_session_id == "ses_2"
    assert second.oc_session_id != first.oc_session_id
    assert second.process_reused is True
    await manager.finish_invocation(second)
    await manager.teardown()


async def test_spawn_surfaces_early_exit(tmp_path):
    manager = SandboxManager(
        _config(tmp_path),
        command_factory=lambda port, workdir, password: [
            sys.executable,
            "-c",
            "import sys; sys.exit(3)",
        ],
    )
    with pytest.raises(SandboxStartError, match="exited early"):
        await manager.ensure()


async def test_reap_shuts_down_idle_sandboxes(tmp_path):
    script = tmp_path / "stub_opencode.py"
    script.write_text(_STUB, encoding="utf-8")
    config = _config(tmp_path)
    manager = SandboxManager(config, command_factory=_factory_for(script))
    handle = await manager.ensure()
    handle.last_used -= config.sandbox_idle_ttl_s + 60.0
    await manager._reap()
    assert manager._handle is None
    await handle.process.wait()
    assert handle.process.returncode is not None


async def test_reap_leaves_busy_and_fresh_sandbox(tmp_path):
    script = tmp_path / "stub_opencode.py"
    script.write_text(_STUB, encoding="utf-8")
    config = _config(tmp_path)
    manager = SandboxManager(config, command_factory=_factory_for(script))
    invocation = await manager.begin_invocation("busy")
    handle = invocation.handle
    handle.last_used -= config.sandbox_idle_ttl_s + 60.0
    await manager._reap()
    try:
        assert manager._handle is handle
        await manager.finish_invocation(invocation)
        assert manager._handle is handle
    finally:
        await manager.teardown()


async def test_different_chats_queue_on_the_shared_server(tmp_path):
    script = tmp_path / "stub_opencode.py"
    script.write_text(_STUB, encoding="utf-8")
    manager = SandboxManager(_config(tmp_path), command_factory=_factory_for(script))

    first = await manager.begin_invocation("chat-a")
    waiting = asyncio.create_task(manager.begin_invocation("chat-b"))
    await asyncio.sleep(0.05)
    assert not waiting.done()

    await manager.finish_invocation(first)
    second = await waiting
    try:
        assert second.handle is first.handle
        assert second.process_reused is True
        assert second.oc_session_id != first.oc_session_id
    finally:
        await manager.finish_invocation(second)
        await manager.teardown()
