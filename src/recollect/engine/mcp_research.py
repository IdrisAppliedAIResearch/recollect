"""Recollect's web research tools, exposed as an MCP stdio server.

The opencode sandbox runs its research model in a separate process and
gives it the exact same ``web_search``/``web_fetch`` implementations the
legacy subagent loop uses, wrapped in the Model Context Protocol. The
process boundary is the point: the sandbox's model can reach the
research tools and nothing else of this machine, and ``web_fetch`` keeps
the SSRF guard that refuses local and private addresses.

Each call gets a fresh ``SearchRunState``: the cache and provider
pacing are per-call, exactly as they are per-run in the legacy loop, so
this long-lived process cannot accumulate state across delegations.
"""

from __future__ import annotations

import json

import httpx
from mcp.server.fastmcp import FastMCP

from .webtools import (
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
        timeout=httpx.Timeout(_SERVER_TIMEOUT_S, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": _UA},
    ) as client:
        return await _web_search(
            client, query, max_results=max_results, state=SearchRunState()
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
        timeout=httpx.Timeout(_SERVER_TIMEOUT_S, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": _UA},
    ) as client:
        return await _web_fetch(client, url, max_chars=max_chars)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
