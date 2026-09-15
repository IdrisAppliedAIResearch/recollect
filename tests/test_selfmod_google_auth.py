"""User-run OAuth helper: scopes, PKCE, storage location and refresh; no network."""

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from recollect.selfmod import google_auth

CLIENT = {"installed": {
    "client_id": "client.apps.googleusercontent.com", "client_secret": "fixture-secret",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}}


def client_file(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps(CLIENT))
    return path


def test_authorization_url_requests_one_role_scope_with_pkce():
    verifier, challenge = google_auth.pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected
    url = google_auth.authorization_url(CLIENT["installed"], "http://127.0.0.1:5000",
                                        "verifier", "state-1", challenge)
    query = parse_qs(urlsplit(url).query)
    assert query["scope"] == [google_auth.SCOPES["verifier"]]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["state-1"]
    with pytest.raises(ValueError):
        google_auth.authorization_url(CLIENT["installed"], "x", "admin", "s", "c")


def test_only_desktop_google_clients_are_accepted(tmp_path):
    bad = tmp_path / "web.json"
    bad.write_text(json.dumps({"web": CLIENT["installed"]}))
    with pytest.raises(ValueError, match="Desktop"):
        google_auth.load_client(bad)


def test_refresh_token_is_stored_outside_repo_with_exact_scope(tmp_path):
    store = tmp_path / "store"
    client = client_file(tmp_path)
    with pytest.raises(ValueError, match="scope"):
        google_auth.store_refresh(store, "verifier", client, {
            "refresh_token": "r", "scope": google_auth.SCOPES["worker"]})
    path = google_auth.store_refresh(store, "verifier", client, {
        "refresh_token": "refresh-fixture", "scope": google_auth.SCOPES["verifier"]})
    record = json.loads(path.read_text())
    assert record["role"] == "verifier" and record["refresh_token"] == "refresh-fixture"
    assert path.name == "verifier.json"


def test_credential_refreshes_on_expiry_and_rejects_scope_drift(tmp_path):
    store = tmp_path / "store"
    client = client_file(tmp_path)
    google_auth.store_refresh(store, "worker", client, {
        "refresh_token": "refresh-fixture", "scope": google_auth.SCOPES["worker"]})
    now, calls = [0.0], []

    def token(request):
        calls.append(dict(parse_qs(request.content.decode())))
        return httpx.Response(200, json={"access_token": f"access-{len(calls)}",
                                         "expires_in": 3600,
                                         "scope": google_auth.SCOPES["worker"]})

    credential = google_auth.RefreshingCredential(
        store, "worker", transport=httpx.MockTransport(token), clock=lambda: now[0])
    assert credential() == "access-1" and credential() == "access-1"
    now[0] = 3600.0
    assert credential() == "access-2"
    assert calls[0]["grant_type"] == ["refresh_token"]

    def drifted(request):
        return httpx.Response(200, json={"access_token": "x", "expires_in": 3600,
                                         "scope": google_auth.SCOPES["verifier"]})

    other = google_auth.RefreshingCredential(
        store, "worker", transport=httpx.MockTransport(drifted), clock=lambda: 0.0)
    with pytest.raises(ValueError, match="scope"):
        other()


def test_credentials_are_never_stored_inside_the_repository():
    with pytest.raises(ValueError, match="outside the repository"):
        google_auth.token_path(google_auth.REPOSITORY / ".agent", "worker")
