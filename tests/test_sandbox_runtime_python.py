"""MCP launches with the interpreter inside its execution environment."""

from __future__ import annotations

import json
import sys

import pytest

from recollect.config import RecollectConfig
from recollect.engine.sandbox import configgen
from recollect.engine.sandbox.manager import SandboxManager


@pytest.mark.parametrize("write", [False, True])
def test_container_interpreter_overrides_windows_host_in_generated_config(
    tmp_path, monkeypatch, write,
):
    monkeypatch.setattr(
        configgen.sys, "executable", r"C:\host\.venv\Scripts\python.exe",
    )
    generate = configgen.write_config if write else configgen.build_config
    result = generate(
        tmp_path / "config",
        base_url="http://host.docker.internal:8000/v1",
        model="local",
        api_key="not-needed",
        steps=24,
        runtime_workdir="/workspace",
        runtime_python="/usr/local/bin/python",
    )
    config = json.loads(result.read_text(encoding="utf-8")) if write else result
    mcp = config["mcp"][configgen.MCP_SERVER]
    assert mcp["command"] == [
        "/usr/local/bin/python", "-m", "recollect.engine.mcp_research",
    ]
    assert mcp["cwd"] == "/workspace"
    assert sys.executable not in mcp["command"]


@pytest.mark.parametrize("container", [False, True])
async def test_manager_selects_container_python_only_for_production_launch(
    tmp_path, monkeypatch, container,
):
    cfg = RecollectConfig(
        embedding_model_path=tmp_path / "unused.gguf",
        sandbox_root=tmp_path / "sandboxes",
    )
    factory = None if container else lambda *args: pytest.fail("Must not launch a stub")
    manager = SandboxManager(cfg, command_factory=factory)
    generated = []
    original = configgen.write_config

    class ConfigCaptured(Exception):
        pass

    def capture(*args, **kwargs):
        path = original(*args, **kwargs)
        generated.append(json.loads(path.read_text(encoding="utf-8")))
        raise ConfigCaptured

    async def available():
        pass

    monkeypatch.setattr(manager, "_check_container_runtime", available)
    monkeypatch.setattr(configgen, "write_config", capture)
    with pytest.raises(ConfigCaptured):
        await manager.ensure()

    assert len(generated) == 1
    mcp = generated[0]["mcp"][configgen.MCP_SERVER]
    expected_python = "/usr/local/bin/python" if container else sys.executable
    assert mcp["command"] == [
        expected_python, "-m", "recollect.engine.mcp_research",
    ]
    expected_cwd = "/workspace" if container else str(
        tmp_path / "sandboxes" / "shared" / "workspace"
    )
    assert mcp["cwd"] == expected_cwd
