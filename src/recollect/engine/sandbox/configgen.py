"""Generate the isolated OpenCode configuration.

The configuration deliberately does not replace OpenCode's native system
prompts. It selects the built-in ``build`` agent and keeps its built-in
``general`` worker available, while permission rules expose only the
container workspace and Recollect's SSRF-guarded research MCP tools. The
container remains the security boundary; these rules are defense in depth.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROVIDER_ID = "recollect"
AGENT_NAME = "build"
SUBAGENT_NAME = "general"
MCP_SERVER = "recollect_research"

_CONTEXT_LIMIT = 200_000
_OUTPUT_LIMIT = 32_768
_CHUNK_TIMEOUT_MS = 600_000


def _permission_table(*, subagent: bool) -> dict:
    """Deny unknown/plugin tools; a worker cannot delegate recursively."""
    task_rule: dict | str = (
        "deny" if subagent else {"*": "deny", SUBAGENT_NAME: "allow"}
    )
    return {
        "*": "deny",
        "read": "allow",
        "edit": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "todowrite": "allow",
        "doom_loop": "allow",
        "bash": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "external_directory": "deny",
        "question": "deny",
        "skill": "deny",
        "lsp": "deny",
        "task": task_rule,
        f"{MCP_SERVER}_*": "allow",
    }


def build_config(
    workdir: Path,
    *,
    base_url: str,
    model: str,
    api_key: str,
    steps: int,
    runtime_workdir: str | None = None,
    runtime_python: str | None = None,
    prompt_dir: str | None = None,
) -> dict:
    """Return a local-provider-only config using native OpenCode agents."""
    del prompt_dir  # retained for call-site compatibility; no prompt files exist
    model_ref = f"{PROVIDER_ID}/{model}"
    runtime_workdir = runtime_workdir or str(workdir)
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model_ref,
        "small_model": model_ref,
        "enabled_providers": [PROVIDER_ID],
        "default_agent": AGENT_NAME,
        "subagent_depth": 1,
        "plugin": [],
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
        "formatter": False,
        "lsp": False,
        "compaction": {"auto": True, "prune": True, "reserved": 8000},
        "provider": {
            PROVIDER_ID: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Recollect local model",
                "options": {
                    "baseURL": base_url,
                    "apiKey": api_key,
                    "timeout": False,
                    "chunkTimeout": _CHUNK_TIMEOUT_MS,
                },
                "models": {
                    model: {
                        "name": "Recollect local model",
                        "limit": {
                            "context": _CONTEXT_LIMIT,
                            "output": _OUTPUT_LIMIT,
                        },
                    }
                },
            }
        },
        "agent": {
            # Supplying only limits and permissions preserves the built-in
            # prompts and modes. This is the same native build -> general
            # workflow used by the transfer-wording benchmark.
            AGENT_NAME: {
                "steps": steps,
                "permission": _permission_table(subagent=False),
            },
            SUBAGENT_NAME: {
                "steps": steps,
                "permission": _permission_table(subagent=True),
            },
            "plan": {"disable": True},
            "explore": {"disable": True},
            "scout": {"disable": True},
        },
        "mcp": {
            MCP_SERVER: {
                "type": "local",
                "command": [
                    runtime_python or sys.executable,
                    "-m",
                    "recollect.engine.mcp_research",
                ],
                "cwd": runtime_workdir,
                "timeout": 120_000,
                "enabled": True,
            }
        },
    }


def write_config(
    workdir: Path,
    *,
    base_url: str,
    model: str,
    api_key: str,
    steps: int,
    runtime_workdir: str | None = None,
    runtime_python: str | None = None,
    prompt_dir: str | None = None,
) -> Path:
    """Write the only host file mounted read-only into the container."""
    workdir.mkdir(parents=True, exist_ok=True)
    config = build_config(
        workdir,
        base_url=base_url,
        model=model,
        api_key=api_key,
        steps=steps,
        runtime_workdir=runtime_workdir,
        runtime_python=runtime_python,
        prompt_dir=prompt_dir,
    )
    config_path = workdir / "opencode.json"
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return config_path
