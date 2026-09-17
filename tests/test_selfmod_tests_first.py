"""Tests-first authoring: an interface and checks, reviewed and frozen before code."""

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from recollect.selfmod import subagent_tree
from recollect.selfmod.tests_first import (
    AUTHOR_PROMPT,
    REGRESSION,
    REVIEW_PROMPT,
    authoring,
    message,
    model_completer,
    parse_tests,
)

REPOSITORY = Path(__file__).resolve().parents[1]
REQUEST = "Add an event called Demo to my calendar tomorrow at 10."
CHECK = "import sys\nsys.exit(0)\n"
GAP = {"missing_capability": "calendar write", "attempted": ["search"],
       "modification_request": "add a calendar tool", "task_id": "t"}
INTERFACE = {"module": "recollect.engine.subagent_tools.calendar",
             "tool_name": "create_event",
             "functions": [{"name": "create_event",
                            "signature": "(title: str, start: str) -> dict",
                            "returns": "{status, steps}"}]}


def authored(**changes):
    value = {
        "interface": INTERFACE,
        "requirements": [
            {"id": "original_request", "acceptance": "Demo is created at 10 tomorrow"},
            {"id": "auth_steps", "acceptance": "Unauthorized use lists auth steps"},
        ],
        "checks": [
            {"name": "creates_event", "requirement_ids": ["original_request"],
             "text": CHECK},
            {"name": "reports_auth", "requirement_ids": ["auth_steps"], "text": CHECK},
        ],
        "unverified": [],
    }
    value.update(changes)
    return value


@pytest.fixture(scope="module")
def tree():
    baseline = subagent_tree.baseline(REPOSITORY)
    return baseline, subagent_tree.change_policy(baseline)


def test_parse_binds_requirements_to_checks_and_appends_regression():
    tests = parse_tests(authored())
    assert tests.names == ("creates_event", "reports_auth", "regression")
    assert tests.checks[-1] == REGRESSION
    assert tests.requirements[0].evidence == "checks: creates_event"
    assert tests.sha256 == parse_tests(authored()).sha256
    assert tests.sha256 != parse_tests(authored(unverified=["qr decodes"])).sha256


def test_interface_and_unverified_bind_development_requirements():
    tests = parse_tests(authored(unverified=["decodes in a phone scanner"]))
    ids = [r.id for r in tests.contract_requirements]
    assert ids == ["original_request", "auth_steps", "interface", "unverified"]
    interface = tests.contract_requirements[2].acceptance
    assert "recollect.engine.subagent_tools.calendar" in interface
    assert "decodes in a phone scanner" in tests.contract_requirements[3].acceptance
    assert [r.id for r in parse_tests(authored()).contract_requirements][-1] == (
        "interface")


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
    ({"interface": {**INTERFACE, "module": "recollect.engine.mcp_research"}},
     "interface"),
    ({"interface": {**INTERFACE, "functions": []}}, "interface"),
    ({"requirements": [{"id": "original_request", "acceptance": "x"},
                       {"id": "interface", "acceptance": "y"}]}, "reserved"),
    ({"unverified": [""]}, "unverified"),
])
def test_parse_rejects_invalid_tests(change, match):
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


def test_message_gives_both_roles_the_codebase_and_check_environment(tree):
    baseline, policy = tree
    text = message(REQUEST, GAP, baseline, policy)
    tags = ["request", "capability_gap", "codebase", "check_environment"]
    assert [text.index(f"<{tag}>") for tag in tags] == sorted(
        text.index(f"<{tag}>") for tag in tags)
    assert '<file path="recollect/engine/mcp_research.py">' in text
    assert "recollect/engine/subagent_tools/" in text
    assert "httpx, mcp and trafilatura" in text
    assert "task_id" not in text and "<tests>" not in text
    reviewed = message(REQUEST, GAP, baseline, policy, tests=authored(),
                       history=[{"stage": "review", "findings": ["x"]}])
    assert reviewed.index("<tests>") < reviewed.index("<history>")


async def test_authoring_revises_until_an_independent_review_approves(tree):
    baseline, policy = tree
    authors = iter([{"requirements": []}, authored(), authored()])
    reviews = iter([{"approved": False, "findings": ["no edge case"],
                     "rationale": "missing"},
                    {"approved": True, "findings": [], "rationale": "anchored"}])
    seen, records = [], []

    async def author(prompt, text):
        assert prompt == AUTHOR_PROMPT
        seen.append(("author", text))
        return next(authors)

    async def reviewer(prompt, text):
        assert prompt == REVIEW_PROMPT and "<tests>" in text
        seen.append(("review", text))
        return next(reviews)

    async def record(kind, data, files=None):
        records.append(kind)

    run = authoring(REQUEST, baseline=baseline, policy=policy, author=author,
                    reviewer=reviewer)
    tests = await run(GAP, lambda: False, record)
    assert tests.names[-1] == "regression" and tests.interface == INTERFACE
    assert records == ["tests_rejected", "tests_authored", "tests_rejected",
                       "tests_authored", "tests_frozen"]
    last_author = [text for kind, text in seen if kind == "author"][-1]
    history = json.loads(last_author.split("<history>\n")[1].split("\n</history>")[0])
    assert [h["stage"] for h in history] == ["authoring", "review"]
    assert history[0]["findings"][0].startswith("Your last reply was rejected")


async def test_cut_off_reply_becomes_a_finding_not_a_crash(tree):
    baseline, policy = tree
    replies = iter([ValueError("the reply was cut off at the length limit"),
                    authored()])
    records = []

    async def author(prompt, text):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def reviewer(prompt, text):
        return {"approved": True, "findings": [], "rationale": "ok"}

    async def record(kind, data, files=None):
        records.append((kind, data))

    run = authoring(REQUEST, baseline=baseline, policy=policy, author=author,
                    reviewer=reviewer)
    assert await run(GAP, lambda: False, record)
    assert "cut off" in records[0][1]["findings"][0]


async def test_authoring_stops_when_the_user_stops(tree):
    baseline, policy = tree

    async def never(prompt, text):
        raise AssertionError("no model call after stop")

    run = authoring(REQUEST, baseline=baseline, policy=policy, author=never,
                    reviewer=never)
    assert await run(GAP, lambda: True, None) is None


async def test_model_completer_sends_text_and_flags_a_cut_off_reply():
    calls, finish = [], ["stop"]

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{
            "finish_reason": finish[0], "message": {"content": '{"ok": true}'}}]})

    complete = model_completer("http://127.0.0.1:1/v1", "local", slot=2,
                               transport=httpx.MockTransport(handle))
    assert await complete("prompt", "<request>\nx\n</request>") == {"ok": True}
    assert calls[0]["id_slot"] == 2 and calls[0]["messages"][0]["content"] == "prompt"
    assert calls[0]["messages"][1]["content"] == "<request>\nx\n</request>"
    assert "max_tokens" not in calls[0]
    finish[0] = "length"
    with pytest.raises(ValueError, match="cut off"):
        await complete("prompt", "x")


class Admission:
    def __init__(self):
        self.calls = []

    async def acquire(self, *, lane):
        self.calls.append(("acquire", lane))

    def release(self, *, lane):
        self.calls.append(("release", lane))


async def test_model_completer_queues_through_model_admission():
    admission = Admission()

    def handle(request):
        assert admission.calls == [("acquire", "worker")]
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": '{"ok": true}'}}]})

    complete = model_completer("http://127.0.0.1:1/v1", "local", admission=admission,
                               lane="worker", transport=httpx.MockTransport(handle))
    assert await complete("prompt", "x") == {"ok": True}
    assert admission.calls == [("acquire", "worker"), ("release", "worker")]
