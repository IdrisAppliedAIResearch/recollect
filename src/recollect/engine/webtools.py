"""Keyless web access for the subagent.

Two tools, six search legs, zero API keys. The subagent is the only
caller, and everything comes back to it as an *observation string*: a JSON
document on success, a JSON document describing the failure otherwise.
Failures are data for the model to adapt to, not exceptions that kill a
run, because a dead run is strictly worse than a run that learned one
source is down.

The search legs are deliberately asymmetric in availability. arXiv,
OpenAlex, Crossref, Europe PMC and DuckDuckGo provide the keyless baseline;
Semantic Scholar throttles anonymous traffic (its own probe returned HTTP
429 on the first keyless call from this machine), so it is best-effort and
its absence is never an error condition.

Fetched pages are untrusted content. Nothing here executes or obeys what a
page says; extraction is a text reduction (trafilatura) only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import trafilatura

#: Scholarly legs come first in a merged result list, so a research task
#: gets its papers before its news articles.
_SCHOLARLY_LIMIT = 3

#: Do not reduce a multi-megabyte page for the model. A cap keeps a hostile
#: "fetch this URL" bounded as much as the timeout does.
_MAX_RESPONSE_BYTES = 2_000_000

_ARXIV_API = "https://export.arxiv.org/api/query"
_OPENALEX_API = "https://api.openalex.org/works"
_CROSSREF_API = "https://api.crossref.org/works"
_EUROPE_PMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_SEMANTIC_SCHOLAR_API = (
    "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
)

_SEARCH_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# arXiv explicitly asks API clients to leave three seconds between calls.
# The others are deliberately conservative: this is an interactive local
# agent, not a bulk harvester, and one useful result beats a burst of 429s.
_PROVIDER_INTERVALS = {
    "arxiv": 3.0,
    "openalex": 0.25,
    "crossref": 1.0,
    "europe_pmc": 0.5,
    "semantic_scholar": 1.0,
    "web": 1.0,
}

_ATOM = "{http://www.w3.org/2005/Atom}"


class _RateLimited(RuntimeError):
    def __init__(self, provider: str, retry_after: float | None) -> None:
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(f"{provider} returned HTTP 429")


class _ProviderUnavailable(RuntimeError):
    """A provider cannot currently serve usable search responses."""


@dataclass
class SearchProviderState:
    """Pacing shared by calls using one warm provider process."""

    next_allowed: dict[str, float] = field(default_factory=dict)
    cooldown_until: dict[str, float] = field(default_factory=dict)
    disabled: dict[str, str] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    async def before_request(self, provider: str) -> None:
        # A model can emit parallel calls even though generation itself has
        # one server slot. Serialize each provider's reservation so two fresh
        # calls cannot both observe the same open pacing window.
        async with self.locks.setdefault(provider, asyncio.Lock()):
            if provider in self.disabled:
                raise _ProviderUnavailable(self.disabled[provider])

            now = time.monotonic()
            cooldown = self.cooldown_until.get(provider, 0.0)
            if cooldown > now:
                remaining = max(1, round(cooldown - now))
                raise _ProviderUnavailable(f"cooling down for {remaining}s")

            delay = self.next_allowed.get(provider, 0.0) - now
            if delay > 0:
                await asyncio.sleep(delay)
            now = time.monotonic()
            cooldown = self.cooldown_until.get(provider, 0.0)
            if provider in self.disabled:
                raise _ProviderUnavailable(self.disabled[provider])
            if cooldown > now:
                remaining = max(1, round(cooldown - now))
                raise _ProviderUnavailable(f"cooling down for {remaining}s")
            self.next_allowed[provider] = now + _PROVIDER_INTERVALS[provider]

    def rate_limited(
        self, provider: str, retry_after: float | None
    ) -> str:
        if provider == "semantic_scholar":
            reason = "disabled for this run after HTTP 429"
            self.disabled[provider] = reason
            return reason

        seconds = max(15.0, retry_after or 30.0)
        self.cooldown_until[provider] = time.monotonic() + seconds
        return f"cooling down for {round(seconds)}s after HTTP 429"

    def timed_out(self, provider: str) -> None:
        self.cooldown_until[provider] = time.monotonic() + 15.0


@dataclass
class SearchRunState:
    """Per-research-run result cache over a shareable pacing state."""

    cache: dict[tuple[str, int], str] = field(default_factory=dict)
    providers: SearchProviderState = field(default_factory=SearchProviderState)

    async def before_request(self, provider: str) -> None:
        await self.providers.before_request(provider)

    def rate_limited(self, provider: str, retry_after: float | None) -> str:
        return self.providers.rate_limited(provider, retry_after)

    def timed_out(self, provider: str) -> None:
        self.providers.timed_out(provider)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "").strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _raise_for_status(response: httpx.Response, provider: str) -> None:
    if response.status_code == 429:
        raise _RateLimited(provider, _retry_after(response))
    response.raise_for_status()


# ---------------------------------------------------------------------------
# The search legs
# ---------------------------------------------------------------------------


class _DDGParser(HTMLParser):
    """Collect (title, url, snippet) triples from the html.duckduckgo.com
    result page.

    A result's title link carries class ``result__a`` and its snippet
    carries ``result__snippet``. Result links hide the real URL behind a
    ``/l/?uddg=`` redirect, so it is decoded rather than passed through.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str, str]] = []
        self._href = ""
        self._title: list[str] = []
        self._snippet: list[str] = []
        self._capture: str | None = None
        self.no_results = False
        self.challenged = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if any(name.startswith("no-results") or name == "result--no-result"
               for name in classes):
            self.no_results = True
        if attributes.get("id") == "challenge-form" or any(
            name.startswith("anomaly-modal") for name in classes
        ):
            self.challenged = True
        if "result__a" in classes or "result-link" in classes:
            self._flush()
            self._href = _ddg_url(attrs)
            self._capture = "title"
        elif "result__snippet" in classes or "result-snippet" in classes:
            self._capture = "snippet"

    def handle_endtag(self, tag):
        if tag in ("a", "span", "div") and self._capture:
            self._capture = None

    def handle_data(self, data):
        if self._capture == "title":
            self._title.append(data)
        elif self._capture == "snippet":
            self._snippet.append(data)

    def _flush(self) -> None:
        if self._href:
            self.items.append(
                (
                    " ".join(self._title).strip(),
                    self._href,
                    " ".join(self._snippet).strip(),
                )
            )
        self._href = ""
        self._title = []
        self._snippet = []

    def close(self) -> None:
        self._flush()
        super().close()


def _ddg_url(attrs: list[tuple[str, str | None]]) -> str:
    """Resolve a result link: decode the ``/l/?uddg=`` redirect wrapper."""
    href = (dict(attrs).get("href") or "").strip()
    if href.startswith("/l/"):
        href = "https://duckduckgo.com" + href
    elif href.startswith("//"):
        href = "https:" + href
    try:
        parts = urlsplit(href)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return ""
        if parts.hostname in {
            "duckduckgo.com", "www.duckduckgo.com",
            "html.duckduckgo.com", "lite.duckduckgo.com",
        } and parts.path.rstrip("/") == "/l":
            href = parse_qs(parts.query).get("uddg", [""])[0]
            parts = urlsplit(href)
        return href if parts.scheme in {"http", "https"} and parts.hostname else ""
    except ValueError:
        return ""


async def _duckduckgo(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[dict]:
    last_error: Exception | None = None
    for endpoint in (
        "https://html.duckduckgo.com/html/",
        "https://lite.duckduckgo.com/lite/",
    ):
        try:
            response = await client.get(
                endpoint,
                params={"q": query},
                timeout=_SEARCH_TIMEOUT,
                follow_redirects=False,
            )
            _raise_for_status(response, "web")
        except (httpx.HTTPError, _RateLimited) as error:
            last_error = error
            continue
        parser = _DDGParser()
        parser.feed(response.text)
        parser.close()
        if parser.challenged or response.status_code == 202:
            last_error = _ProviderUnavailable(
                "DuckDuckGo returned a verification challenge; "
                "general web search is unavailable"
            )
            continue
        results = []
        for title, url, snippet in parser.items[:max_results]:
            entry: dict = {"source": "web", "title": title, "url": url}
            if snippet:
                entry["snippet"] = snippet[:300]
            results.append(entry)
        if results:
            return results
        if parser.no_results:
            return []
        last_error = _ProviderUnavailable(
            "DuckDuckGo returned an unrecognized search page; "
            "general web search is unavailable"
        )
    if last_error is not None:
        raise last_error
    return []


async def _arxiv(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[dict]:
    response = await client.get(
        _ARXIV_API,
        params={
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max_results,
        },
        timeout=_SEARCH_TIMEOUT,
    )
    _raise_for_status(response, "arxiv")
    root = ElementTree.fromstring(response.text)
    results = []
    for entry in root.findall(f"{_ATOM}entry"):
        title = " ".join((entry.findtext(f"{_ATOM}title") or "").split())
        summary = " ".join((entry.findtext(f"{_ATOM}summary") or "").split())
        url = ""
        for link in entry.findall(f"{_ATOM}link"):
            if link.get("rel") in (None, "alternate"):
                url = link.get("href") or ""
                break
        if not title or not url:
            continue
        results.append(
            {
                "source": "arxiv",
                "title": title,
                "url": url,
                "snippet": summary[:300],
                "year": (entry.findtext(f"{_ATOM}published") or "")[:4] or None,
            }
        )
    return results


def _openalex_abstract(index: dict | None) -> str:
    if not isinstance(index, dict):
        return ""
    positions = [position for values in index.values() for position in values]
    if not positions:
        return ""
    words = [""] * (max(positions) + 1)
    for word, indexes in index.items():
        for position in indexes:
            if 0 <= position < len(words):
                words[position] = word
    return " ".join(words).strip()


async def _openalex(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[dict]:
    response = await client.get(
        _OPENALEX_API,
        params={
            "search": query,
            "per_page": max_results,
            "select": (
                "id,display_name,publication_year,doi,primary_location,"
                "abstract_inverted_index"
            ),
        },
        timeout=_SEARCH_TIMEOUT,
    )
    _raise_for_status(response, "openalex")
    results = []
    for work in (response.json() or {}).get("results") or []:
        location = work.get("primary_location") or {}
        url = (
            work.get("doi")
            or location.get("landing_page_url")
            or work.get("id")
            or ""
        )
        title = work.get("display_name") or ""
        if not title or not url:
            continue
        entry: dict = {"source": "openalex", "title": title, "url": url}
        abstract = _openalex_abstract(work.get("abstract_inverted_index"))
        if abstract:
            entry["snippet"] = abstract[:300]
        if work.get("publication_year"):
            entry["year"] = int(work["publication_year"])
        results.append(entry)
    return results


def _crossref_year(item: dict) -> int | None:
    for key in ("published-print", "published-online", "published", "created"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            return int(parts[0][0])
    return None


async def _crossref(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[dict]:
    response = await client.get(
        _CROSSREF_API,
        params={
            "query.bibliographic": query,
            "rows": max_results,
            "select": (
                "DOI,title,abstract,published,published-print,"
                "published-online,created,URL"
            ),
        },
        timeout=_SEARCH_TIMEOUT,
    )
    _raise_for_status(response, "crossref")
    results = []
    for item in ((response.json() or {}).get("message") or {}).get("items") or []:
        titles = item.get("title") or []
        title = titles[0] if titles else ""
        doi = item.get("DOI") or ""
        url = item.get("URL") or (f"https://doi.org/{doi}" if doi else "")
        if not title or not url:
            continue
        entry: dict = {"source": "crossref", "title": title, "url": url}
        abstract = _naive_text(item.get("abstract") or "")
        if abstract:
            entry["snippet"] = abstract[:300]
        year = _crossref_year(item)
        if year:
            entry["year"] = year
        results.append(entry)
    return results


async def _europe_pmc(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[dict]:
    response = await client.get(
        _EUROPE_PMC_API,
        params={
            "query": query,
            "format": "json",
            "pageSize": max_results,
            "resultType": "core",
        },
        timeout=_SEARCH_TIMEOUT,
    )
    _raise_for_status(response, "europe_pmc")
    results = []
    result_list = (response.json() or {}).get("resultList") or {}
    for paper in result_list.get("result") or []:
        title = paper.get("title") or ""
        doi = paper.get("doi") or ""
        source = paper.get("source") or "MED"
        identifier = paper.get("id") or paper.get("pmid") or paper.get("pmcid")
        url = (
            f"https://doi.org/{doi}"
            if doi
            else (
                f"https://europepmc.org/article/{source}/{identifier}"
                if identifier
                else ""
            )
        )
        if not title or not url:
            continue
        entry: dict = {"source": "europe_pmc", "title": title, "url": url}
        abstract = paper.get("abstractText") or ""
        if abstract:
            entry["snippet"] = " ".join(abstract.split())[:300]
        if paper.get("pubYear"):
            entry["year"] = int(paper["pubYear"])
        results.append(entry)
    return results


async def _semantic_scholar(
    client: httpx.AsyncClient, query: str, max_results: int
) -> list[dict]:
    response = await client.get(
        _SEMANTIC_SCHOLAR_API,
        params={
            "query": query,
            "fields": "title,abstract,year,venue,url,externalIds",
            "limit": max_results,
        },
        timeout=_SEARCH_TIMEOUT,
    )
    _raise_for_status(response, "semantic_scholar")
    results = []
    for paper in (response.json() or {}).get("data") or []:
        url = paper.get("url") or ""
        if not url:
            arxiv_id = (paper.get("externalIds") or {}).get("ArXiv")
            if arxiv_id:
                url = f"https://arxiv.org/abs/{arxiv_id}"
        if not paper.get("title") or not url:
            continue
        entry: dict = {
            "source": "semantic_scholar",
            "title": paper["title"],
            "url": url,
        }
        if paper.get("abstract"):
            entry["snippet"] = " ".join(paper["abstract"].split())[:300]
        if paper.get("year"):
            entry["year"] = int(paper["year"])
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


async def _gather_leg(
    name: str,
    leg,
    client: httpx.AsyncClient,
    query: str,
    limit: int,
    state: SearchRunState,
) -> tuple[list[dict], list[str]]:
    """Run one leg; a leg failure is an empty list plus a note, not a raise.

    Semantic Scholar's 429 is reported as "skipped" rather than as a
    failure, because it is the expected steady state for keyless traffic.
    """
    try:
        await state.before_request(name)
        return await leg(client, query, limit), []
    except _ProviderUnavailable as error:
        return [], [f"{name}: skipped - {error}"]
    except _RateLimited as error:
        disposition = state.rate_limited(name, error.retry_after)
        return [], [f"{name}: {disposition}"]
    except httpx.TimeoutException as error:
        state.timed_out(name)
        return [], [f"{name}: {type(error).__name__}; cooling down for 15s"]
    except (httpx.HTTPError, ElementTree.ParseError, ValueError) as error:
        return [], [f"{name}: {type(error).__name__}: {error}"]


def _dedupe(results: list[dict]) -> list[dict]:
    """One entry per normalized URL; first occurrence wins."""
    seen: set[str] = set()
    unique = []
    for entry in results:
        key = _norm_url(entry.get("url", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _round_robin(groups: list[list[dict]]) -> list[dict]:
    merged = []
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index < len(group):
                merged.append(group[index])
    return merged


def _norm_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    host = parts.netloc.lower()
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    return f"{parts.scheme.lower()}://{host}{parts.path.rstrip('/')}"


async def web_search(
    client: httpx.AsyncClient,
    query: str,
    *,
    max_results: int = 8,
    state: SearchRunState | None = None,
) -> str:
    """Merge the keyless search legs into one ranked, deduplicated list.

    Scholarly entries are ordered first and take their share of the cap;
    the web leg fills the remainder. Each leg's failure is recorded in
    ``errors`` so the model can see which leg was unavailable and adapt
    (rephrase the query, rely on the other legs) instead of stalling.
    """
    state = state or SearchRunState()
    max_results = max(1, min(int(max_results), 20))
    cache_key = (" ".join(query.casefold().split()), max_results)
    if cache_key in state.cache:
        return state.cache[cache_key]

    scholarly_limit = min(_SCHOLARLY_LIMIT, max_results)
    web_limit = max_results - scholarly_limit + _SCHOLARLY_LIMIT

    legs = await asyncio.gather(
        _gather_leg("arxiv", _arxiv, client, query, scholarly_limit, state),
        _gather_leg(
            "openalex", _openalex, client, query, scholarly_limit, state
        ),
        _gather_leg(
            "crossref", _crossref, client, query, scholarly_limit, state
        ),
        _gather_leg(
            "europe_pmc", _europe_pmc, client, query, scholarly_limit, state
        ),
        _gather_leg(
            "semantic_scholar",
            _semantic_scholar,
            client,
            query,
            scholarly_limit,
            state,
        ),
        _gather_leg("web", _duckduckgo, client, query, web_limit, state),
    )
    (arxiv, arxiv_err), (openalex, openalex_err), (crossref, crossref_err), (
        europe_pmc,
        europe_pmc_err,
    ), (s2, s2_err), (web, web_err) = legs

    scholarly = _dedupe(
        _round_robin([arxiv, openalex, crossref, europe_pmc, s2])
    )
    web_reserve = min(2, max_results // 3)
    scholarly_cap = max_results - web_reserve
    # Reserve up to two slots for ordinary web results, but let scholarly
    # overflow reclaim them when that leg is empty or duplicates a paper.
    merged = _dedupe(
        [
            *scholarly[:scholarly_cap],
            *web[:web_reserve],
            *scholarly[scholarly_cap:],
            *web[web_reserve:],
        ]
    )[:max_results]
    result = json.dumps(
        {
            "tool": "web_search",
            "query": query,
            "results": merged,
            "errors": [
                *arxiv_err,
                *openalex_err,
                *crossref_err,
                *europe_pmc_err,
                *s2_err,
                *web_err,
            ],
        },
        ensure_ascii=False,
    )
    state.cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------


def _blocked_host(hostname: str) -> str:
    """Resolve a hostname and refuse anything that is not the public internet.

    The subagent decides what to fetch, and its context contains untrusted
    page content: a page saying "fetch http://127.0.0.1:8080/..." must not
    become a fetch of this machine's own services. Loopback, private,
    link-local, reserved and unspecified ranges are all refused.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as error:
        return f"host {hostname!r} does not resolve: {error}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return (
                f"refused: {hostname!r} resolves to a local or private "
                f"address ({ip}); only public http/https pages may be fetched"
            )
    return ""


_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _naive_text(html: str) -> str:
    """Fallback reduction for pages trafilatura cannot find an article in."""
    text = _TAG_RE.sub(" ", html)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _extract_article(raw: bytes, url: str) -> str:
    """trafilatura first, naive strip second. Runs in a worker thread."""
    document = raw.decode("utf-8", "replace")
    article = trafilatura.extract(
        document, url=url, include_comments=False, include_tables=True
    )
    if article and article.strip():
        return _WHITESPACE_RE.sub(" ", article).strip()
    return _naive_text(document)


async def web_fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_chars: int = 4_000,
) -> str:
    """Fetch one public page and reduce it to its article text.

    Returns the JSON observation document the subagent reads. Every failure
    mode is a document, because the subagent must keep running: a fetch that
    raises would end the whole research turn.
    """
    max_chars = max(200, min(int(max_chars), 8_000))
    requested_url = url.strip()
    current_url = requested_url
    seen: set[str] = set()
    content = b""
    status = 0
    final_url = current_url
    for _ in range(6):
        parts = urlsplit(current_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return _fetch_document(
                requested_url,
                current_url,
                error="only http/https URLs",
                error_kind="invalid_url",
                retryable=False,
            )
        blocked = await asyncio.to_thread(_blocked_host, parts.hostname)
        if blocked:
            return _fetch_document(
                requested_url,
                current_url,
                error=blocked,
                error_kind="blocked_address",
                retryable=False,
            )
        normalized = current_url.casefold()
        if normalized in seen:
            return _fetch_document(
                requested_url,
                current_url,
                error="redirect loop",
                error_kind="redirect_loop",
                retryable=False,
            )
        seen.add(normalized)
        try:
            async with client.stream(
                "GET", current_url, follow_redirects=False
            ) as response:
                status = response.status_code
                final_url = str(response.url)
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "").strip()
                    if not location:
                        return _fetch_document(
                            requested_url,
                            final_url,
                            status=status,
                            error="redirect response had no Location header",
                            error_kind="invalid_redirect",
                            retryable=False,
                        )
                    current_url = urljoin(final_url, location)
                    continue
                if not response.is_success:
                    return _http_failure(requested_url, final_url, response)
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > _MAX_RESPONSE_BYTES:
                        return _fetch_document(
                            requested_url,
                            final_url,
                            status=status,
                            error="response exceeded the 2 MB limit",
                            error_kind="response_too_large",
                            retryable=False,
                        )
                content = bytes(chunks)
                break
        except httpx.HTTPError as error:
            return _fetch_document(
                requested_url,
                current_url,
                error=f"{type(error).__name__}: {error}",
                error_kind="network_error",
                retryable=True,
            )
    else:
        return _fetch_document(
            requested_url,
            current_url,
            error="too many redirects",
            error_kind="too_many_redirects",
            retryable=False,
        )

    text = await asyncio.to_thread(_extract_article, content, final_url)
    truncated = len(text) > max_chars
    return json.dumps(
        {
            "tool": "web_fetch",
            "url": requested_url,
            "final_url": final_url,
            "status": status,
            "truncated": truncated,
            "text": text[:max_chars] + (" [...]" if truncated else ""),
        },
        ensure_ascii=False,
    )


def _fetch_document(
    requested_url: str,
    final_url: str,
    *,
    error: str,
    error_kind: str,
    retryable: bool,
    status: int | None = None,
    retry_after_s: float | None = None,
) -> str:
    document: dict = {
        "tool": "web_fetch",
        "url": requested_url,
        "final_url": final_url,
        "error_kind": error_kind,
        "retryable": retryable,
        "error": error,
    }
    if status is not None:
        document["status"] = status
    if retry_after_s is not None:
        document["retry_after_s"] = retry_after_s
    return json.dumps(document, ensure_ascii=False)


def _http_failure(
    requested_url: str, final_url: str, response: httpx.Response
) -> str:
    status = response.status_code
    retry_after = _retry_after(response)
    if status in {401, 403}:
        kind, retryable = "access_denied", False
    elif status in {404, 410}:
        kind, retryable = "not_found", False
    elif status == 429:
        kind, retryable = "rate_limited", True
    elif status in {408, 425} or 500 <= status < 600:
        kind, retryable = "upstream_failure", True
    else:
        kind, retryable = "http_error", False
    return _fetch_document(
        requested_url,
        final_url,
        status=status,
        error=f"HTTP {status}; do not rely on this response as a source",
        error_kind=kind,
        retryable=retryable,
        retry_after_s=retry_after,
    )
