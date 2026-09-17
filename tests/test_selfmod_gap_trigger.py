"""Only A's durable structured gap report starts self-modification."""

import json

import pytest

from recollect.selfmod.contracts import IntegrityError
from recollect.selfmod.gap_trigger import parse_gap_report


def gap_message(kind="blocked", related="start:request-1", **report_changes):
    report = {"type": "capability_gap",
              "missing_capability": "No available tool can perform the operation.",
              "attempted": ["listed available tools"],
              "modification_request": "Add the smallest integration for it.",
              **report_changes}
    return {"direction": "subagent", "task_id": "task-original", "kind": kind,
            "message_id": "report-7", "revision": 1, "payload": {
                "text": "Blocked.\n```capability_gap\n" + json.dumps(report) + "\n```",
                "reply_to": related, "sources": [], "artifacts": []}}


def test_only_durable_structured_subagent_gap_reports_are_accepted():
    report = parse_gap_report(gap_message())
    assert report["task_id"] == "task-original"
    assert report["related_message_id"] == "start:request-1"
    assert parse_gap_report(gap_message(kind="result")) is None
    keyword_only = gap_message()
    keyword_only["payload"]["text"] = "this request is unsupported"
    assert parse_gap_report(keyword_only) is None
    assert parse_gap_report(gap_message(attempted="not a list")) is None
    assert parse_gap_report(gap_message(modification_request=" ")) is None
    with pytest.raises(IntegrityError, match="subagent report"):
        parse_gap_report({**gap_message(), "direction": "main"})
