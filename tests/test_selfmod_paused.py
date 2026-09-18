"""A paused build's resumable record: the tree, plan, step and frozen contract."""

import json
from pathlib import Path

import pytest

from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.paused import SCHEMA, PausedBuild, clear, load, path_for, save
from recollect.selfmod.tests_first import parse_tests
from tests.test_selfmod_tests_first import authored

BASELINE_SHA = "ab" * 32


def make_pause():
    tree = Snapshot((File("recollect/engine/subagent_tools/post.py",
                          b"def post(): ...\n"),))
    return PausedBuild(
        session_id="sess", task_id="task", request="post it",
        gap={"missing_capability": "POST"}, attempt=2,
        feedback=("attempt 1: boom",),
        plan={"summary": "add the tool",
              "changes": [{"path": "recollect/engine/subagent_tools/post.py",
                           "operation": "create", "reason": "new tool"}]},
        step={"kind": "ask", "question": "which calendar?"},
        tree=tree, tests=parse_tests(authored()), connections="guide text",
        baseline_sha256=BASELINE_SHA)


def test_save_then_load_round_trips_the_whole_pause(tmp_path):
    pause = make_pause()
    save(tmp_path, pause)
    assert load(tmp_path) == pause


def test_the_record_is_json_in_the_root_with_base64_blobs(tmp_path):
    save(tmp_path, make_pause())
    assert path_for(tmp_path) == Path(tmp_path) / "paused_build.json"
    record = json.loads((tmp_path / "paused_build.json")
                        .read_text(encoding="utf-8"))
    assert record["schema"] == SCHEMA
    assert record["attempt"] == 2
    assert record["feedback"] == ["attempt 1: boom"]
    assert record["step"] == {"kind": "ask", "question": "which calendar?"}
    # The tree and contract travel as (path, base64) pairs, not host paths.
    assert all(len(file) == 2 for file in record["tree"])
    assert "interface" in record["tests"] and record["tests"]["unverified"] == []


def test_load_returns_none_when_there_is_no_record(tmp_path):
    assert load(tmp_path) is None


def test_load_refuses_an_unrecognized_record(tmp_path):
    (tmp_path / "paused_build.json").write_text(
        json.dumps({"schema": 999}), encoding="utf-8")
    with pytest.raises(ValueError):
        load(tmp_path)


def test_clear_drops_the_record_and_is_idempotent(tmp_path):
    save(tmp_path, make_pause())
    clear(tmp_path)
    assert load(tmp_path) is None
    clear(tmp_path)  # a second clear is a no-op, not an error


def test_load_rebuilds_the_frozen_contract_and_tree(tmp_path):
    pause = make_pause()
    save(tmp_path, pause)
    loaded = load(tmp_path)
    assert loaded.tree == pause.tree
    assert loaded.tests == pause.tests
    assert loaded.tests.sha256 == pause.tests.sha256
    assert loaded.feedback == ("attempt 1: boom",)
    assert loaded.baseline_sha256 == BASELINE_SHA
