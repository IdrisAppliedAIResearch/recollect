"""Recollect's web research tools, exposed as an MCP stdio server.

The opencode sandbox runs its research model in a separate process and
gives it the exact same ``web_search``/``web_fetch`` implementations the
legacy subagent loop uses, wrapped in the Model Context Protocol. The
process boundary is the point: the sandbox's model can reach the
research tools and nothing else of this machine, and ``web_fetch`` keeps
the SSRF guard that refuses local and private addresses.

Each call gets a fresh result cache. Provider pacing/cooldowns are shared
by the warm MCP process, so consecutive fresh delegations cannot burst the
same keyless service or immediately repeat a known rate-limited request.
"""

from __future__ import annotations

import json
import os
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP

from .webtools import (
    PublicWebTransport,
    SearchProviderState,
    SearchRunState,
)
from .webtools import (
    web_fetch as _web_fetch,
)
from .webtools import (
    web_search as _web_search,
)

#: A single leg is bounded, and the merged search is a parallel gather
#: over its legs, so this also bounds the whole call.
_SERVER_TIMEOUT_S = 30.0
_UA = "recollect-research/1.0 (local research agent)"

mcp = FastMCP("recollect_research")
_provider_state = SearchProviderState()


@mcp.tool()
async def web_search(query: str, max_results: int = 8) -> str:
    """Search the open web and scholarly indexes (arXiv, OpenAlex,
    Crossref, Europe PMC, Semantic Scholar). Returns a JSON document with
    a ranked "results" list (title, url, snippet, source tag) and an
    "errors" list recording any search leg that was unavailable."""
    if not query.strip():
        return json.dumps(
            {"tool": "web_search", "error": "missing 'query'"}, ensure_ascii=False
        )
    async with httpx.AsyncClient(
        transport=PublicWebTransport(),
        trust_env=False,
        timeout=httpx.Timeout(_SERVER_TIMEOUT_S, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": _UA},
    ) as client:
        return await _web_search(
            client,
            query,
            max_results=max_results,
            # Results remain call-local, while provider pacing survives in
            # the warm MCP process so a fresh OpenCode conversation cannot
            # accidentally burst a provider that the previous call just hit.
            state=SearchRunState(providers=_provider_state),
        )


@mcp.tool()
async def web_fetch(url: str, max_chars: int = 4_000) -> str:
    """Fetch one public http/https page and reduce it to its article
    text. Read a search result with this before relying on it."""
    if not url.strip():
        return json.dumps(
            {"tool": "web_fetch", "error": "missing 'url'"}, ensure_ascii=False
        )
    async with httpx.AsyncClient(
        transport=PublicWebTransport(),
        trust_env=False,
        timeout=httpx.Timeout(_SERVER_TIMEOUT_S, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": _UA},
    ) as client:
        return await _web_fetch(client, url, max_chars=max_chars)


async def report_message(
    kind: Literal["accepted", "progress", "finding", "question", "blocked", "result"],
    text: str,
    revision: int,
    related_message_id: str = "",
    sources: list[str] | None = None,
    artifacts: list[str] | None = None,
) -> str:
    """Send a concise message to the main conversation's task mailbox.

    Acknowledge each instruction revision with accepted before acting on it,
    copying its related_message_id exactly. Report useful findings and blockers
    as work proceeds. Cite public source URLs and relative workspace artifact
    paths. Tool execution history stays private. This receipt records a local
    report; the host separately persists it before delivering an update.
    """
    if kind not in {"accepted", "progress", "finding", "question", "blocked", "result"}:
        raise ValueError("unsupported report kind")
    if not text.strip() or len(text) > 4000 or not 1 <= revision <= 1_000_000:
        raise ValueError("report requires bounded text and a positive revision")
    if len(related_message_id) > 128:
        raise ValueError("related message ID is too long")
    for values in (sources or [], artifacts or []):
        if len(values) > 32 or any(len(value) > 2048 for value in values):
            raise ValueError("report references exceed the allowed size")
    return json.dumps({
        "kind": kind,
        "text": text.strip(),
        "revision": revision,
        "related_message_id": related_message_id,
        "sources": sources or [],
        "artifacts": artifacts or [],
    }, ensure_ascii=False)


if os.environ.get("RECOLLECT_TASK_REPORTING") == "1":
    mcp.tool()(report_message)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
