"""Search-provider parsing, merge, caching, and per-run cooldown behavior."""

from __future__ import annotations

import json

import httpx

import recollect.engine.webtools as webtools


def _result(source: str) -> list[dict]:
    return [
        {
            "source": source,
            "title": f"{source} paper",
            "url": f"https://example.com/{source}",
        }
    ]


async def test_search_expands_sources_and_caches_exact_query(monkeypatch):
    calls: list[str] = []

    def provider(source: str):
        async def search(client, query, max_results):
            calls.append(source)
            return _result(source)

        return search

    providers = {
        "_arxiv": "arxiv",
        "_openalex": "openalex",
        "_crossref": "crossref",
        "_europe_pmc": "europe_pmc",
        "_semantic_scholar": "semantic_scholar",
        "_duckduckgo": "web",
    }
    for name, source in providers.items():
        monkeypatch.setattr(webtools, name, provider(source))

    state = webtools.SearchRunState()
    first = await webtools.web_search(
        object(), "memory retrieval", max_results=8, state=state
    )
    second = await webtools.web_search(
        object(), "  MEMORY   retrieval ", max_results=8, state=state
    )

    assert first == second
    assert calls == list(providers.values())
    sources = {entry["source"] for entry in json.loads(first)["results"]}
    assert sources == set(providers.values())


async def test_semantic_scholar_429_disables_only_that_run(monkeypatch):
    for provider in webtools._PROVIDER_INTERVALS:
        monkeypatch.setitem(webtools._PROVIDER_INTERVALS, provider, 0.0)

    async def empty(client, query, max_results):
        return []

    semantic_calls = 0

    async def limited(client, query, max_results):
        nonlocal semantic_calls
        semantic_calls += 1
        raise webtools._RateLimited("semantic_scholar", 60.0)

    for name in (
        "_arxiv",
        "_openalex",
        "_crossref",
        "_europe_pmc",
        "_duckduckgo",
    ):
        monkeypatch.setattr(webtools, name, empty)
    monkeypatch.setattr(webtools, "_semantic_scholar", limited)

    state = webtools.SearchRunState()
    first = json.loads(
        await webtools.web_search(object(), "query one", state=state)
    )
    second = json.loads(
        await webtools.web_search(object(), "query two", state=state)
    )

    assert semantic_calls == 1
    assert any("disabled for this run" in error for error in first["errors"])
    assert any("disabled for this run" in error for error in second["errors"])


async def test_arxiv_requests_are_spaced_three_seconds(monkeypatch):
    now = 100.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    monkeypatch.setattr(webtools.time, "monotonic", monotonic)
    monkeypatch.setattr(webtools.asyncio, "sleep", sleep)

    state = webtools.SearchRunState()
    await state.before_request("arxiv")
    await state.before_request("arxiv")

    assert sleeps == [3.0]


async def test_new_scholarly_provider_payloads_are_normalized():
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openalex.org":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "https://openalex.org/W1",
                            "display_name": "OpenAlex title",
                            "publication_year": 2024,
                            "doi": "https://doi.org/10.1/openalex",
                            "primary_location": {},
                            "abstract_inverted_index": {
                                "Reconstructed": [0],
                                "abstract": [1],
                            },
                        }
                    ]
                },
            )
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200,
                json={
                    "message": {
                        "items": [
                            {
                                "DOI": "10.1/crossref",
                                "title": ["Crossref title"],
                                "abstract": "<p>Crossref abstract</p>",
                                "published": {"date-parts": [[2023, 1, 2]]},
                            }
                        ]
                    }
                },
            )
        if request.url.host == "www.ebi.ac.uk":
            return httpx.Response(
                200,
                json={
                    "resultList": {
                        "result": [
                            {
                                "id": "123",
                                "source": "MED",
                                "title": "Europe PMC title",
                                "abstractText": "Europe PMC abstract",
                                "pubYear": "2022",
                            }
                        ]
                    }
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        openalex = await webtools._openalex(client, "query", 1)
        crossref = await webtools._crossref(client, "query", 1)
        europe_pmc = await webtools._europe_pmc(client, "query", 1)

    assert openalex == [
        {
            "source": "openalex",
            "title": "OpenAlex title",
            "url": "https://doi.org/10.1/openalex",
            "snippet": "Reconstructed abstract",
            "year": 2024,
        }
    ]
    assert crossref == [
        {
            "source": "crossref",
            "title": "Crossref title",
            "url": "https://doi.org/10.1/crossref",
            "snippet": "Crossref abstract",
            "year": 2023,
        }
    ]
    assert europe_pmc == [
        {
            "source": "europe_pmc",
            "title": "Europe PMC title",
            "url": "https://europepmc.org/article/MED/123",
            "snippet": "Europe PMC abstract",
            "year": 2022,
        }
    ]
