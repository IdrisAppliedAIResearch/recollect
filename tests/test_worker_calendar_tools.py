"""The sandbox's calendar tools: relay access only, tokens never escape."""

import json

import httpx
import pytest

from recollect.engine.subagent_tools.google_calendar import (
    KEY_VARIABLE,
    URL_VARIABLE,
    _create_event,
    _list_events,
)

RELAY = "http://127.0.0.1:59999"
ACCESS = "secret-access-token"


class Services:
    """Relay and Google API behind one transport; records every request."""

    def __init__(self, relay_status=200, google_status=200, google_body=None):
        self.relay_status = relay_status
        self.google_status = google_status
        self.google_body = google_body or {"items": [
            {"id": "e1", "summary": "Dentist",
             "start": {"dateTime": "2026-09-18T10:00:00Z"},
             "end": {"dateTime": "2026-09-18T11:00:00Z"}}]}
        self.requests = []

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, request):
        self.requests.append(request)
        if request.url.host == "127.0.0.1":
            assert request.url.path == "/connectors/google_calendar"
            assert request.headers["authorization"] == "Bearer svc-key"
            if self.relay_status != 200:
                return httpx.Response(self.relay_status)
            return httpx.Response(200, json={
                "access_token": ACCESS, "expires_in": 3599, "scope": "s",
                "calendar_id": "me@group.calendar.google.com"})
        assert request.headers["authorization"] == f"Bearer {ACCESS}"
        return httpx.Response(self.google_status, json=self.google_body)


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setenv(URL_VARIABLE, RELAY)
    monkeypatch.setenv(KEY_VARIABLE, "svc-key")
    return Services()


async def test_listing_uses_the_relay_token_and_configured_calendar(relay):
    document = json.loads(await _list_events(transport=relay.transport()))
    assert document["events"] == [{"id": "e1", "summary": "Dentist",
                                   "start": "2026-09-18T10:00:00Z",
                                   "end": "2026-09-18T11:00:00Z"}]
    assert document["calendar_id"] == "me@group.calendar.google.com"
    assert "me%40group.calendar.google.com" not in relay.requests[1].url.path
    assert "me@group.calendar.google.com" in relay.requests[1].url.path
    assert ACCESS not in json.dumps(document)


async def test_creating_an_event_posts_it_and_returns_its_link(relay):
    relay.google_body = {"id": "new1", "summary": "Standup",
                         "start": {"dateTime": "2026-09-18T09:00:00+05:30"},
                         "end": {"dateTime": "2026-09-18T09:15:00+05:30"},
                         "htmlLink": "https://calendar.google.com/event?x"}
    document = json.loads(await _create_event(
        "Standup", "2026-09-18T09:00:00+05:30", "2026-09-18T09:15:00+05:30",
        location="Room 2", transport=relay.transport()))
    assert document["event_id"] == "new1"
    assert document["link"].endswith("event?x")
    sent = json.loads(relay.requests[1].content)
    assert sent["summary"] == "Standup" and sent["location"] == "Room 2"
    assert "description" not in sent
    assert sent["start"] == {"dateTime": "2026-09-18T09:00:00+05:30"}
    assert relay.requests[1].method == "POST"
    assert ACCESS not in json.dumps(document)


async def test_an_all_day_pair_is_sent_as_dates(relay):
    relay.google_body = {"id": "a1", "summary": "Off",
                         "start": {"date": "2026-09-20"},
                         "end": {"date": "2026-09-21"}}
    await _create_event("Off", "2026-09-20", "2026-09-21",
                        transport=relay.transport())
    sent = json.loads(relay.requests[1].content)
    assert sent["start"] == {"date": "2026-09-20"}


@pytest.mark.parametrize("start,end", [
    ("tomorrow", "2026-09-19"),
    ("2026-09-18", "2026-09-18T10:00:00Z"),
])
async def test_bad_time_pairs_never_reach_google(relay, start, end):
    document = json.loads(await _create_event("X", start, end,
                                              transport=relay.transport()))
    assert "error" in document
    assert len(relay.requests) == 0


async def test_without_a_connection_service_the_tool_refuses_cleanly(
        monkeypatch):
    monkeypatch.delenv(URL_VARIABLE, raising=False)
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    document = json.loads(await _list_events(transport=relay_transport()))
    assert "No connection service" in document["error"]


def relay_transport():
    return httpx.MockTransport(lambda request: httpx.Response(500))


async def test_a_service_that_is_not_connected_says_so(relay):
    relay.relay_status = 404
    document = json.loads(await _list_events(transport=relay.transport()))
    assert "not connected" in document["error"]


async def test_a_revoked_consent_tells_the_worker_to_ask_again(relay):
    relay.google_status = 403
    document = json.loads(await _list_events(transport=relay.transport()))
    assert "revoked" in document["error"]
    document = json.loads(await _create_event(
        "X", "2026-09-18", "2026-09-19", transport=relay.transport()))
    assert "revoked" in document["error"]


@pytest.mark.parametrize("bad", ["home/../other", "x?y=1", "a#b",
                                 "a b", "x/primary"])
async def test_a_calendar_id_cannot_inject_query_or_path(relay, bad):
    document = json.loads(await _list_events(calendar_id=bad,
                                             transport=relay.transport()))
    assert "not a usable calendar id" in document["error"]
    assert len(relay.requests) == 1  # the relay was asked, Google never was


async def test_a_plain_calendar_id_addresses_that_calendar(relay):
    document = json.loads(await _list_events(
        calendar_id="team@group.calendar.google.com",
        transport=relay.transport()))
    assert document["calendar_id"] == "team@group.calendar.google.com"
    assert "/calendars/team@group.calendar.google.com/events" in (
        relay.requests[1].url.path)


async def test_googles_error_body_never_reaches_the_prompt(relay):
    relay.google_status = 500
    relay.google_body = {"detail": "internal-detail-not-for-prompts"}
    document = json.loads(await _list_events(transport=relay.transport()))
    assert document["error"] == "Google Calendar returned HTTP 500."
    assert "internal-detail-not-for-prompts" not in json.dumps(document)
