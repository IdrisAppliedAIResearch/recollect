"""Conversational research, optional delivery, and progressive skill disclosure."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from recollect.engine.sandbox.configgen import write_config
from recollect.engine.sandbox.runner import OpenCodeRunner, TaskReport
from recollect.engine.subagent import SubagentResult
from recollect.task_store import TaskStore
from tests import test_task_store as store_tests
from tests import test_tasks as coordinator_tests

environment = coordinator_tests.environment
tasks = store_tests.tasks
workspace = store_tests.workspace


def test_continuous_skills_are_read_only_config_assets_not_prompt_bodies(tmp_path):
    path = write_config(
        tmp_path, base_url="http://local/v1", model="test", api_key="test",
        steps=24, continuous=True, runtime_workdir="/workspace", prompt_dir="/config",
    )
    config = json.loads(path.read_text())
    assert config["skills"] == {"paths": ["/config/skills"]}
    names = {"recollect-reporting", "recollect-files", "recollect-research"}
    for agent in ("build", "general"):
        assert config["agent"][agent]["permission"]["skill"] == {
            "*": "deny", **dict.fromkeys(names, "allow"),
        }
        assert "prompt" not in config["agent"][agent]
    assert {p.parent.name for p in tmp_path.glob("skills/*/SKILL.md")} == names
    prompt = OpenCodeRunner._continuous_prompt("Research a company", 2, "steer-2")
    assert "Instruction revision: 2" in prompt
    assert "Related message ID: steer-2" in prompt
    assert "recollect-reporting" in prompt
    assert "Create files only when the user requested a file" in prompt
    assert "2 MiB" not in prompt
    assert "2 MiB" in (tmp_path / "skills/recollect-files/SKILL.md").read_text()


def test_scratch_checkpoint_does_not_download_without_selected_deliverable(
    tasks, workspace, tmp_path,
):
    store, session, _ = tasks
    downloads = tmp_path / "Downloads"
    store = TaskStore(replace(store.config, downloads_dir=downloads))
    task = store_tests._start(store, session)
    (workspace / "scratch.md").write_text("Working notes")
    (workspace / "requested.csv").write_text("name,value\nA,1\n")
    archived = store.export_workspace(
        session, task["task_id"], workspace, deliver_names=[],
    )
    assert len(archived) == 2
    assert all("download_path" not in item for item in archived)
    assert not downloads.exists()
    # Selecting an already checkpointed file delivers it without another version.
    saved = store.export_workspace(
        session, task["task_id"], workspace, deliver_names=["requested.csv"],
    )
    assert [p.name for p in downloads.iterdir()] == ["requested.csv"]
    assert all(item["version"] == 1 for item in saved)
    assert next(i for i in saved if i["name"] == "requested.csv")["download_path"]
    assert "download_path" not in next(i for i in saved if i["name"] == "scratch.md")


@pytest.mark.parametrize("file_requested,artifact_name", [
    (False, "requested.md"), (True, "requested.md"), (True, "/workspace/requested.md"),
])
async def test_worker_report_reaches_main_reply_and_only_reported_files_download(
    environment, file_requested, artifact_name, tmp_path,
):
    state = environment
    downloads = tmp_path / "Downloads"
    state.store.config = replace(state.store.config, downloads_dir=downloads)
    answer = "Alpha overlaps in health IT; Beta supplies claims software."
    source = "https://example.org/verified"

    async def behavior(**kwargs):
        await kwargs["report"](TaskReport("accepted", "Accepted", 1))
        await kwargs["report"](TaskReport(
            "finding", "Alpha serves federal health programs.", 1, sources=[source],
        ))
        (state.workspace / "scratch.md").write_text("Private working notes")
        if file_requested:
            (state.workspace / "requested.md").write_text(answer)
        await kwargs["report"](TaskReport(
            "result", answer, 1, sources=[source],
            artifacts=[artifact_name] if file_requested else [],
        ))
        await kwargs["save_workspace"](state.workspace)
        yield SubagentResult(
            "research", "ok", "{}", "Done; see the file.",
            sources=["https://example.org/unrelated-search-hit"],
        )

    state.behavior = behavior
    task = await coordinator_tests.submit(state)
    key = (state.session_id, task["task_id"])
    await state.coordinator._execute(task)
    saved = state.store.get(*key)
    assert saved["result"] == answer
    event = state.coordinator._notifications[key]
    await state.coordinator._announce_one(key, event)
    notice = state.store.notifications(state.session_id)[-1]
    assert answer in notice["text"]
    assert "scratch.md" not in notice["text"]
    assert notice["source_refs"] == [source]
    evidence = json.loads(state.generator.calls[-1]["user_message"])
    assert evidence["text"] == answer
    assert evidence["findings"] == ["Alpha serves federal health programs."]
    assert evidence["sources"] == [source]
    if file_requested:
        assert "requested.md" in notice["text"]
        assert (downloads / "requested.md").read_text() == answer
        assert len(list(downloads.iterdir())) == 1
    else:
        assert not downloads.exists()
        assert "/artifacts/" not in notice["text"]
    assert not state.store.config.store_path(state.session_id).exists()


async def test_completion_synthesis_keeps_detail_beyond_old_700_character_cutoff(
    environment, monkeypatch,
):
    state = environment
    task = await coordinator_tests.submit(state)
    key = (state.session_id, task["task_id"])
    answer = "Detailed finding. " * 70 + "Final specific comparison."

    async def stream(messages, *, trace, max_tokens):
        assert max_tokens == 1024
        trace.response_text = answer
        yield None

    monkeypatch.setattr(state.generator, "stream", stream)
    state.coordinator._queue_update(key, "result", "result", answer, 1)
    await state.coordinator._announce_one(key, state.coordinator._notifications[key])
    assert state.store.notifications(state.session_id)[0]["text"] == answer


async def test_failed_synthesis_preserves_available_findings(environment, monkeypatch):
    state = environment
    task = await coordinator_tests.submit(state)
    key = (state.session_id, task["task_id"])
    answer = "Supported finding. " * 150 + "A final caveat."

    async def stream(*args, **kwargs):
        raise ValueError("Model unavailable")
        yield

    monkeypatch.setattr(state.generator, "stream", stream)
    state.coordinator._queue_update(key, "result", "result", answer, 1)
    await state.coordinator._announce_one(key, state.coordinator._notifications[key])
    assert answer in state.store.notifications(state.session_id)[0]["text"]
    assert not Path(state.store.config.store_path(state.session_id)).exists()
