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
import re
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .subagent_tools.http_request import (
    http_request as _http_request,
)
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
    """Search the web and scholarly indexes (arXiv, OpenAlex, Crossref, Europe
    PMC, Semantic Scholar). Returns JSON: results (title, url, snippet, source)
    and errors (search legs that were unavailable). A snippet is not evidence:
    fetch the page before relying on it."""
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
async def web_fetch(
    url: str, max_chars: int = 4_000, view: Literal["article", "page"] = "article",
) -> str:
    """Fetch one public http/https page as text. view="article" (default)
    returns the main article; if a label or date is missing, retry once with
    view="page". Raise max_chars only if the result says it was truncated.
    Fetching the same URL and view again returns the same text."""
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
        return await _web_fetch(client, url, max_chars=max_chars, view=view)


#: A result blaming the toolset is a capability gap, not a finished request.
_MISSING_TOOL = re.compile(
    r"\b(?:tool|tools|tooling|toolset)\b[^.]{0,60}\b(?:does not|doesn't|do not|"
    r"don't|cannot|can't|only) support"
    r"|\b(?:no|none of my|without an?) (?:available |suitable )?tools?\b"
    r"[^.]{0,40}\b(?:can|could|that|capable|to)\b"
)


async def report_message(
    kind: Literal["accepted", "progress", "finding", "question", "blocked", "result"],
    text: Annotated[str, Field(min_length=1, max_length=4000)],
    revision: Annotated[int, Field(ge=1, le=1_000_000)],
    related_message_id: str = "",
    sources: list[str] | None = None,
    artifacts: list[str] | None = None,
) -> str:
    """Send one report to the main conversation. It sees only these reports, not
    your tool calls.
    - kind: accepted | progress | finding | question | blocked | result
    - text: up to 4000 characters. Summarize long files and put their paths in
      artifacts.
    - revision, related_message_id: copy exactly from the instruction you are
      answering.
    - sources: public URLs that support the text.
    - artifacts: relative /workspace paths of files the user asked for.
    """
    if kind not in {"accepted", "progress", "finding", "question", "blocked", "result"}:
        raise ValueError("unsupported report kind")
    if not text.strip() or len(text) > 4000:
        raise ValueError("report requires bounded text (1-4000 characters); "
                         "summarize long files and put their paths in artifacts")
    if not 1 <= revision <= 1_000_000:
        raise ValueError("revision must be between 1 and 1000000")
    if len(related_message_id) > 128:
        raise ValueError("related message ID is too long")
    for values in (sources or [], artifacts or []):
        if len(values) > 32 or any(len(value) > 2048 for value in values):
            raise ValueError("report references exceed the allowed size")
    if kind == "result" and _MISSING_TOOL.search(text.lower().replace("’", "'")):
        raise ValueError(
            "This result says no tool can do what was asked. That is not a result: "
            "send kind=blocked with the capability_gap block from "
            "recollect-reporting, so the missing capability can be built.")
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
