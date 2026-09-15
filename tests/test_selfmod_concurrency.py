"""Generic three-lane concurrency evidence with a fake server; no model runs."""

import pytest

from recollect.selfmod.concurrency import (
    REQUIRED_OVERLAP_NS,
    LaneProbe,
    generating_intervals,
    longest_overlap,
    run_concurrency_check,
)
from tests.selfmod_fake_llama import FakeLlama

URL = "http://127.0.0.1:8001/v1"
PROBES = tuple(LaneProbe(lane, slot, f"Generic {lane} probe; end with ANSWER",
                         "ANSWER")
               for slot, lane in enumerate(("conversation", "worker", "modifier")))


async def check(fake, probes=PROBES):
    return await run_concurrency_check(URL, "fixture-model", probes,
                                       transport=fake.transport(),
                                       poll_interval=0.02,
                                       preflight=lambda: {"gpu": "fixture"})


async def test_linked_simultaneous_generation_meets_registered_criterion():
    fake = FakeLlama(tokens=60, delay=0.03)
    report = await check(fake)
    assert report["passed"] and report["overlap_ns"] >= REQUIRED_OVERLAP_NS
    assert sorted(r["id_slot"] for r in fake.requests) == [0, 1, 2]
    assert all("max_tokens" not in r for r in fake.requests)
    assert report["preflight"] == {"gpu": "fixture"}
    assert report["props"] == {"total_slots": 3, "n_ctx": 8192}
    assert all(r["correct"] and r["duration_ns"] > 0 for r in report["records"])
    assert all(r["response_ids"] for r in report["records"])


async def test_serialized_requests_on_open_sockets_are_not_concurrency():
    report = await check(FakeLlama(tokens=40, delay=0.03, serial=True))
    assert not report["passed"]
    assert report["reason"] == "insufficient_simultaneous_generation"
    assert report["overlap_ns"] < REQUIRED_OVERLAP_NS


async def test_processing_without_decoding_is_not_generation():
    report = await check(FakeLlama(tokens=60, delay=0.03, decode=False))
    assert not report["passed"] and report["overlap_ns"] == 0


async def test_incorrect_probe_fails_even_with_overlap():
    report = await check(FakeLlama(tokens=60, delay=0.03, answer="WRONG"))
    assert not report["passed"] and report["reason"] == "incorrect_or_failed_probe"
    assert report["overlap_ns"] >= REQUIRED_OVERLAP_NS


async def test_slow_but_correct_probes_are_not_failed_by_duration():
    report = await check(FakeLlama(tokens=110, delay=0.03))
    assert report["passed"]
    assert all(r["duration_ns"] > 3 * 10**9 for r in report["records"])


async def test_insufficient_server_slots_is_a_recorded_feasibility_failure():
    report = await check(FakeLlama(slots=1, tokens=5))
    assert report == {**report, "passed": False, "reason": "insufficient_server_slots"}


async def test_busy_pinned_slot_before_check_is_not_accepted():
    fake = FakeLlama()
    fake.state[1]["is_processing"] = True
    report = await check(fake)
    assert not report["passed"]
    assert report["reason"] == "pinned_slot_not_idle_before_check"


def test_probe_inventory_requires_one_distinct_slot_per_lane():
    with pytest.raises(ValueError):
        LaneProbe("research", 0, "p", "e")
    duplicate = (PROBES[0], PROBES[1], LaneProbe("modifier", 1, "p", "ANSWER"))
    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(run_concurrency_check(URL, "m", duplicate))


def test_overlap_is_intersection_not_union():
    assert longest_overlap([[(0, 10)], [(5, 20)], [(8, 9)]]) == (1, (8, 9))
    assert longest_overlap([[(0, 5)], [(6, 10)], [(0, 10)]]) == (0, None)
    samples = [
        (0, [{"id": 0, "is_processing": True, "id_task": 1,
              "next_token": [{"n_decoded": 1}]}]),
        (10, [{"id": 0, "is_processing": True, "id_task": 1,
               "next_token": [{"n_decoded": 2}]}]),
        (20, [{"id": 0, "is_processing": True, "id_task": 2,
               "next_token": [{"n_decoded": 3}]}]),
        (30, [{"id": 0, "is_processing": True, "id_task": 2,
               "next_token": [{"n_decoded": 3}]}]),
    ]
    # A task change and a stalled decode count never extend generation.
    assert generating_intervals(samples, 0, (0, 30)) == [(0, 10)]
    assert generating_intervals(samples, 0, (15, 30)) == []
