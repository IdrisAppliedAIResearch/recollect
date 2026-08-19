"""The generated opencode config: the invariants it must encode."""

from __future__ import annotations

import json
import re
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
    assert config["default_agent"] == "researcher"
    assert config["subagent_depth"] == 1
    assert config["autoupdate"] is False
    assert config["share"] == "disabled"
    assert config["snapshot"] is False

    provider = config["provider"]["recollect"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    options = provider["options"]
    assert options["baseURL"] == "http://127.0.0.1:8000/v1"
    # No request-level timeout: a queued request on the single-slot
    # server must not be misread as a dead one. The chunk timeout is the
    # liveness net, and it must outlast a queue wait.
    assert options["timeout"] is False
    assert options["chunkTimeout"] > 0
    assert set(provider["models"]) == {"local"}


def test_permissions_gate_the_sandbox(tmp_path):
    _, config = _generate(tmp_path)
    for name in ("build", "plan", "general", "explore", "scout"):
        assert config["agent"][name] == {"disable": True}

    researcher = config["agent"]["researcher"]
    assert researcher["mode"] == "primary"
    assert researcher["steps"] == 24
    assert researcher["prompt"] == "{file:researcher.md}"
    assert researcher["temperature"] == 0.7
    perm = researcher["permission"]
    # Deny by default: opencode allows any permission it was not told
    # about, and plugins from higher-scope configs can register tools
    # this table never names.
    assert list(perm)[0] == "*"
    assert perm["*"] == "deny"
    assert perm["bash"] == "deny"
    assert perm["webfetch"] == "deny"
    assert perm["websearch"] == "deny"
    assert perm["external_directory"] == "deny"
    assert perm["question"] == "deny"
    assert perm["skill"] == "deny"
    assert perm["read"] == "allow"
    assert perm["edit"] == "allow"
    assert perm["doom_loop"] == "allow"
    assert perm["recollect_research_*"] == "allow"
    # Task rules match in order, last match wins: the allow must come
    # after the deny-all, or it would be dead.
    task_rules = perm["task"]
    assert list(task_rules) == ["*", "researcher-sub"]
    assert task_rules["*"] == "deny"
    assert task_rules["researcher-sub"] == "allow"

    sub = config["agent"]["researcher-sub"]
    assert sub["mode"] == "subagent"
    assert sub["hidden"] is True
    assert sub["permission"]["task"] == "deny"


def test_mcp_wraps_the_recollect_tools_with_the_running_interpreter(tmp_path):
    workdir, config = _generate(tmp_path)
    mcp = config["mcp"][configgen.MCP_SERVER]
    assert mcp["type"] == "local"
    assert mcp["command"] == [sys.executable, "-m", "recollect.engine.mcp_research"]
    assert mcp["cwd"] == str(workdir)
    # A search fans out over six providers; the 5s MCP default is far
    # too tight for that.
    assert mcp["timeout"] == 120_000
    assert mcp["enabled"] is True


def test_promise_karries_the_final_answer_contract(tmp_path):
    workdir, _ = _generate(tmp_path)
    prompt = (workdir / "researcher.md").read_text(encoding="utf-8")
    prompt = re.sub(r"\s+", " ", prompt)  # the source wraps at ~72 cols
    for fragment in (
        "recollect_research_web_search",
        "recollect_research_web_fetch",
        '"summary"',
        '"findings"',
        '"sources"',
        "untrusted",
        "notes.md",
        "You have no shell",
        # The JSON receipt is unconditional - a prose refusal would
        # otherwise ship as an unparseable "partial".
        "No matter the outcome",
        "exactly one fenced JSON block",
    ):
        assert fragment in prompt


def test_finalizer_agent_has_no_tools_at_all(tmp_path):
    _, config = _generate(tmp_path)
    finalizer = config["agent"][configgen.FINALIZER_NAME]
    assert finalizer["mode"] == "primary"
    assert finalizer["prompt"] == "{file:finalizer.md}"
    # The whole point: opencode has no per-message "tools off" flag, but
    # `ToolRegistry.materialize` deletes every tool whose last matching
    # rule is resource "*" effect deny, MCP tools included - so this bare
    # table is the runner's equivalent of the legacy backend's
    # `tools=None`. Anything allowed after it would put a tool back.
    assert finalizer["permission"] == {"*": "deny"}
    # Same model settings as the researcher: the tool surface is meant to
    # be the only difference between the two passes.
    assert finalizer["temperature"] == config["agent"]["researcher"]["temperature"]


def test_the_finalizer_is_not_starved_of_its_one_working_turn(tmp_path):
    """The step budget that made the finalize pass a no-op on first ship.

    opencode numbers a turn's steps from 1 and, on the step where
    `step >= agent.steps`, it materializes no tools, appends its own
    "MAXIMUM STEPS REACHED ... Respond with text only" message to the
    request and sets toolChoice "none". So the `steps`-th turn is the
    forced wrap-up, not a working one. At `steps: 1` the finalizer's only
    turn is that wrap-up: it recites the banner, `_parse_final` refuses
    it, and the pass changes nothing - which is what a live capped run
    showed. Two gives it one real turn, with the wrap-up as a backstop it
    should never reach, since opencode only continues a turn after a tool
    call and this agent has no tools to call.
    """
    _, config = _generate(tmp_path)
    assert config["agent"][configgen.FINALIZER_NAME]["steps"] >= 2


def test_finalizer_prompt_does_not_restate_the_receipt_contract(tmp_path):
    workdir, _ = _generate(tmp_path)
    prompt = (workdir / "finalizer.md").read_text(encoding="utf-8")
    collapsed = re.sub(r"\s+", " ", prompt)
    assert "no tools at all" in collapsed
    assert "read by a program" in collapsed
    # The contract itself is `subagent._FINALIZE_PARTIAL`, sent as the
    # message. A second copy here could drift from the legacy one, and
    # then the two backends would be asking for different things.
    for fragment in ('"summary"', '"findings"', '"sources"', "fenced JSON"):
        assert fragment not in prompt
