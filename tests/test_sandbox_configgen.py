"""The generated OpenCode config: the invariants it must encode."""

from __future__ import annotations

import json
import sys

from recollect.engine.sandbox import configgen


def _generate(tmp_path) -> tuple:
    workdir = tmp_path / "s1"
    path = configgen.write_config(
        workdir,
        base_url="http://127.0.0.1:8000/v1",
        model="local",
        api_key="not-needed",
        steps=24,
    )
    config = json.loads(path.read_text(encoding="utf-8"))
    return workdir, config


def test_local_provider_is_the_only_enabled_one(tmp_path):
    _, config = _generate(tmp_path)
    assert config["model"] == "recollect/local"
    assert config["small_model"] == "recollect/local"
    assert config["enabled_providers"] == ["recollect"]
    assert config["default_agent"] == "build"
    assert config["subagent_depth"] == 1
    assert config["autoupdate"] is False
    assert config["share"] == "disabled"
    assert config["snapshot"] is False
    assert config["compaction"]["auto"] is True

    provider = config["provider"]["recollect"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    options = provider["options"]
    assert options["baseURL"] == "http://127.0.0.1:8000/v1"
    assert options["timeout"] is False
    assert options["chunkTimeout"] > 0
    assert set(provider["models"]) == {"local"}


def test_native_agents_are_preserved_with_defense_in_depth_permissions(tmp_path):
    _, config = _generate(tmp_path)
    assert config["agent"]["plan"] == {"disable": True}
    assert config["agent"]["explore"] == {"disable": True}
    assert config["agent"]["scout"] == {"disable": True}

    build = config["agent"][configgen.AGENT_NAME]
    general = config["agent"][configgen.SUBAGENT_NAME]
    for agent in (build, general):
        assert agent["steps"] == 24
        assert "prompt" not in agent
        assert "mode" not in agent
        permission = agent["permission"]
        assert list(permission)[0] == "*"
        assert permission["*"] == "deny"
        assert permission["bash"] == "deny"
        assert permission["webfetch"] == "deny"
        assert permission["websearch"] == "deny"
        assert permission["external_directory"] == "deny"
        assert permission["question"] == "deny"
        assert permission["skill"] == "deny"
        assert permission["read"] == "allow"
        assert permission["edit"] == "allow"
        assert permission["doom_loop"] == "allow"
        assert permission["recollect_research_*"] == "allow"

    assert build["permission"]["task"] == {"*": "deny", "general": "allow"}
    assert general["permission"]["task"] == "deny"


def test_mcp_wraps_the_recollect_tools_with_the_running_interpreter(tmp_path):
    workdir, config = _generate(tmp_path)
    mcp = config["mcp"][configgen.MCP_SERVER]
    assert mcp["type"] == "local"
    assert mcp["command"] == [sys.executable, "-m", "recollect.engine.mcp_research"]
    assert mcp["cwd"] == str(workdir)
    assert mcp["timeout"] == 120_000
    assert mcp["enabled"] is True


def test_container_paths_are_written_without_prompt_overrides(tmp_path):
    workdir = tmp_path / "config"
    path = configgen.write_config(
        workdir,
        base_url="http://host.docker.internal:8000/v1",
        model="local",
        api_key="not-needed",
        steps=24,
        runtime_workdir="/workspace",
        prompt_dir="/config",
    )
    config = json.loads(path.read_text(encoding="utf-8"))

    assert config["provider"]["recollect"]["options"]["baseURL"] == (
        "http://host.docker.internal:8000/v1"
    )
    assert config["mcp"][configgen.MCP_SERVER]["cwd"] == "/workspace"
    assert sorted(path.parent.iterdir()) == [path]
