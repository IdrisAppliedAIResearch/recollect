"""Connectors: an existing service is tried before a capability is built.

Issue #28. When a worker reports a missing capability, the first question is
whether a connector already covers it; the self-modification build becomes
the fallback. A connector's credentials stay on the host in a private
directory outside the repository, and worker tools reach them through the
keyed loopback connection service exactly like connected accounts:
short-lived access, tool process only, nothing in prompts or transcripts.

Connecting is consent, not plumbing: the app never opens a sign-in page on
its own. The user's click (or spoken yes) starts the flow, and only the
provider's own tab collects the Allow.
"""

from .base import (
    Connector,
    ConnectorStore,
    NotConnected,
    UnknownConnector,
    connector_store,
)
from .google_calendar import GoogleCalendar
from .manager import CONNECTORS, ConnectorManager

__all__ = [
    "CONNECTORS",
    "Connector",
    "ConnectorManager",
    "ConnectorStore",
    "GoogleCalendar",
    "NotConnected",
    "UnknownConnector",
    "connector_store",
]
