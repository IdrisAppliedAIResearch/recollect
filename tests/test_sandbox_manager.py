"""The sandbox manager against a stub opencode server.

The stub is a plain stdlib HTTP server that speaks just enough of the
opencode API (health + session list/create) for the manager's lifecycle
code. The real opencode binary is never touched here.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from recollect.config import RecollectConfig
from recollect.engine.sandbox.manager import (
    SandboxManager,
    SandboxStartError,
)

_STUB = textwrap.dedent(
    """
    import http.server
    import json
    import os
    import sys

    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1])
    workdir = args[args.index("--workdir") + 1]
    session = "recollect:" + os.path.basename(workdir.rstrip("/\\\\"))

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
            elif self.path == "/session":
                # A stored session for this sandbox's own session, plus a
                # stale one from another project that must not be picked.
                self._send(200, [
                    {
                        "id": "ses_other",
                        "title": session,
                        "directory": "/elsewhere",
                        "time": {"updated": 999},
                    },
                    {
                        "id": "ses_stub",
                        "title": session,
                        "directory": workdir,
                        "time": {"updated": 1},
                    },
                ])
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if self.path == "/session":
                self._send(200, {"id": "ses_new", "title": "stub"})
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


async def test_ensure_spawns_once_and_reattaches(tmp_path):
    script = tmp_path / "stub_opencode.py"
    script.write_text(_STUB, encoding="utf-8")
    manager = SandboxManager(_config(tmp_path), command_factory=_factory_for(script))
    handle = await manager.ensure("s1")
    try:
        assert handle.oc_session_id == "ses_stub"
        assert handle.workdir == tmp_path / "sandboxes" / "s1"
        # The workdir path opencode will store with the session must be
        # absolute, or re-attachment can never match it.
        assert handle.workdir.is_absolute()
        assert (handle.workdir / "opencode.json").is_file()
        assert (handle.workdir / "researcher.md").is_file()
        assert handle.process.returncode is None
        again = await manager.ensure("s1")
        assert again is handle  # no respawn while alive
    finally:
        await manager.teardown("s1")
    assert handle.process.returncode is not None


async def test_dead_handle_is_restarted(tmp_path):
    script = tmp_path / "stub_opencode.py"
    script.write_text(_STUB, encoding="utf-8")
    manager = SandboxManager(_config(tmp_path), command_factory=_factory_for(script))
    handle = await manager.ensure("s1")
    handle.process.kill()
    await handle.process.wait()
    second = await manager.ensure("s1")
    try:
        assert second is not handle
        assert second.oc_session_id == "ses_stub"
        assert second.process.returncode is None
    finally:
        await manager.teardown("s1")


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
        await manager.ensure("s2")


async def test_reap_shuts_down_idle_sandboxes(tmp_path):
    script = tmp_path / "stub_opencode.py"
    script.write_text(_STUB, encoding="utf-8")
    config = _config(tmp_path)
    manager = SandboxManager(config, command_factory=_factory_for(script))
    handle = await manager.ensure("s3")
    handle.last_used -= config.sandbox_idle_ttl_s + 60.0
    await manager._reap()
    assert "s3" not in manager._handles
    await handle.process.wait()
    assert handle.process.returncode is not None


async def test_reap_leaves_busy_and_fresh_sandboxes(tmp_path):
    script = tmp_path / "stub_opencode.py"
    script.write_text(_STUB, encoding="utf-8")
    config = _config(tmp_path)
    manager = SandboxManager(config, command_factory=_factory_for(script))
    busy = await manager.ensure("busy")
    await manager.ensure("fresh")
    busy.busy = True
    busy.last_used -= config.sandbox_idle_ttl_s + 60.0
    await manager._reap()
    try:
        assert "busy" in manager._handles
        assert "fresh" in manager._handles
    finally:
        await manager.teardown("busy")
        await manager.teardown("fresh")
