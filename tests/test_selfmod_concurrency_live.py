"""Opt-in generic concurrency qualification against a real three-slot server.

Never runs by default: it needs an explicitly provided loopback server already
started with three slots. It sends generic non-target prompts only, no task or
calendar content, and records GPU/server identity as observational evidence.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from recollect.selfmod.concurrency import LaneProbe, run_concurrency_check

pytestmark = pytest.mark.skipif(
    os.environ.get("RECOLLECT_RUN_SELFMOD_REAL_MODEL_TESTS") != "1"
    or not os.environ.get("RECOLLECT_SELFMOD_THREE_SLOT_URL"),
    reason="opt in with an explicitly started three-slot model server",
)

PROMPTS = {
    "conversation": "List the numbers from 1 to 120 separated by spaces, "
                    "then write the word ALPHA.",
    "worker": "List the numbers from 200 to 320 separated by spaces, "
              "then write the word BRAVO.",
    "modifier": "List the numbers from 400 to 520 separated by spaces, "
                "then write the word CHARLIE.",
}
EXPECTED = {"conversation": "ALPHA", "worker": "BRAVO", "modifier": "CHARLIE"}


def gpu_preflight():
    """Read-only hardware identity; it never changes GPU or server state."""
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used",
         "--format=csv,noheader"], capture_output=True, text=True, check=False)
    return {"returncode": result.returncode, "stdout": result.stdout[:4096],
            "stderr": result.stderr[:4096]}


async def test_real_server_three_lane_generation_overlaps():
    probes = tuple(LaneProbe(lane, slot, PROMPTS[lane], EXPECTED[lane])
                   for slot, lane in enumerate(("conversation", "worker", "modifier")))
    report = await run_concurrency_check(
        os.environ["RECOLLECT_SELFMOD_THREE_SLOT_URL"],
        os.environ.get("RECOLLECT_SELFMOD_MODEL", "local"), probes,
        poll_interval=0.05, preflight=gpu_preflight,
    )
    evidence = Path(".agent") / "concurrency-live-report.json"
    evidence.write_text(json.dumps(report, indent=1, sort_keys=True))
    assert report["passed"], report["reason"]
