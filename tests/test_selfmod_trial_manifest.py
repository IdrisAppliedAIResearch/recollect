"""Runtime manifest schema, registered request, identities and comparison."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from recollect.selfmod import trial_manifest
from recollect.selfmod.journal import IntegrityError

REPO = Path(__file__).resolve().parents[1]
PROTOCOL = (REPO / "docs" / "SELF_MODIFICATION_PREREGISTRATION.md").read_text(
    encoding="utf-8")


def manifest(tmp_path, **changes):
    value = {
        "schema_version": 1, "attempt_id": "primary-1",
        "registrations": dict.fromkeys(trial_manifest.REGISTRATION_DOCUMENTS,
                                       "a" * 64),
        "source": {}, "subagent": {}, "model": {}, "native": {}, "evaluator": {},
        "calendar": {}, "documents": {},
        "experiment": {"id": "selfmod-2026-09", "event_date": "2026-09-22",
                       "request": trial_manifest.render_request(
                           "selfmod-2026-09", "2026-09-22")},
    }
    value.update(changes)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value))
    return path


def test_request_template_is_the_registered_outcome_story_text():
    quoted = " ".join(line.removeprefix("> ").strip() for line in PROTOCOL.splitlines()
                      if line.startswith("> "))
    assert quoted == trial_manifest.REQUEST_TEMPLATE
    text = trial_manifest.render_request("selfmod-2026-09", "2026-09-22")
    assert '"Self-modification review selfmod-2026-09" on 2026-09-22' in text
    for bad in (("Selfmod", "2026-09-22"), ("selfmod-2026-09", "22/09/2026")):
        with pytest.raises(ValueError):
            trial_manifest.render_request(*bad)


def test_registered_event_date_is_next_chicago_day_after_start():
    # 03:00 UTC on 22 September is still 21 September in Chicago.
    assert trial_manifest.registered_event_date(
        datetime(2026, 9, 22, 3, 0, tzinfo=UTC)) == "2026-09-22"
    assert trial_manifest.registered_event_date(
        datetime(2026, 9, 22, 6, 0, tzinfo=UTC)) == "2026-09-23"
    with pytest.raises(ValueError, match="aware"):
        trial_manifest.registered_event_date(datetime(2026, 9, 22))


def test_manifest_load_pins_schema_registrations_and_exact_request(tmp_path):
    loaded = trial_manifest.RuntimeManifest.load(manifest(tmp_path))
    names = dict(loaded.registrations())
    assert names["runtime"] == loaded.sha256
    assert set(names) == {*trial_manifest.REGISTRATION_DOCUMENTS, "runtime"}
    with pytest.raises(IntegrityError, match="template"):
        trial_manifest.RuntimeManifest.load(manifest(tmp_path, experiment={
            "id": "selfmod-2026-09", "event_date": "2026-09-22",
            "request": "Create the event."}))
    with pytest.raises(IntegrityError, match="registration"):
        trial_manifest.RuntimeManifest.load(manifest(tmp_path, registrations={
            "protocol": "a" * 64}))
    with pytest.raises(IntegrityError, match="schema"):
        trial_manifest.RuntimeManifest.load(manifest(tmp_path, extra={}))


def test_documents_and_evaluator_identities_are_exact_file_hashes():
    registrations, documents = trial_manifest.document_identities(REPO)
    receipt = (REPO / "docs" / "SELF_MODIFICATION_PREREGISTRATION.sha256").read_text()
    assert registrations["protocol"] in receipt
    assert set(documents) == set(trial_manifest.FROZEN_DOCUMENTS)
    assert trial_manifest.evaluator_identity(REPO) == (
        trial_manifest.evaluator_identity(REPO))


def test_source_identity_reports_head_and_cleanliness():
    identity = trial_manifest.source_identity(REPO)
    assert len(identity["git_head"]) == 40 and isinstance(identity["clean"], bool)


def test_compare_requires_every_frozen_leaf():
    frozen = {"model": {"slots": 3, "weight": "w"}, "clean": True}
    assert trial_manifest.compare(frozen, {"model": {"slots": 3, "weight": "w",
                                                     "extra": 1}, "clean": True}) == []
    assert trial_manifest.compare(frozen, {"model": {"slots": 1}, "clean": True}) == [
        "/model/slots", "/model/weight"]
    assert trial_manifest.compare(frozen, {"model": "gone"}) == ["/model", "/clean"]
