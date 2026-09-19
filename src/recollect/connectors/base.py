"""Connector definitions and the host-side credential files.

A connector is an existing service Recollect can offer to connect instead of
building the capability from scratch. Two private files per connector sit in
a directory outside the repository: ``<id>.client.json``, written once by
whoever registers the OAuth client with the provider, and ``<id>.grant.json``,
written when the user allows the connection. Only this process reads them.

Keyword matching is deliberately dumb: it seeds the demo until the official
MCP registry (issue #28) can answer a capability gap with real candidates.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path


def connector_store() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return root / "recollect" / "connectors"


class Connector:
    """One connectable service; instances are module-level constants.

    ``tools`` names the worker tools this connector ships once connected; the
    continuation brief tells the resumed worker what it suddenly has.
    """

    id = ""
    name = ""
    description = ""
    #: Lower-case words naming the service inside a gap report.
    keywords: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    auth_url = ""
    token_url = ""
    #: Extra consent params a provider needs to hand out a refresh token.
    auth_params: dict[str, str] = {}
    #: Where the OAuth client registers its loopback redirect. A client JSON's
    #: ``redirect_port`` overrides it (0 asks the OS for a free port).
    redirect_port = 8723

    def matches(self, text: str) -> bool:
        lowered = text.lower()
        return any(word in lowered for word in self.keywords)

    async def connection(self, access_token: str, client) -> dict:
        """What worker tools need alongside the token (calendar id, zone)."""
        return {}


class UnknownConnector(ValueError):
    """No such connector is defined in this build."""


class NotConnected(ValueError):
    """The connector exists, but the user has not connected it."""


class ConnectorStore:
    """``<id>.client.json`` and ``<id>.grant.json`` under a private root."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, connector_id: str, kind: str) -> Path:
        return self._root / f"{connector_id}.{kind}.json"

    def client(self, connector_id: str) -> dict | None:
        try:
            return json.loads(
                self._path(connector_id, "client").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def connectable(self, connector_id: str) -> bool:
        client = self.client(connector_id)
        return bool(client and client.get("client_id") and client.get(
            "client_secret"))

    def grant(self, connector_id: str) -> dict | None:
        try:
            return json.loads(
                self._path(connector_id, "grant").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def save(self, connector_id: str, grant: dict) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(connector_id, "grant")
        path.write_text(json.dumps(grant), encoding="utf-8")
        # The refresh token lives here; keep the file owner-only where that
        # means anything (a no-op on Windows, where the store is per-user).
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    def forget(self, connector_id: str) -> bool:
        try:
            self._path(connector_id, "grant").unlink()
        except FileNotFoundError:
            return False
        return True

    def connected(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(path.name.removesuffix(".grant.json")
                      for path in self._root.glob("*.grant.json"))
