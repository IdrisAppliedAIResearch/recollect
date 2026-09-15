"""Freeze/trial commands: identities, manifest assembly, start-failure accounting."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from recollect.selfmod import trial_cli, trial_live, trial_manifest
from recollect.selfmod.candidate_evaluator import EVALUATION_CHECKS
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.google_auth import SCOPES
from recollect.selfmod.journal import IntegrityError

REPO = Path(__file__).resolve().parents[1]
BASE = "sha256:" + "a" * 64
CALENDAR = {"calendar_id": "private@group.calendar.google.com",
            "alias": "selfmod-test", "time_zone": "America/Chicago"}


def inputs():
    return {
        "attempt_id": "primary-1",
        "experiment": {"id": "selfmod-2026-09", "event_date": "2026-09-22",
                       "dedup_id": "v0selfmod20260901",
                       "workload_request": "Summarize recent llama.cpp releases.",
                       "probe": {"prompt": "What is 17 plus 25?", "answer": "42"}},
        "lane_probes": [{"lane": "conversation", "slot": 0, "prompt": "p",
                         "expected": "e"}],
        "native": {"image_id": BASE, "image_environment": [], "binary_sha256": "b" * 64,
                   "context_limit": 43776, "output_limit": 4096},
    }


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "credentials"
    root.mkdir()
    for role, scope in SCOPES.items():
        (root / f"{role}.json").write_text(json.dumps({"role": role, "scope": scope}))
    return root


async def identities(tmp_path, store):
    weight = tmp_path / "model.gguf"
    weight.write_bytes(b"weights")
    props = {"total_slots": 3, "default_generation_settings": {"n_ctx": 131072}}
    return await trial_manifest.collect_identities(
        REPO, base_url="http://127.0.0.1:8001/v1", model="model.gguf",
        weight_path=weight, base_image_id=BASE, calendar=CALENDAR,
        credential_store=store,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=props)))


async def test_collected_identities_cover_every_frozen_section(tmp_path, store):
    value = await identities(tmp_path, store)
    assert value["model"]["total_slots"] == 3 and value["model"]["n_ctx"] == 131072
    assert value["model"]["weight_sha256"] == trial_manifest.file_sha256(
        tmp_path / "model.gguf")
    assert value["calendar"] == {**CALENDAR, "scopes": SCOPES}
    assert value["subagent"]["base_image_id"] == BASE
    assert set(value["registrations"]) == set(trial_manifest.REGISTRATION_DOCUMENTS)


async def test_freeze_assembles_a_loadable_manifest_only_from_clean_source(
    tmp_path, store,
):
    value = await identities(tmp_path, store)
    with pytest.raises(IntegrityError, match="clean"):
        trial_manifest.freeze({**value, "source": {**value["source"],
                                                   "clean": False}}, inputs())
    clean = {**value, "source": {**value["source"], "clean": True}}
    path = tmp_path / "manifest.json"
    path.write_bytes(trial_manifest.freeze(clean, inputs()))
    manifest = trial_manifest.RuntimeManifest.load(path)
    assert manifest.value["evaluator"]["checks"] == list(EVALUATION_CHECKS)
    assert manifest.value["experiment"]["request"] == trial_manifest.render_request(
        "selfmod-2026-09", "2026-09-22")
    config = trial_cli.controller_config(manifest, REPO)
    assert dict(config.registrations)["runtime"] == manifest.sha256
    assert config.contract.original_request == manifest.value["experiment"]["request"]
    assert config.evaluation_checks == EVALUATION_CHECKS
    # CP0 compares the recomputed model block without the frozen probe inputs.
    assert trial_manifest.compare(
        {k: v for k, v in manifest.value["model"].items() if k != "lane_probes"},
        value["model"]) == []


async def test_environment_start_failure_is_recorded_and_accounted(
    tmp_path, store, monkeypatch,
):
    value = await identities(tmp_path, store)
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(trial_manifest.freeze(
        {**value, "source": {**value["source"], "clean": True}}, inputs()))
    monkeypatch.setenv("RECOLLECT_EMBEDDING_MODEL_PATH", str(tmp_path / "e.gguf"))
    monkeypatch.setattr(trial_cli, "pinned_docker", lambda root: object())

    class Failing:
        def __init__(self, settings):
            self.settings = settings

        async def start(self):
            raise RuntimeError("docker engine unavailable")

        async def finish(self, failure):
            self.failure = failure
            return Snapshot((File("finish.json", b"{}"),))

    monkeypatch.setattr(trial_live, "LiveTrialEnvironment", Failing)
    cp6, controller = await trial_cli.run_trial(
        manifest, tmp_path / "attempt", REPO, store)
    observed = controller._checkpoints[-1].value["observations"]
    assert observed["result"] == "primary_failed"
    assert "environment_start_failed:RuntimeError" in observed["reasons"]
    assert observed["unreached"] == ["CP0", "CP1", "CP2", "CP3", "CP4", "CP5"]
    with pytest.raises(FileExistsError):
        await trial_cli.run_trial(manifest, tmp_path / "attempt", REPO, store)


def test_rejected_feedback_is_results_only():
    evaluation = type("E", (), {})()
    evaluation.checks = {"creates_exact_event": False}
    evaluation.evidence = Snapshot((
        File("scenarios/creates_exact_event/result.json", b'{"passed": false}'),
        File("scenarios/creates_exact_event/steps.json", b"[]"),
        File("probe.json", b"{}")))
    assert trial_live._feedback(evaluation) == {
        "checks": {"creates_exact_event": False},
        "scenarios": {"creates_exact_event": {"passed": False}}}
    assert trial_live._feedback(None) is None


def test_cli_exposes_both_commands(capsys):
    from recollect import cli

    with pytest.raises(SystemExit):
        cli.main(["selfmod-trial", "--help"])
    assert "--attempt-root" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["selfmod-freeze", "--help"])
    assert "--inputs" in capsys.readouterr().out


def test_asyncio_is_importable_for_trial_entrypoints():
    assert asyncio.iscoroutinefunction(trial_cli.run_trial)
