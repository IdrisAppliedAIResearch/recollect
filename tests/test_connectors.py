"""Connectors: gap lookup, one-word consent, host-held credentials, no leaks."""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from recollect.connectors import (
    ConnectorManager,
    ConnectorStore,
    GoogleCalendar,
    NotConnected,
    UnknownConnector,
)
from recollect.connectors.google_calendar import CALENDAR_LIST_URL
from recollect.connectors.google_calendar import TOKEN_URL as GOOGLE_TOKEN_URL

CLIENT = {"client_id": "client-id", "client_secret": "client-secret",
          "redirect_port": 0}
CALENDAR_GAP = {"missing_capability": "add an event to the user's calendar",
                "modification_request": "write to Google Calendar"}


class Provider:
    """Google's token endpoint and primary-calendar lookup, without Google."""

    def __init__(self):
        self.exchanges, self.refreshes = [], 0

    def __call__(self, request):
        if str(request.url) == GOOGLE_TOKEN_URL:
            form = dict(httpx.QueryParams(request.content.decode()))
            if form["grant_type"] == "authorization_code":
                assert form["code_verifier"], "PKCE verifier must be sent"
                self.exchanges.append(form)
                return httpx.Response(200, json={
                    "access_token": "access-1", "refresh_token": "refresh-1",
                    "expires_in": 3599, "scope": "calendar.events"})
            self.refreshes += 1
            assert form["refresh_token"] == "refresh-1"
            return httpx.Response(200, json={
                "access_token": f"access-{self.refreshes + 1}",
                "expires_in": 3599, "scope": "calendar.events"})
        if str(request.url) == f"{CALENDAR_LIST_URL}/primary":
            assert request.headers["authorization"].startswith("Bearer access-")
            return httpx.Response(200, json={"id": "primary-calendar",
                                             "timeZone": "America/Chicago"})
        return httpx.Response(404)


def configured_store(tmp_path):
    store = ConnectorStore(tmp_path)
    (tmp_path / "google_calendar.client.json").write_text(json.dumps(CLIENT))
    return store


def consenting_browser(provider_state):
    """The user's browser: reads the consent URL, clicks Allow at the provider."""

    async def browser(url):
        query = parse_qs(urlparse(url).query)
        provider_state["url"] = query
        async with httpx.AsyncClient() as client:
            await client.get(query["redirect_uri"][0],
                             params={"code": "the-code",
                                     "state": query["state"][0]})

    return browser


def manager(store, *, transport, browser=None, timeout=10.0):
    return ConnectorManager(store, transport=transport, browser=browser,
                            callback_timeout=timeout)


async def test_the_store_keeps_grants_until_forgotten(tmp_path):
    store = ConnectorStore(tmp_path)
    assert store.grant("google_calendar") is None and not store.connected()
    store.save("google_calendar", {"refresh_token": "r", "connected_at": "x"})
    assert store.grant("google_calendar")["refresh_token"] == "r"
    assert store.connected() == ["google_calendar"]
    assert store.forget("google_calendar") and not store.connected()
    assert not store.forget("google_calendar")


def test_a_gap_matches_only_a_configured_service(tmp_path):
    empty = ConnectorManager(ConnectorStore(tmp_path))
    assert empty.find(CALENDAR_GAP) is None
    store = configured_store(tmp_path)
    value = ConnectorManager(store)
    assert isinstance(value.find(CALENDAR_GAP), GoogleCalendar)
    assert value.find({"missing_capability": "read a PDF"}) is None
    store.save("google_calendar", {"refresh_token": "r", "connected_at": "x"})
    assert value.find(CALENDAR_GAP) is None  # already connected: nothing to offer


async def test_connecting_consent_on_the_provider_tab_stores_the_grant(tmp_path):
    seen = {}
    value = manager(configured_store(tmp_path),
                    transport=httpx.MockTransport(Provider()),
                    browser=consenting_browser(seen))
    entry = await value.connect("google_calendar")
    query = seen["url"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["scope"] == ["https://www.googleapis.com/auth/calendar.events"]
    assert query["redirect_uri"][0].startswith("http://127.0.0.1:")
    assert entry["connected"] and entry["connector_id"] == "google_calendar"
    with pytest.raises(ValueError, match="already connected"):
        await value.connect("google_calendar")
    await value.close()


async def test_worker_access_is_short_lived_and_secret_free(tmp_path):
    provider = Provider()
    value = manager(configured_store(tmp_path),
                    transport=httpx.MockTransport(provider),
                    browser=consenting_browser({}))
    await value.connect("google_calendar")
    first = await value.connection("google_calendar")
    assert first["access_token"] == "access-1"  # the exchanged token, cached
    assert first["calendar_id"] == "primary-calendar"
    assert first["calendar_time_zone"] == "America/Chicago"
    value._tokens.clear()
    second = await value.connection("google_calendar")
    assert second["access_token"] == "access-2" and provider.refreshes == 1
    assert "refresh" not in json.dumps(second)
    assert "client-secret" not in json.dumps(value.status())
    await value.close()


async def test_a_denied_or_stalled_sign_in_fails_the_connect_only(tmp_path):
    async def denied(url):
        query = parse_qs(urlparse(url).query)
        async with httpx.AsyncClient() as client:
            await client.get(query["redirect_uri"][0],
                             params={"error": "access_denied",
                                     "state": query["state"][0]})

    value = manager(configured_store(tmp_path),
                    transport=httpx.MockTransport(Provider()), browser=denied)
    with pytest.raises(RuntimeError, match="access_denied"):
        await value.connect("google_calendar")
    assert not value.connected()

    async def walking_away(url):
        pass

    stalled = manager(configured_store(tmp_path),
                      transport=httpx.MockTransport(Provider()),
                      browser=walking_away, timeout=0.2)
    with pytest.raises(RuntimeError, match="not completed in time"):
        await stalled.connect("google_calendar")
    await value.close()
    await stalled.close()


async def test_mismatched_state_is_ignored_until_the_real_redirect(tmp_path):
    async def stray_first(url):
        query = parse_qs(urlparse(url).query)
        async with httpx.AsyncClient() as client:
            await client.get(query["redirect_uri"][0],
                             params={"code": "phishing", "state": "not-it"})
            await client.get(query["redirect_uri"][0],
                             params={"code": "the-code",
                                     "state": query["state"][0]})

    value = manager(configured_store(tmp_path),
                    transport=httpx.MockTransport(Provider()),
                    browser=stray_first)
    entry = await value.connect("google_calendar")
    assert entry["connected"]
    await value.close()


async def test_nothing_serves_an_unconnected_or_unknown_connector(tmp_path):
    value = manager(configured_store(tmp_path),
                    transport=httpx.MockTransport(Provider()))
    with pytest.raises(NotConnected):
        await value.connection("google_calendar")
    with pytest.raises(UnknownConnector):
        await value.connection("nope")
    with pytest.raises(UnknownConnector):
        await value.connect("nope")
    unconfigured = ConnectorManager(ConnectorStore(tmp_path / "empty"))
    with pytest.raises(ValueError, match="no OAuth client"):
        await unconfigured.connect("google_calendar")
    assert [entry["connectable"] for entry in unconfigured.status()] == [False]
    await value.close()
    await unconfigured.close()


async def test_a_broken_redirect_port_is_reported_plainly(tmp_path):
    store = ConnectorStore(tmp_path)
    (tmp_path / "google_calendar.client.json").write_text(json.dumps(
        {"client_id": "c", "client_secret": "s", "redirect_port": "8723a"}))
    value = manager(store, transport=httpx.MockTransport(Provider()))
    with pytest.raises(ValueError, match="redirect_port"):
        await value.connect("google_calendar")
    assert not value.connected()
    await value.close()


async def test_disconnect_takes_effect_at_once(tmp_path):
    value = manager(configured_store(tmp_path),
                    transport=httpx.MockTransport(Provider()),
                    browser=consenting_browser({}))
    await value.connect("google_calendar")
    assert value.disconnect("google_calendar")
    assert not value.disconnect("google_calendar")
    with pytest.raises(NotConnected):
        await value.connection("google_calendar")
    await value.close()
