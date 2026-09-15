"""Provider relay: alias routes, bound worker action, host invocation, refusals."""

import httpx
import pytest

from recollect.selfmod.journal import IntegrityError, Journal
from recollect.selfmod.provider_broker import ProviderBroker, ProviderPolicy
from recollect.selfmod.provider_relay import ProviderRelay
from tests.selfmod_fake_calendar import FakeCalendar

ORIGIN = "https://calendar-fixture.invalid"
DEDUP = "v0selfmodfi0ture01"  # base32hex: a-v and digits only


@pytest.fixture
async def relay(tmp_path):
    calendar = FakeCalendar()
    journal = Journal.create(tmp_path / "provider")
    broker = ProviderBroker(ProviderPolicy(calendar.calendar_id, ORIGIN), journal,
                            credentials={"worker": lambda: "fixture-token"},
                            transport=calendar.transport())
    invocations = iter(["call-1", "call-2", "call-3"])
    value = ProviderRelay(broker, alias="selfmod-test",
                          invocation_for=lambda task_id: next(invocations))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(value.app), base_url="http://relay",
        headers={"Authorization": "Bearer " + value.token},
    ) as client:
        yield value, broker, calendar, client, journal
    await broker.aclose()
    journal.close()


def kinds(journal, kind):
    return [r.value["data"] for r in journal.verify() if r.value["kind"] == kind]


def event(**changes):
    return {"id": DEDUP, "summary": "Fixture review",
            "start": {"dateTime": "2026-01-02T15:00:00-06:00"},
            "end": {"dateTime": "2026-01-02T15:30:00-06:00"}, **changes}


async def test_token_is_required_and_nothing_is_forwarded_without_it(relay):
    value, _, calendar, client, journal = relay
    response = await client.get("/recollect/action",
                                headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert (await client.post("/calendar/v3/calendars/selfmod-test/events",
                              json=event(), headers={"Authorization": ""})
            ).status_code == 401
    assert not calendar.requests and not kinds(journal, "provider_operation")


async def test_unbound_relay_refuses_and_records_the_violation(relay):
    _, _, calendar, client, journal = relay
    assert (await client.get("/recollect/action")).status_code == 409
    response = await client.post("/calendar/v3/calendars/selfmod-test/events",
                                 json=event())
    assert response.status_code == 409 and not calendar.requests
    assert [r["status"] for r in kinds(journal, "provider_relay_refused")] == [409, 409]


async def test_bound_action_creates_with_frozen_identity_and_host_invocation(relay):
    value, broker, calendar, client, journal = relay
    capability = broker.issue("worker", task_id="task-b", action_id="action-1",
                              dedup_id=DEDUP, routing_epoch=2, serving_digest="b" * 64)
    value.bind(capability)
    broker.open_gate("action-1", "fixture continuation released")
    action = (await client.get("/recollect/action")).json()
    assert action == {"action_id": "action-1", "event_id": DEDUP,
                      "calendar": "selfmod-test"}
    created = await client.post("/calendar/v3/calendars/selfmod-test/events",
                                json=event(id=action["event_id"]))
    assert created.status_code == 200 and DEDUP in calendar.events
    read = await client.get(f"/calendar/v3/calendars/selfmod-test/events/{DEDUP}")
    assert read.status_code == 200 and read.json()["id"] == DEDUP
    listed = await client.get("/calendar/v3/calendars/selfmod-test/events",
                              params={"q": "Fixture"})
    assert [e["id"] for e in listed.json()["items"]] == [DEDUP]
    operations = kinds(journal, "provider_operation")
    assert [o["invocation_id"] for o in operations] == ["call-1", "call-2", "call-3"]
    assert operations[0]["kind"] == "mutation" and operations[0]["task_id"] == "task-b"
    assert "fixture-token" not in str(journal.verify())


async def test_closed_gate_wrong_alias_and_malformed_bodies_are_refused(relay):
    value, broker, calendar, client, journal = relay
    value.bind(broker.issue("worker", task_id="task-a", action_id="action-1",
                            dedup_id=DEDUP))
    closed = await client.post("/calendar/v3/calendars/selfmod-test/events",
                               json=event())
    assert closed.status_code == 403 and "gate" in closed.text
    wrong = await client.post("/calendar/v3/calendars/primary/events", json=event())
    assert wrong.status_code == 404
    bad = await client.post("/calendar/v3/calendars/selfmod-test/events",
                            content=b"[1]",
                            headers={"Content-Type": "application/json"})
    assert bad.status_code == 400
    assert not calendar.requests
    assert [r["status"] for r in kinds(journal, "provider_relay_refused")] == [
        403, 404, 400]
    deleted = await client.delete(f"/calendar/v3/calendars/selfmod-test/events/{DEDUP}")
    assert deleted.status_code == 405


async def test_only_a_worker_capability_can_be_bound_and_unbinding_stops_service(
    relay,
):
    value, broker, _, client, journal = relay
    with pytest.raises(IntegrityError, match="worker"):
        value.bind(broker.issue("cleanup"))
    value.bind(broker.issue("worker", action_id="action-1", dedup_id=DEDUP))
    value.unbind("fixture end")
    assert (await client.get("/recollect/action")).status_code == 409
    assert kinds(journal, "provider_relay_unbound")[-1]["action_id"] == "action-1"


async def test_replay_binding_uses_its_phase_and_fixed_invocation(relay):
    value, broker, calendar, client, journal = relay
    original = broker.issue("worker", task_id="task-b", action_id="action-1",
                            dedup_id=DEDUP)
    broker.open_gate("action-1", "fixture")
    value.bind(original)
    events = "/calendar/v3/calendars/selfmod-test/events"
    assert (await client.post(events, json=event())).status_code == 200
    with pytest.raises(IntegrityError, match="replay"):
        value.bind(original, invocation_id="replay:call-1")
    value.bind(original, phase="replay", invocation_id="replay:call-1")
    replayed = await client.post(events, json=event())
    assert replayed.status_code == 409 and len(calendar.events) == 1
    operations = kinds(journal, "provider_operation")
    assert [(o["phase"], o["invocation_id"]) for o in operations] == [
        ("original", "call-1"), ("replay", "replay:call-1")]
    assert kinds(journal, "provider_relay_bound")[-1]["phase"] == "replay"


async def test_relay_listens_for_containers_and_closes(relay):
    value, *_ = relay
    await value.start()
    try:
        async with httpx.AsyncClient(base_url=value.base_url) as client:
            response = await client.get("/recollect/action")
        assert response.status_code == 401
    finally:
        await value.close()
