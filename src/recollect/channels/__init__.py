"""Off-device delivery seam: the host sends, at delivery time.

A tool answers a call; it cannot be proactive, and delivery happens when
a scheduled job fires — possibly with no worker alive. So sending lives
here, host-side, beside the deliverer that composes it: the on-device
Notice always posts first, then this hub fans the text out to the named
channel, or the default, or every configured one.

Channel configuration is user-dropped credential files — the
``connectors`` pattern: ``%LOCALAPPDATA%/recollect/channels/<name>.json``,
outside the repository. The agent configures and tests channels over the
relay; it never sends from the sandbox and never holds the secrets.

Adding a channel is one module exposing ``async def send(config, text)``
and raising :class:`ChannelError` with a user-safe reason.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from . import ntfy
from .error import ChannelError  # noqa: F401 - re-exported, the seam's error


def channel_store() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return root / "recollect" / "channels"


#: Channel name -> module. A registry lookup, never an import spiral:
#: channels stay leaf modules.
REGISTRY = {"ntfy": ntfy}


class ChannelHub:
    """What is configured, and one send at a time, from credential files."""

    def __init__(self, store: str | Path | None = None,
                 registry: dict | None = None) -> None:
        self._store = Path(store) if store is not None else channel_store()
        self._registry = dict(REGISTRY if registry is None else registry)

    def _config(self, name: str) -> dict | None:
        """The parsed config file, or None when absent or unusable."""
        try:
            raw = json.loads((self._store / f"{name}.json").read_text(
                encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def configured(self) -> list[dict]:
        """[{name, default}] — never the config values: a topic URL is a
        credential, and this list reaches worker transcripts."""
        found = []
        for name in sorted(self._registry):
            config = self._config(name)
            if config is not None:
                found.append({"name": name,
                              "default": bool(config.get("default"))})
        return found

    def targets(self, channel: str = "") -> list[str]:
        """Where a notice goes: the named channel, else the default ones,
        else every configured one. ``ValueError`` for a named channel that
        is not configured — the reason is recorded on the failed job."""
        if channel:
            # A named target comes from a job payload, which is not trusted
            # input: only registry slugs may reach the store path. Anything
            # else is answered as not configured — the same words a missing
            # file gets, so the answer cannot probe the filesystem.
            if channel not in self._registry:
                raise ValueError(f"channel {channel!r} is not configured")
            if self._config(channel) is None:
                raise ValueError(f"channel {channel!r} is not configured")
            return [channel]
        listed = self.configured()
        defaults = [entry["name"] for entry in listed if entry["default"]]
        return defaults or [entry["name"] for entry in listed]

    async def send(self, name: str, text: str) -> None:
        module = self._registry.get(name)
        if module is None:
            raise ChannelError(f"no such channel: {name!r}")
        config = await asyncio.to_thread(self._config, name)
        if config is None:
            raise ChannelError(f"channel {name!r} is not configured")
        await module.send(config, text)
