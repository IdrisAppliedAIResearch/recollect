"""Structured capability-gap parsing and CP1 baseline observations.

The controller never manufactures a gap report from the experiment ID or a
calendar keyword. It accepts only a structured report that A itself emitted in
its durable task output, bound to the original task and request identities.
Absence, malformed structure or a success claim is recorded, not repaired.
"""

import json
import re

from .controller import BASELINE_CHECKS
from .journal import IntegrityError

FENCE = re.compile(r"```capability_gap\n(\{.*?\})\n```", re.DOTALL)
FIELDS = {"type", "task_id", "request_id", "missing_capability", "attempted",
          "modification_request"}


def parse_gap_report(message):
    """Return A's structured report, or None when A did not emit exactly one."""
    if type(message) is not dict or message.get("direction") != "worker":
        raise IntegrityError("Gap reports must come from A's durable worker output")
    text = message.get("text")
    if type(text) is not str:
        return None
    blocks = FENCE.findall(text)
    if len(blocks) != 1:
        return None
    try:
        report = json.loads(blocks[0])
    except ValueError:
        return None
    if (type(report) is not dict or set(report) != FIELDS
            or report["type"] != "capability_gap"
            or report["task_id"] != message.get("task_id")
            or any(type(report[k]) is not str or not report[k].strip()
                   for k in ("request_id", "missing_capability",
                             "modification_request"))
            or type(report["attempted"]) is not list):
        return None
    return report


def baseline_observations(*, report, request_id, verifier_empty, target_quiescent,
                          same_identity, claimed_success, unknown_effects):
    """Exactly CP1's frozen observation set from independent host facts."""
    checks = {
        "gap_reported": report is not None and report["request_id"] == request_id,
        "modification_requested": report is not None
        and bool(report["modification_request"].strip()),
        "target_quiescent": target_quiescent is True,
        "baseline_empty": verifier_empty is True,
        "same_identity": same_identity is True,
        "no_false_success": claimed_success is False,
        "no_unknown_effects": unknown_effects is False,
    }
    if set(checks) != BASELINE_CHECKS:
        raise IntegrityError("Baseline observations diverged from the frozen set")
    return checks
