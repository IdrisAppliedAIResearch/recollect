"""A's served subagent tree, the modifier scope and the bundle tool-host launch."""

import os
import subprocess
import sys
from pathlib import Path

from recollect.engine.sandbox import configgen
from recollect.engine.sandbox.manager import SandboxDeployment
from recollect.selfmod import subagent_tree
from recollect.selfmod.deployment import SubagentBundle
from recollect.selfmod.files import materialize

REPO = Path(__file__).resolve().parents[1]
BASE = "sha256:" + "a" * 64


def test_baseline_reads_exact_repository_bytes_with_empty_package_markers():
    tree = subagent_tree.baseline(REPO)
    files = {f.path: f.content for f in tree.files}
    for path, source in subagent_tree.REPOSITORY_FILES:
        assert files[path] == (REPO / source).read_bytes()
    for marker in subagent_tree.PACKAGE_MARKERS:
        assert files[marker] == b""
    skills = REPO / subagent_tree.SKILLS_SOURCE
    assert {p for p in files if p.startswith("skills/")} == {
        "skills/" + p.relative_to(skills).as_posix()
        for p in skills.rglob("*") if p.is_file()}
    assert "skills/recollect-reporting/SKILL.md" in files
    assert subagent_tree.baseline(REPO).sha256 == tree.sha256


def test_modifier_scope_protects_the_tool_host_and_never_deletes():
    tree = subagent_tree.baseline(REPO)
    policy = subagent_tree.change_policy(tree)
    assert policy.baseline_sha256 == tree.sha256
    for path in subagent_tree.PROTECTED:
        assert not policy.permits(path, "modify")
    assert policy.permits("recollect/engine/mcp_research.py", "modify")
    assert policy.permits("dependencies.lock", "modify")
    assert policy.permits("skills/recollect-reporting/SKILL.md", "modify")
    assert policy.permits("recollect/engine/subagent_tools/new_tool.py", "create")
    assert not policy.permits("skills/new-skill/SKILL.md", "create")
    assert policy.permits("recollect/engine/subagent_tools/new_tool.py", "create")
    assert not policy.permits("recollect/engine/new_top_level.py", "create")
    assert not policy.permits("recollect/engine/mcp_research.py", "delete")


def test_tree_is_a_valid_bundle_with_pinned_tool_host_launch():
    tree = subagent_tree.baseline(REPO)
    bundle = SubagentBundle(tree, BASE, subagent_tree.LAUNCH)
    assert bundle.manifest["launch"] == {
        "python_path": subagent_tree.BUNDLE_PYTHONPATH,
        "tool_host": subagent_tree.TOOL_HOST_MODULE}
    assert bundle.digest == SubagentBundle(tree, BASE, subagent_tree.LAUNCH).digest


def test_bundle_deployment_launches_its_tool_host_on_the_bundle_path(tmp_path):
    common = dict(base_url="http://m/v1", model="m", api_key="k", steps=4,
                  continuous=True, runtime_python="/usr/local/bin/python")
    pinned = configgen.build_config(tmp_path, **common,
                                    bundle_pythonpath=subagent_tree.BUNDLE_PYTHONPATH)
    server = pinned["mcp"][configgen.MCP_SERVER]
    assert server["command"] == ["/usr/local/bin/python", "-m",
                                 subagent_tree.TOOL_HOST_MODULE]
    assert server["environment"] == {"RECOLLECT_TASK_REPORTING": "1",
                                     "PYTHONPATH": subagent_tree.BUNDLE_PYTHONPATH}
    legacy = configgen.build_config(tmp_path, **common)["mcp"][configgen.MCP_SERVER]
    assert legacy["command"][-1] == "recollect.engine.mcp_research"
    assert legacy["environment"] == {"RECOLLECT_TASK_REPORTING": "1"}
    deployment = SandboxDeployment("sha256:" + "b" * 64, tmp_path, tmp_path,
                                   subagent_tree.BUNDLE_PYTHONPATH)
    assert deployment.python_path == subagent_tree.BUNDLE_PYTHONPATH


def test_materialized_tree_imports_its_own_tool_server_not_the_host_package(tmp_path):
    root = tmp_path / "bundle"
    materialize(root, subagent_tree.baseline(REPO))
    env = {**os.environ, "PYTHONPATH": str(root)}
    probe = ("import recollect, recollect.engine.toolhost as host, "
             "recollect.engine.mcp_research as server; "
             "print(recollect.__file__); print(host.__file__); print(server.__file__)")
    output = subprocess.run([sys.executable, "-c", probe], env=env, cwd=tmp_path,
                            capture_output=True, text=True, check=True).stdout
    for line in output.splitlines():
        assert Path(line).resolve().is_relative_to(root.resolve())
