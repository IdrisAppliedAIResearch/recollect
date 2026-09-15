"""Evaluator provider fixture, relay reconciliation path and tool transport wiring."""

import json

import httpx
import pytest

from recollect.config import RecollectConfig
from recollect.engine.sandbox import configgen
from recollect.engine.sandbox.manager import SandboxManager
from recollect.selfmod import acceptance, provider_fixture
from recollect.selfmod.journal import Journal
from recollect.selfmod.provider_broker import ProviderBroker, ProviderPolicy
from recollect.selfmod.provider_relay import ProviderRelay

ORIGIN = "https://calendar-fixture.invalid"
EVENT_ID = "v0candidatecheck01"


def body(**changes):
    return {"id": EVENT_ID, "summary": "Candidate check",
            "start": {"dateTime": "2031-03-04T10:00:00-06:00",
                      "timeZone": "America/Chicago"},
            "end": {"dateTime": "2031-03-04T10:30:00-06:00",
                    "timeZone": "America/Chicago"}, **changes}


async def direct(calendar, method, path, **kwargs):
    async with httpx.AsyncClient(transport=calendar.transport(),
                                 base_url="https://fixture",
                                 headers={"Authorization": "Bearer x"}) as client:
        return await client.request(method, path, **kwargs)


async def test_fixture_answers_api_shapes_without_assertions():
    calendar = provider_fixture.FixtureCalendar()
    events = f"/calendar/v3/calendars/{calendar.calendar_id}/events"
    assert (await direct(calendar, "POST", events, content=b"nope")).status_code == 400
    assert (await direct(calendar, "POST", events, json={"id": 1})).status_code == 400
    assert (await direct(calendar, "GET", "/other")).status_code == 404
    created = await direct(calendar, "POST", events, json=body())
    assert created.status_code == 200 and created.json()["htmlLink"]
    assert (await direct(calendar, "POST", events, json=body())).status_code == 409
    listed = await direct(calendar, "GET", events, params={"q": "Candidate"})
    assert [e["id"] for e in listed.json()["items"]] == [EVENT_ID]
    assert (await direct(calendar, "PATCH", events + "/" + EVENT_ID)).status_code == 405
    async with httpx.AsyncClient(transport=calendar.transport(),
                                 base_url="https://fixture") as client:
        assert (await client.get(events)).status_code == 401


@pytest.mark.parametrize("status", [400, 403, 500])
async def test_failed_inserts_create_nothing(status):
    calendar = provider_fixture.FixtureCalendar()
    calendar.faults.append(provider_fixture.fail_inserts(status))
    events = f"/calendar/v3/calendars/{calendar.calendar_id}/events"
    assert (await direct(calendar, "POST", events, json=body())).status_code == status
    assert not calendar.events
    assert (await direct(calendar, "GET", events)).status_code == 200


async def test_lost_response_requires_a_read_before_another_create(tmp_path):
    calendar = provider_fixture.FixtureCalendar()
    calendar.faults.append(provider_fixture.lose_first_insert_response())
    journal = Journal.create(tmp_path / "provider")
    broker = ProviderBroker(ProviderPolicy(calendar.calendar_id, ORIGIN), journal,
                            credentials={"worker": lambda: "fixture"},
                            transport=calendar.transport())
    relay = ProviderRelay(broker, alias="evaluation")
    relay.bind(broker.issue("worker", action_id="check", dedup_id=EVENT_ID))
    broker.open_gate("check", "fixture scenario")
    events = "/calendar/v3/calendars/evaluation/events"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(relay.app), base_url="http://relay",
            headers={"Authorization": "Bearer " + relay.token},
        ) as client:
            lost = await client.post(events, json=body())
            assert lost.status_code == 502 and EVENT_ID in calendar.events
            again = await client.post(events, json=body())
            assert again.status_code == 403 and "Reconcile" in again.text
            read = await client.get(events + "/" + EVENT_ID)
            assert read.status_code == 200
            repeat = await client.post(events, json=body())
            assert repeat.status_code == 409
        assert len(calendar.events) == 1
    finally:
        await broker.aclose()
        journal.close()


def test_tool_environment_is_limited_to_provider_transport(tmp_path):
    common = dict(base_url="http://m/v1", model="m", api_key="k", steps=1,
                  continuous=True)
    config = configgen.build_config(tmp_path, **common, tool_environment={
        "RECOLLECT_PROVIDER_URL": "http://host.docker.internal:5",
        "RECOLLECT_PROVIDER_TOKEN": "relay-capability"})
    assert config["mcp"][configgen.MCP_SERVER]["environment"] == {
        "RECOLLECT_TASK_REPORTING": "1",
        "RECOLLECT_PROVIDER_URL": "http://host.docker.internal:5",
        "RECOLLECT_PROVIDER_TOKEN": "relay-capability"}
    for bad in ({"PATH": "/tmp"}, {"RECOLLECT_TASK_REPORTING": "0"},
                {"RECOLLECT_PROVIDER_URL": 5}):
        with pytest.raises(ValueError, match="provider transport"):
            configgen.build_config(tmp_path, **common, tool_environment=bad)


def test_manager_routes_the_relay_through_the_host_gateway(tmp_path):
    config = RecollectConfig(embedding_model_path=tmp_path / "e.gguf",
                             data_dir=tmp_path / "var")
    manager = SandboxManager(config)
    manager.configure_tools("http://127.0.0.1:43210", "token")
    assert manager._tool_environment == {
        "RECOLLECT_PROVIDER_URL": "http://host.docker.internal:43210",
        "RECOLLECT_PROVIDER_TOKEN": "token"}
    manager._handle = object()
    with pytest.raises(RuntimeError, match="live sandbox"):
        manager.configure_tools("http://127.0.0.1:1", "other")


def test_acceptance_texts_name_no_tool_and_cover_the_transport():
    requirements = {r.id for r in acceptance.REQUIREMENTS}
    assert {"exact-event", "stable-identity", "honest-reporting"} <= requirements
    notes = acceptance.TRANSPORT_NOTES
    assert "RECOLLECT_PROVIDER_URL" in notes and "/recollect/action" in notes
    assert "event_id" in notes and "timeout" in notes
    assert json.dumps([r.acceptance for r in acceptance.REQUIREMENTS]).count(
        "recollect_research_") == 0
