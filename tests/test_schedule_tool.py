"""The worker's timer tools speak to the relay, and to nothing else."""

import json

import httpx
import pytest

from recollect.engine.subagent_tools import scheduling


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setenv(scheduling.URL_VARIABLE, "http://relay:1")
    monkeypatch.setenv(scheduling.KEY_VARIABLE, "key-1")


def _calls(handler):
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)
    return seen, httpx.MockTransport(record)


async def test_booking_posts_the_deliverers_contract(relay):
    def handler(request):
        return httpx.Response(200, json={"job_id": "ab" * 16,
                                         "status": "pending"})
    seen, transport = _calls(handler)
    doc = json.loads(await scheduling._book(
        "2026-09-20T08:00:00+00:00", " call mom ", "s1", "t1",
        transport=transport))
    assert doc["result"]["status"] == "pending"
    body = json.loads(seen[0].content)
    assert body["due_at"] == "2026-09-20T08:00:00+00:00"
    assert body["payload"] == {"session_id": "s1", "task_id": "t1",
                               "text": "call mom"}
    assert seen[0].url.path == "/schedules"
    assert seen[0].headers["authorization"] == "Bearer key-1"


async def test_a_named_channel_travels_and_an_empty_one_does_not(relay):
    def handler(request):
        return httpx.Response(200, json={"job_id": "ab" * 16})
    seen, transport = _calls(handler)
    await scheduling._book("2026-09-20T08:00:00+00:00", "x", "s1", "t1",
                           transport=transport)
    assert "channel" not in json.loads(seen[0].content)["payload"]
    await scheduling._book("2026-09-20T08:00:00+00:00", "x", "s1", "t1",
                           channel="ntfy", transport=transport)
    assert json.loads(seen[1].content)["payload"]["channel"] == "ntfy"


async def test_the_relays_own_reason_is_forwarded_to_the_model(relay):
    def handler(request):
        return httpx.Response(400, json={
            "detail": "A job needs a timezone-aware due_at"})
    doc = json.loads(await scheduling._book(
        "2026-09-20T08:00:00", "x", "s1", "t1",
        transport=httpx.MockTransport(handler)))
    assert "timezone-aware" in doc["error"]


async def test_invented_or_missing_identity_is_refused_without_network(
        relay):
    def never(request):  # pragma: no cover - the call must not happen
        raise AssertionError("no network before identity is checked")
    transport = httpx.MockTransport(never)
    doc = json.loads(await scheduling._book("2026-09-20T08:00:00+00:00",
                                            "x", "", "", transport=transport))
    assert "recollect identity" in doc["error"]
    bad = json.loads(await scheduling._book("2026-09-20T08:00:00+00:00",
                                            "x", "s/../x", "t1",
                                            transport=transport))
    assert "recollect identity" in bad["error"]


async def test_no_relay_env_says_scheduling_is_offline(monkeypatch):
    monkeypatch.delenv(scheduling.URL_VARIABLE, raising=False)
    monkeypatch.delenv(scheduling.KEY_VARIABLE, raising=False)
    doc = json.loads(await scheduling._list())
    assert "offline" in doc["error"]


async def test_cancel_validates_the_job_id_and_hits_the_relay(relay):
    def handler(request):
        assert request.method == "DELETE"
        return httpx.Response(200, json={"status": "canceled"})
    seen, transport = _calls(handler)
    doc = json.loads(await scheduling._cancel(
        "0123456789abcdef", transport=transport))
    assert doc["result"]["status"] == "canceled"
    assert seen[0].url.path == "/schedules/0123456789abcdef"
    bad = json.loads(await scheduling._cancel("../../etc",
                                              transport=transport))
    assert "hex id" in bad["error"]


async def test_a_refusal_without_a_readable_reason_stays_status_only(relay):
    def handler(request):
        return httpx.Response(418, text="not a json error page")
    doc = json.loads(await scheduling._list(
        transport=httpx.MockTransport(handler)))
    assert doc["error"] == ("The connection service refused the request "
                            "(HTTP 418).")


async def test_a_non_json_2xx_still_answers_as_a_document(relay):
    def handler(request):
        return httpx.Response(200, content=b"<html>proxy</html>")
    doc = json.loads(await scheduling._list(
        transport=httpx.MockTransport(handler)))
    assert "result" not in doc
    assert "without a result" in doc["error"]
