"""User-run Google OAuth for the experiment's provider principals.

The user runs ``authorize`` personally in a browser; the agent never enters or
sees credential values. Worker and verifier use separate roles and scopes, so
the independent verifier is mechanically read-only. Tokens are stored outside
the repository and every sandbox mount; commands print only paths and scopes.

    uv run --no-sync python -m recollect.selfmod.google_auth authorize \
        --client-secret C:/path/client_secret.json --role worker
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

SCOPES = {
    "worker": "https://www.googleapis.com/auth/calendar.events",
    "verifier": "https://www.googleapis.com/auth/calendar.readonly",
}
GOOGLE_AUTH_ORIGIN = "https://accounts.google.com"
GOOGLE_TOKEN_ORIGIN = "https://oauth2.googleapis.com"


def default_store():
    return Path(os.environ["LOCALAPPDATA"]) / "recollect" / "selfmod-google"


def load_client(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    client = value.get("installed")
    if (type(client) is not dict or not client.get("client_id")
            or not client.get("client_secret")
            or not str(client.get("token_uri", "")).startswith(GOOGLE_TOKEN_ORIGIN)
            or not str(client.get("auth_uri", "")).startswith(GOOGLE_AUTH_ORIGIN)):
        raise ValueError("Use a Google 'Desktop app' OAuth client JSON")
    return client


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorization_url(client, redirect_uri, role, state, challenge):
    if role not in SCOPES:
        raise ValueError("Role must be worker or verifier")
    return client["auth_uri"] + "?" + urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect_uri,
        "response_type": "code", "scope": SCOPES[role], "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent",
    })


REPOSITORY = Path(__file__).resolve().parents[3]


def token_path(store, role):
    if role not in SCOPES:
        raise ValueError("Role must be worker or verifier")
    resolved = Path(store).resolve()
    if resolved == REPOSITORY or REPOSITORY in resolved.parents:
        raise ValueError("Credentials must be stored outside the repository")
    return resolved / f"{role}.json"


def store_refresh(store, role, client_path, token):
    if token.get("scope", "").split() != [SCOPES[role]] or not token.get(
            "refresh_token"):
        raise ValueError("Google did not grant exactly the requested scope")
    path = token_path(store, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "role": role, "scope": SCOPES[role],
        "client_secret_path": str(Path(client_path).absolute()),
        "refresh_token": token["refresh_token"],
    }), encoding="utf-8")
    os.replace(temporary, path)
    return path


class RefreshingCredential:
    """Broker credential source: refreshes a short-lived access token on demand."""

    def __init__(self, store, role, *, transport=None, clock=time.monotonic):
        self._path = token_path(store, role)
        self._role, self._clock = role, clock
        self._transport = transport
        self._token, self._expires = None, 0.0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            if self._token is None or self._clock() >= self._expires:
                record = json.loads(self._path.read_text(encoding="utf-8"))
                if record.get("role") != self._role or record.get("scope") != SCOPES[
                        self._role]:
                    raise ValueError("Stored credential role or scope changed")
                client = load_client(record["client_secret_path"])
                with httpx.Client(transport=self._transport, timeout=None,
                                  trust_env=False, follow_redirects=False) as http:
                    response = http.post(client["token_uri"], data={
                        "client_id": client["client_id"],
                        "client_secret": client["client_secret"],
                        "refresh_token": record["refresh_token"],
                        "grant_type": "refresh_token",
                    })
                response.raise_for_status()
                value = response.json()
                if value.get("scope", SCOPES[self._role]).split() != [
                        SCOPES[self._role]]:
                    raise ValueError("Refreshed token scope changed")
                self._token = value["access_token"]
                self._expires = self._clock() + max(0, int(value["expires_in"]) - 60)
            return self._token


def _receive_code(state):
    """One loopback redirect; state must match. Returns (redirect_uri, code)."""
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            if query.get("state") != [state] or "code" not in query:
                result["error"] = query.get("error", ["state_mismatch"])[0]
            else:
                result["code"] = query["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization received; you can close this tab.")

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    return server, result


def authorize(client_path, role, store):
    client = load_client(client_path)
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    server, result = _receive_code(state)
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    url = authorization_url(client, redirect, role, state, challenge)
    print("Open this URL in your browser and approve the requested scope:")
    print(url)
    webbrowser.open(url)
    server.handle_request()
    server.server_close()
    if "code" not in result:
        raise SystemExit("Authorization failed: " + result.get("error", "unknown"))
    with httpx.Client(timeout=None, trust_env=False, follow_redirects=False) as http:
        response = http.post(client["token_uri"], data={
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "code": result["code"], "code_verifier": verifier,
            "redirect_uri": redirect, "grant_type": "authorization_code",
        })
    response.raise_for_status()
    path = store_refresh(store, role, client_path, response.json())
    print(f"Stored {role} credential ({SCOPES[role]}) at {path}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m recollect.selfmod.google_auth")
    commands = parser.add_subparsers(dest="command", required=True)
    grant = commands.add_parser("authorize")
    grant.add_argument("--client-secret", required=True, type=Path)
    grant.add_argument("--role", required=True, choices=sorted(SCOPES))
    grant.add_argument("--store", type=Path, default=None)
    args = parser.parse_args(argv)
    authorize(args.client_secret, args.role, args.store or default_store())
    return 0


if __name__ == "__main__":
    sys.exit(main())
