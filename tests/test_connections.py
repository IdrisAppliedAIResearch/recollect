"""Connected accounts reach worker tools as short-lived tokens, never as secrets."""

import json
from pathlib import Path

import httpx

from recollect.connections import (
    CALENDAR_LIST_URL,
    GUIDE,
    TOKEN_URL,
    ConnectionService,
    GoogleAccount,
)
from recollect.engine.sandbox.configgen import build_config
from recollect.selfmod.tests_first import message


def store(tmp_path):
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {
        "client_id": "id", "client_secret": "client-secret"}}))
    for role, scope in (("worker", "calendar.events"), ("verifier", "readonly")):
        (tmp_path / f"{role}.json").write_text(json.dumps({
            "role": role, "scope": scope, "refresh_token": f"refresh-{role}",
            "client_secret_path": str(client)}))
    return tmp_path


class Google:
    def __init__(self):
        self.refreshes, self.lists = [], 0

    def __call__(self, request):
        if str(request.url) == TOKEN_URL:
            form = dict(httpx.QueryParams(request.content.decode()))
            self.refreshes.append(form["refresh_token"])
            role = form["refresh_token"].removeprefix("refresh-")
            return httpx.Response(200, json={"access_token": f"access-{role}",
                                             "expires_in": 3599, "scope": role})
        if str(request.url).startswith(CALENDAR_LIST_URL):
            self.lists += 1
            assert request.headers["authorization"] == "Bearer access-verifier"
            return httpx.Response(200, json={"items": [
                {"summary": "Personal", "id": "me"},
                {"summary": "recollect-selfmod-test", "id": "test-calendar",
                 "timeZone": "America/Chicago"}]})
        return httpx.Response(404)


async def test_worker_gets_a_fresh_token_and_the_test_calendar(tmp_path):
    google = Google()
    account = GoogleAccount(store(tmp_path), transport=httpx.MockTransport(google))
    assert GoogleAccount.available(tmp_path)
    first = await account.connection()
    second = await account.connection()
    assert first["access_token"] == "access-worker"
    assert first["calendar_id"] == "test-calendar"
    assert first["calendar_time_zone"] == "America/Chicago"
    # Tokens and the calendar lookup are cached; the read-only role finds it.
    assert second["access_token"] == "access-worker"
    assert sorted(google.refreshes) == ["refresh-verifier", "refresh-worker"]
    assert google.lists == 1
    assert "refresh-worker" not in json.dumps(first)
    await account.close()


async def test_the_service_answers_only_holders_of_its_key(tmp_path):
    account = GoogleAccount(store(tmp_path), transport=httpx.MockTransport(Google()))
    service = ConnectionService(account)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app),
                                 base_url="http://service") as client:
        assert (await client.get("/google")).status_code == 401
        wrong = await client.get("/google", headers={"Authorization": "Bearer no"})
        assert wrong.status_code == 401
        ok = await client.get("/google",
                              headers={"Authorization": f"Bearer {service.key}"})
        assert ok.status_code == 200 and ok.json()["calendar_id"] == "test-calendar"
    await account.close()


async def test_a_google_failure_is_an_error_not_a_crash(tmp_path):
    account = GoogleAccount(store(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": "invalid_grant"})))
    service = ConnectionService(account)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app),
                                 base_url="http://service") as client:
        response = await client.get(
            "/google", headers={"Authorization": f"Bearer {service.key}"})
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]
    assert not GoogleAccount.available(Path(tmp_path / "missing"))
    await account.close()


def test_only_the_worker_tool_process_receives_the_connection():
    config = build_config(Path("."), base_url="http://m/v1", model="q", api_key="k",
                          steps=4, continuous=True,
                          connections=("http://host.docker.internal:9", "key"))
    environment = config["mcp"]["recollect_research"]["environment"]
    assert environment["RECOLLECT_CONNECTIONS_URL"] == "http://host.docker.internal:9"
    assert environment["RECOLLECT_CONNECTIONS_TOKEN"] == "key"
    assert "key" not in json.dumps(config["agent"])
    plain = build_config(Path("."), base_url="http://m/v1", model="q", api_key="k",
                         steps=4, continuous=True)
    assert "RECOLLECT_CONNECTIONS_URL" not in plain["mcp"]["recollect_research"][
        "environment"]


def test_test_authors_learn_the_connection_convention_only_when_connected():
    from recollect.selfmod.subagent_tree import baseline, change_policy

    tree = baseline(Path(__file__).resolve().parents[1])
    gap = {"missing_capability": "calendar"}
    connected = message("book it", gap, tree, change_policy(tree), connections=GUIDE)
    assert "<connected_accounts>" in connected
    assert "RECOLLECT_CONNECTIONS_URL" in connected
    assert "<connected_accounts>" not in message("book it", gap, tree,
                                                 change_policy(tree))
