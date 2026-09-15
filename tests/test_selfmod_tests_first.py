"""Tests-first authoring: anchored, validated, reviewed and frozen before code."""

import json
import subprocess
import sys

import httpx
import pytest

from recollect.selfmod.tests_first import (
    REGRESSION,
    authoring,
    model_completer,
    parse_tests,
)

REQUEST = "Add an event called Demo to my calendar tomorrow at 10."
CHECK = "import sys\nsys.exit(0)\n"


def authored(**changes):
    value = {
        "requirements": [
            {"id": "original_request", "acceptance": "Demo is created at 10 tomorrow"},
            {"id": "auth_steps", "acceptance": "Unauthorized use lists auth steps"},
        ],
        "checks": [
            {"name": "creates_event", "requirement_ids": ["original_request"],
             "text": CHECK},
            {"name": "reports_auth", "requirement_ids": ["auth_steps"], "text": CHECK},
        ],
    }
    value.update(changes)
    return value


def test_parse_binds_requirements_to_checks_and_appends_regression():
    tests = parse_tests(authored())
    assert tests.names == ("creates_event", "reports_auth", "regression")
    assert tests.checks[-1] == REGRESSION
    assert tests.requirements[0].evidence == "checks: creates_event"
    assert tests.sha256 == parse_tests(authored()).sha256


@pytest.mark.parametrize("change, match", [
    ({"requirements": [{"id": "other", "acceptance": "x"}]}, "anchored"),
    ({"checks": [{"name": "creates_event", "requirement_ids": ["original_request"],
                  "text": CHECK}]}, "without a check"),
    ({"checks": [{"name": "broken", "requirement_ids": ["original_request",
                                                        "auth_steps"],
                  "text": "def (:\n"}]}, "does not parse"),
    ({"checks": [{"name": "regression", "requirement_ids": ["original_request",
                                                            "auth_steps"],
                  "text": CHECK}]}, "Invalid"),
    ({"checks": [{"name": "x", "requirement_ids": ["unknown"], "text": CHECK}]},
     "Invalid"),
    ({"extra": []}, "exactly"),
])
def test_parse_rejects_unanchored_uncovered_or_invalid_tests(change, match):
    with pytest.raises(ValueError, match=match):
        parse_tests(authored(**change))


def test_regression_check_fails_on_uncompilable_source(tmp_path):
    script = tmp_path / "regression.py"
    script.write_bytes(REGRESSION.content)
    source = tmp_path / "source"
    source.mkdir()
    (source / "ok.py").write_text("value = 1\n")
    run = [sys.executable, "-I", "-S", "-B", str(script)]
    assert subprocess.run(run, cwd=source, check=False).returncode == 0
    (source / "bad.py").write_text("def (:\n")
    assert subprocess.run(run, cwd=source, check=False,
                          capture_output=True).returncode == 1


async def test_authoring_revises_until_an_independent_review_approves():
    authors = iter([{"requirements": []}, authored(), authored()])
    reviews = iter([{"approved": False, "findings": ["no edge case"],
                     "rationale": "missing"},
                    {"approved": True, "findings": [], "rationale": "anchored"}])
    seen, records = [], []

    async def author(prompt, context):
        seen.append(("author", context))
        return next(authors)

    async def reviewer(prompt, context):
        seen.append(("review", context))
        assert context["original_request"] == REQUEST
        return next(reviews)

    async def record(kind, data, files=None):
        records.append(kind)

    run = authoring(REQUEST, author=author, reviewer=reviewer)
    tests = await run({"missing_capability": "calendar"}, lambda: False, record)
    assert tests.names[-1] == "regression"
    assert records == ["tests_rejected", "tests_authored", "tests_rejected",
                       "tests_authored", "tests_frozen"]
    last_author = [c for kind, c in seen if kind == "author"][-1]
    assert last_author["original_request"] == REQUEST
    assert [h["stage"] for h in last_author["history"]] == ["authoring", "review"]


async def test_authoring_stops_when_the_user_stops():
    async def never(prompt, context):
        raise AssertionError("no model call after stop")

    run = authoring(REQUEST, author=never, reviewer=never)
    assert await run({}, lambda: True, None) is None


async def test_model_completer_sends_one_shot_json_request():
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": '{"ok": true}'}}]})

    complete = model_completer("http://127.0.0.1:1/v1", "local", slot=2,
                               transport=httpx.MockTransport(handle))
    assert await complete("prompt", {"a": 1}) == {"ok": True}
    assert calls[0]["id_slot"] == 2 and calls[0]["messages"][0]["content"] == "prompt"
