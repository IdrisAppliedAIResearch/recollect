"""Provider broker, independent verifier, replay, attribution and gap trigger.

Fixture provider only: no Google account, credential, network or calendar write.
"""

import json
from datetime import date

import httpx
import pytest

from recollect.selfmod.calendar_evaluator import (
    CalendarVerifier,
    ExpectedEvent,
    attribute,
    cleanup,
)
from recollect.selfmod.gap_trigger import baseline_observations, parse_gap_report
from recollect.selfmod.journal import IntegrityError, Journal
from recollect.selfmod.provider_broker import ProviderBroker, ProviderPolicy
from tests.selfmod_fake_calendar import FakeCalendar

EXPECTED = ExpectedEvent("exp0001", date(2026, 10, 1), "America/Chicago")
# Google Calendar client event IDs are base32hex (0-9, a-v).
DEDUP = "selfmod0001action1"


@pytest.fixture
async def world(tmp_path):
    calendar = FakeCalendar()
    journal = Journal.create(tmp_path / "provider")
    policy = ProviderPolicy(calendar.calendar_id, 10)
    broker = ProviderBroker(policy, journal, credential=lambda: "host-secret-token",
                            transport=calendar.transport())
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    verifier = CalendarVerifier(broker, broker.issue("verifier"), EXPECTED,
                                sleep=sleep)
    yield calendar, journal, broker, verifier, sleeps
    await broker.aclose()
    journal.close()


def event_body(expected=EXPECTED, dedup=DEDUP, **changes):
    return {"id": dedup, "summary": expected.title,
            "start": {"dateTime": expected.start.isoformat(),
                      "timeZone": expected.time_zone},
            "end": {"dateTime": expected.end.isoformat(),
                    "timeZone": expected.time_zone}, **changes}


def worker(broker, **binding):
    return broker.issue("worker", task_id="task-original", action_id="action-1",
                        dedup_id=DEDUP, routing_epoch=2, serving_digest="b" * 64,
                        invocation_id="inv-1", **binding)


def operations(journal):
    return [r.value["data"] for r in journal.verify()
            if r.value["kind"] == "provider_operation"]


async def test_broker_forwards_only_policy_routes_and_never_records_credential(world):
    calendar, journal, broker, _, _ = world
    capability = worker(broker)
    events = broker.policy.events_path
    for method, path, params in (("PATCH", events + "/x1234", None),
                                 ("GET", "/calendar/v3/users/me/calendarList", None),
                                 ("GET", events, {"privateExtendedProperty": "x"}),
                                 ("DELETE", events + "/x1234", None)):
        with pytest.raises(IntegrityError):
            await broker.request(capability, method, path, params=params)
    assert not calendar.requests
    forged = type(capability)("worker", "forged-token")
    with pytest.raises(IntegrityError, match="forged"):
        await broker.request(forged, "GET", events)
    archive = b"".join(r.body + b"".join(f.content for f in r.files.files)
                       for r in journal.verify())
    assert b"host-secret-token" not in archive


async def test_target_gate_dedup_identity_and_attempt_limits_are_enforced(world):
    calendar, journal, broker, _, _ = world
    capability = worker(broker)
    events = broker.policy.events_path
    with pytest.raises(IntegrityError, match="gate is closed"):
        await broker.request(capability, "POST", events, body=event_body())
    broker.open_gate("action-1", "cp4_sealed_continuation_released")
    with pytest.raises(IntegrityError, match="dedup"):
        await broker.request(capability, "POST", events,
                             body=event_body(dedup="othervalue1"))
    calendar.faults.append(lambda request: httpx.ReadTimeout("uncertain")
                           if request.method == "POST" else None)
    for _ in range(3):
        with pytest.raises(httpx.ReadTimeout):
            await broker.request(capability, "POST", events, body=event_body())
    with pytest.raises(IntegrityError, match="exhausted"):
        await broker.request(capability, "POST", events, body=event_body())
    calendar.faults.clear()
    status, _ = await broker.request(capability, "POST", events, body=event_body(),
                                     phase="replay")
    assert status == 200
    writes = [o for o in operations(journal) if o["kind"] == "mutation"]
    assert [w["attempt"] for w in writes] == [1, 2, 3, 1]
    assert all(w["transient"] for w in writes[:3])


async def test_fixture_principal_can_never_use_live_broker(world):
    _, _, broker, _, _ = world
    with pytest.raises(IntegrityError, match="Fixture"):
        broker.issue("fixture")


async def test_verifier_is_read_only_and_cleanup_limited_to_verified_event(world):
    calendar, _, broker, verifier, _ = world
    with pytest.raises(IntegrityError):
        await broker.request(verifier._capability, "POST", broker.policy.events_path,
                             body=event_body())
    calendar.insert(event_body(dedup="unrelated00001", summary="Other meeting"))
    remover = broker.issue("cleanup")
    with pytest.raises(IntegrityError, match="verified"):
        await broker.request(remover, "DELETE",
                             broker.policy.events_path + "/unrelated00001")
    assert calendar.events["unrelated00001"]["status"] == "confirmed"


async def test_exactly_one_correct_event_replay_attribution_and_cleanup(world):
    calendar, journal, broker, verifier, _ = world
    empty, _ = await verifier.baseline_empty()
    assert empty
    capability = worker(broker)
    broker.open_gate("action-1", "cp4_sealed_continuation_released")
    events = broker.policy.events_path
    status, created = await broker.request(capability, "POST", events,
                                           body=event_body())
    assert status == 200
    first = await verifier.verify(created["id"], claimed="created")
    assert first["action_result"] == "pass" and first["matching_count"] == 1
    status, _ = await broker.request(capability, "POST", events, body=event_body(),
                                     phase="replay")
    assert status == 409
    replay = await verifier.verify_replay(first, created["id"])
    assert replay["result"] == "pass"
    attribution = attribute(
        operations(journal), verified_event_id=created["id"], action_id="action-1",
        serving_digest="b" * 64, routing_epoch=2,
        invocation_modules={"inv-1": "tools/calendar_integration.py"},
        sealed_after_ns=0)
    assert attribution["attributed"], attribution
    receipt = await cleanup(broker, broker.issue("cleanup"), first)
    assert receipt["deleted"]
    assert calendar.events[created["id"]]["status"] == "cancelled"


@pytest.mark.parametrize("fault", ["duplicate", "wrong_time", "attendee", "absent"])
async def test_wrong_duplicate_or_absent_events_fail_with_field_detail(world, fault):
    calendar, _, _, verifier, _ = world
    calendar.insert(event_body())
    if fault == "duplicate":
        calendar.insert(event_body(dedup="selfmod0001dupe"))
    elif fault == "wrong_time":
        calendar.events[DEDUP]["start"]["dateTime"] = EXPECTED.start.replace(
            hour=16).isoformat()
    elif fault == "attendee":
        calendar.events[DEDUP]["attendees"] = [{"email": "x@example.invalid"}]
    result = await verifier.verify("missingevent1" if fault == "absent" else DEDUP)
    assert result["action_result"] == "failed"
    if fault == "duplicate":
        assert result["observed"] == "duplicate"
    elif fault == "absent":
        assert result["observed"] == "absent"
    else:
        assert result["observed"] == "wrong" and result["mismatches"]


async def test_exhausted_transient_reads_are_unknown_not_empty(world):
    calendar, _, _, verifier, sleeps = world
    calendar.faults.append(lambda request: httpx.Response(503))
    empty, result = await verifier.baseline_empty()
    assert empty is False and result["complete"] is False
    assert result["count"] is None and sleeps == [1, 2]
    verified = await verifier.verify(DEDUP)
    assert verified["observed"] == "unknown"


async def test_complete_search_reads_every_page(world):
    calendar, _, _, verifier, _ = world
    for index in range(5):
        calendar.insert(event_body(dedup=f"otherexp0001{index}",
                                   summary=f"Self-modification review exp0001 {index}"))
    result = await verifier.search()
    assert result["complete"] and result["count"] == 5
    assert len(result["reads"]) == 3


@pytest.mark.parametrize("fault", ["pre_activation", "wrong_digest", "unmapped",
                                   "two_creates"])
def test_missing_or_ambiguous_attribution_fails(fault):
    write = {"kind": "mutation", "action_id": "action-1", "status": 200,
             "response_fields": {"id": DEDUP}, "serving_digest": "b" * 64,
             "routing_epoch": 2, "invocation_id": "inv-1", "dispatched_ns": 100}
    writes = [dict(write)]
    if fault == "pre_activation":
        writes[0]["dispatched_ns"] = 5
    elif fault == "wrong_digest":
        writes[0]["serving_digest"] = "a" * 64
    elif fault == "unmapped":
        writes[0]["invocation_id"] = "inv-unknown"
    else:
        writes.append(dict(write))
    result = attribute(writes, verified_event_id=DEDUP, action_id="action-1",
                       serving_digest="b" * 64, routing_epoch=2,
                       invocation_modules={"inv-1": "tools/calendar.py"},
                       sealed_after_ns=10)
    assert not result["attributed"] and result["reasons"]


def gap_message(**report_changes):
    report = {"type": "capability_gap", "task_id": "task-original",
              "request_id": "request-1",
              "missing_capability": "No integration can create provider events.",
              "attempted": ["listed available tools"],
              "modification_request": "Add an event creation integration.",
              **report_changes}
    return {"direction": "worker", "task_id": "task-original",
            "text": "I cannot do this.\n```capability_gap\n" + json.dumps(report)
            + "\n```\n"}


def test_only_as_emitted_structured_gap_reports_are_accepted():
    report = parse_gap_report(gap_message())
    assert report["request_id"] == "request-1"
    assert parse_gap_report({"direction": "worker", "task_id": "task-original",
                             "text": "calendar exp0001 unsupported"}) is None
    assert parse_gap_report(gap_message(task_id="other-task")) is None
    assert parse_gap_report(gap_message(extra="field")) is None
    with pytest.raises(IntegrityError, match="worker output"):
        parse_gap_report({**gap_message(), "direction": "controller"})
    checks = baseline_observations(report=report, request_id="request-1",
                                   verifier_empty=True, target_quiescent=True,
                                   same_identity=True, claimed_success=False,
                                   unknown_effects=False)
    assert all(checks.values())
    failing = baseline_observations(report=None, request_id="request-1",
                                    verifier_empty=None, target_quiescent=True,
                                    same_identity=True, claimed_success=True,
                                    unknown_effects=False)
    assert not failing["gap_reported"] and not failing["no_false_success"]
    assert not failing["baseline_empty"]
