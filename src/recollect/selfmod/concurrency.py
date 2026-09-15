"""Generic three-lane concurrency check; observational timing, no cutoffs.

Each probe is pinned to its lane's server slot and streamed with no token cap.
A background sampler records /slots with host monotonic time. Generation is
counted only where a slot is processing one task whose decoded-token count rises
between consecutive samples, inside its linked request's host window. Open
sockets, queued or deferred requests and prompt processing never count. The
registered criterion is at least one second of simultaneous generation across
all three lanes. Durations are recorded; slowness alone never fails a probe.
"""

import asyncio
import json
import time
from dataclasses import asdict, dataclass

import httpx

from .model_settlement import SlotObserver, loopback_root, slot_state

REQUIRED_OVERLAP_NS = 1_000_000_000
LANE_ORDER = ("conversation", "worker", "modifier")


@dataclass(frozen=True)
class LaneProbe:
    lane: str
    slot: int
    prompt: str
    expected: str

    def __post_init__(self):
        if (self.lane not in LANE_ORDER or type(self.slot) is not int
                or not 0 <= self.slot < 64 or not self.prompt or not self.expected):
            raise ValueError("Freeze a lane, pinned slot, prompt and expected answer")


def generating_intervals(samples, slot, window):
    """Intervals where one slot's single task advanced decoding inside a window.

    Sampling can be faster than token cadence, so equal decoded counts between
    two observed increases of the same continuously processing task stay inside
    one interval. A task change, an idle or unlinked sample, or the time before
    the first observed increase never counts as generation.
    """
    start, end = window
    intervals, anchor = [], None
    for observed, value in samples:
        if not start <= observed <= end:
            continue
        state = slot_state(value, slot)
        if (not state["is_processing"] or state["id_task"] is None
                or state["n_decoded"] is None):
            anchor = None
            continue
        if anchor is None or anchor[1] != state["id_task"]:
            anchor = (observed, state["id_task"], state["n_decoded"])
            continue
        if state["n_decoded"] > anchor[2]:
            if intervals and intervals[-1][1] == anchor[0]:
                intervals[-1] = (intervals[-1][0], observed)
            else:
                intervals.append((anchor[0], observed))
            anchor = (observed, state["id_task"], state["n_decoded"])
    return intervals


def longest_overlap(interval_sets):
    """Longest contiguous time covered by every interval set simultaneously."""
    if not interval_sets or any(not intervals for intervals in interval_sets):
        return 0, None
    common = interval_sets[0]
    for intervals in interval_sets[1:]:
        merged = []
        for a_start, a_end in common:
            for b_start, b_end in intervals:
                start, end = max(a_start, b_start), min(a_end, b_end)
                if start < end:
                    merged.append((start, end))
        common = sorted(merged)
    best = max(common, key=lambda item: item[1] - item[0], default=None)
    return (best[1] - best[0], best) if best else (0, None)


async def _probe(client, model, probe, record):
    record["dispatched_ns"] = time.monotonic_ns()
    payload = {"model": model, "stream": True, "id_slot": probe.slot,
               "messages": [{"role": "user", "content": probe.prompt}],
               "chat_template_kwargs": {"enable_thinking": False}}
    text, identities = [], set()
    async with client.stream("POST", "/v1/chat/completions", json=payload) as response:
        record["status"] = response.status_code
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            event = json.loads(data)
            if isinstance(event.get("id"), str):
                identities.add(event["id"])
            for choice in event.get("choices") or ():
                content = (choice.get("delta") or {}).get("content")
                if isinstance(content, str) and content:
                    record.setdefault("first_content_ns", time.monotonic_ns())
                    text.append(content)
    record["completed_ns"] = time.monotonic_ns()
    record["text"] = "".join(text)
    record["response_ids"] = sorted(identities)
    record["correct"] = response.status_code == 200 and probe.expected in record["text"]


async def run_concurrency_check(base_url, model, probes, *, transport=None,
                                poll_interval=0.05, preflight=None):
    """Run pinned probes together and return raw, linked evidence plus verdict."""
    if sorted(p.lane for p in probes) != sorted(LANE_ORDER) or len(
            {p.slot for p in probes}) != len(probes):
        raise ValueError("One probe per lane on distinct pinned slots is required")
    root = loopback_root(base_url)
    observer = SlotObserver(root, transport=transport, poll_interval=poll_interval)
    client = httpx.AsyncClient(
        base_url=root, transport=transport or httpx.AsyncHTTPTransport(
            retries=0, trust_env=False),
        trust_env=False, follow_redirects=False, timeout=None,
    )
    samples, stop = [], asyncio.Event()
    records = {p.lane: {"lane": p.lane, "slot": p.slot} for p in probes}
    evidence = {"version": 1, "criterion_overlap_ns": REQUIRED_OVERLAP_NS,
                "probes": [asdict(p) for p in probes],
                "preflight": preflight() if preflight is not None else None}
    try:
        properties = (await client.get("/props")).json()
        evidence["props"] = {
            "total_slots": properties.get("total_slots"),
            "n_ctx": (properties.get("default_generation_settings") or {}).get("n_ctx"),
        }
        if (type(evidence["props"]["total_slots"]) is not int
                or evidence["props"]["total_slots"] < len(probes)):
            evidence.update(passed=False, reason="insufficient_server_slots")
            return evidence
        _, initial = await observer.slots()
        if any(slot_state(initial, p.slot)["is_processing"] for p in probes):
            evidence.update(passed=False, reason="pinned_slot_not_idle_before_check")
            return evidence

        async def sample():
            while not stop.is_set():
                samples.append(await observer.slots())
                await asyncio.sleep(poll_interval)

        sampler = asyncio.create_task(sample())
        try:
            results = await asyncio.gather(
                *(_probe(client, model, p, records[p.lane]) for p in probes),
                return_exceptions=True,
            )
        finally:
            stop.set()
            await asyncio.gather(sampler, return_exceptions=True)
        for probe, result in zip(probes, results, strict=True):
            if isinstance(result, BaseException):
                records[probe.lane].update(correct=False,
                                           error=type(result).__name__)
        # Record settlement after the check; an idle poll has no deadline.
        while True:
            observed, value = await observer.slots()
            samples.append((observed, value))
            if not any(slot_state(value, p.slot)["is_processing"] for p in probes):
                break
            await asyncio.sleep(poll_interval)
        intervals = {}
        for probe in probes:
            record = records[probe.lane]
            window = (record.get("dispatched_ns", 0),
                      record.get("completed_ns", time.monotonic_ns()))
            intervals[probe.lane] = generating_intervals(samples, probe.slot, window)
            record["duration_ns"] = window[1] - window[0]
        overlap, span = longest_overlap([intervals[p.lane] for p in probes])
        evidence.update(
            records=[records[lane] for lane in LANE_ORDER],
            generating_intervals=intervals, overlap_ns=overlap, overlap_span=span,
            samples=[{"monotonic_ns": t, "slots": value} for t, value in samples],
            all_correct=all(r.get("correct") is True for r in records.values()),
        )
        evidence["passed"] = bool(evidence["all_correct"]
                                  and overlap >= REQUIRED_OVERLAP_NS)
        evidence["reason"] = (None if evidence["passed"] else
                              "incorrect_or_failed_probe" if not evidence["all_correct"]
                              else "insufficient_simultaneous_generation")
        return evidence
    finally:
        await client.aclose()
        await observer.aclose()
