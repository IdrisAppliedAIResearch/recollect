"""Generate the isolated OpenCode configuration.

The configuration deliberately does not replace OpenCode's native system
prompts. It selects the built-in ``build`` agent and keeps its built-in
``general`` worker available, while permission rules expose only the
container workspace and Recollect's SSRF-guarded research MCP tools. The
container remains the security boundary; these rules are defense in depth.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

PROVIDER_ID = "recollect"
AGENT_NAME = "build"
SUBAGENT_NAME = "general"
MCP_SERVER = "recollect_research"

_CONTEXT_LIMIT = 200_000
_OUTPUT_LIMIT = 32_768
_CHUNK_TIMEOUT_MS = 600_000
#: The pinned binary always applies this per-request MCP timeout; a bundle's
#: tool host keeps healthy calls alive with progress.
MCP_TIMEOUT_MS = 120_000
TOOL_HOST_MODULE = "recollect.engine.toolhost"


def _permission_table(*, subagent: bool, continuous: bool = False) -> dict:
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
        "skill": ({"*": "deny", "recollect-reporting": "allow",
                   "recollect-files": "allow", "recollect-research": "allow"}
                  if continuous else "deny"),
        "lsp": "deny",
        "task": task_rule,
        f"{MCP_SERVER}_*": "allow",
    }


def build_development_config(
    *, base_url: str, model: str, api_key: str, context_limit: int, output_limit: int
) -> dict:
    """Stock OpenCode for self-modification agents, as a person would run it.

    Every native tool is allowed, bash and the network included, with no step
    limit and no Recollect MCP or skills. The container is the boundary: the
    workspace holds only the tree under development, and no credentials.
    """
    if context_limit <= output_limit or output_limit < 1:
        raise ValueError("OpenCode context must leave room beyond the output reserve")
    model_ref = f"{PROVIDER_ID}/{model}"
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model_ref,
        "small_model": model_ref,
        "enabled_providers": [PROVIDER_ID],
        "default_agent": AGENT_NAME,
        "plugin": [],
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
        "compaction": {"auto": True, "prune": True, "reserved": 8000},
        "provider": {
            PROVIDER_ID: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Recollect local model",
                "options": {"baseURL": base_url, "apiKey": api_key, "timeout": False},
                "models": {model: {"name": "Recollect local model", "limit": {
                    "context": context_limit, "output": output_limit}}},
            }
        },
        # Nobody answers questions and the workspace is the only directory.
        "permission": {"*": "allow", "question": "deny",
                       "external_directory": "deny"},
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
    context_limit: int = _CONTEXT_LIMIT,
    output_limit: int = _OUTPUT_LIMIT,
    continuous: bool = False,
    unbounded: bool = False,
    bundle_pythonpath: str | None = None,
    connections: tuple[str, str] | None = None,
) -> dict:
    """Return a local-provider-only config using native OpenCode agents.

    ``bundle_pythonpath`` serves a verified deployment bundle: its harness-owned
    tool host runs the bundle's own tool server, and the base image's packaged
    copy is left off the import path. ``connections`` is the connected-account
    service URL and key, given only to the tool process's environment.
    """
    skill_root = (prompt_dir or str(workdir)).rstrip("/\\") + "/skills"
    environment = {"RECOLLECT_TASK_REPORTING": "1"} if continuous else {}
    module = "recollect.engine.mcp_research"
    if bundle_pythonpath is not None:
        environment["PYTHONPATH"] = bundle_pythonpath
        module = TOOL_HOST_MODULE
    if connections is not None:
        environment["RECOLLECT_CONNECTIONS_URL"] = connections[0]
        environment["RECOLLECT_CONNECTIONS_TOKEN"] = connections[1]
    model_ref = f"{PROVIDER_ID}/{model}"
    runtime_workdir = runtime_workdir or str(workdir)
    if context_limit <= output_limit or output_limit < 1:
        raise ValueError("OpenCode context must leave room beyond the output reserve")
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model_ref,
        "small_model": model_ref,
        "enabled_providers": [PROVIDER_ID],
        "default_agent": AGENT_NAME,
        "subagent_depth": 1,
        "plugin": [],
        **({"skills": {"paths": [skill_root]}} if continuous else {}),
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
                    # The pinned binary arms its chunk timer only for a positive
                    # value; omission is the qualified unbounded setting.
                    **({} if unbounded else {"chunkTimeout": _CHUNK_TIMEOUT_MS}),
                },
                "models": {
                    model: {
                        "name": "Recollect local model",
                        "limit": {
                            "context": context_limit,
                            "output": output_limit,
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
                "permission": _permission_table(subagent=False, continuous=continuous),
            },
            SUBAGENT_NAME: {
                "steps": steps,
                "permission": _permission_table(subagent=True, continuous=continuous),
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
                    module,
                ],
                "cwd": runtime_workdir,
                "timeout": MCP_TIMEOUT_MS,
                "enabled": True,
                **({"environment": environment} if environment else {}),
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
    context_limit: int = _CONTEXT_LIMIT,
    output_limit: int = _OUTPUT_LIMIT,
    continuous: bool = False,
    skills_source: Path | None = None,
    unbounded: bool = False,
    bundle_pythonpath: str | None = None,
    development: bool = False,
    connections: tuple[str, str] | None = None,
) -> Path:
    """Write configuration and bundled skills into the read-only config mount.

    ``skills_source`` selects a verified deployment bundle's skills tree; the
    default remains the skills shipped with this package.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    config_path = workdir / "opencode.json"
    if development:
        config = build_development_config(
            base_url=base_url, model=model, api_key=api_key,
            context_limit=context_limit, output_limit=output_limit)
        config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return config_path
    if continuous:
        shutil.copytree(
            skills_source or Path(__file__).with_name("skills"), workdir / "skills",
            dirs_exist_ok=True,
        )
    config = build_config(
        workdir,
        base_url=base_url,
        model=model,
        api_key=api_key,
        steps=steps,
        runtime_workdir=runtime_workdir,
        runtime_python=runtime_python,
        prompt_dir=prompt_dir,
        context_limit=context_limit,
        output_limit=output_limit,
        continuous=continuous,
        unbounded=unbounded,
        bundle_pythonpath=bundle_pythonpath,
        connections=connections,
    )
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return config_path
