"""General search must not silently disappear behind scholarly results."""

import json

import httpx
import pytest

from recollect.engine import webtools

TARGET = "https://example.com/library?branch=central"
ENCODED = "https%3A%2F%2Fexample.com%2Flibrary%3Fbranch%3Dcentral"


@pytest.mark.parametrize("href", [
    f"/l/?uddg={ENCODED}",
    f"//duckduckgo.com/l/?uddg={ENCODED}",
    f"https://duckduckgo.com/l/?uddg={ENCODED}",
    f"https://html.duckduckgo.com/l/?uddg={ENCODED}",
    f"https://lite.duckduckgo.com/l/?uddg={ENCODED}",
])
def test_duckduckgo_redirect_forms_resolve_to_the_actual_result(href):
    assert webtools._ddg_url([("href", href)]) == TARGET


@pytest.mark.parametrize(("href", "expected"), [
    (TARGET, TARGET),
    ("//example.com/library", "https://example.com/library"),
    ("/settings", ""),
    ("/l/?uddg=javascript%3Aalert(1)", ""),
    ("/l/?uddg=file%3A%2F%2F%2Fetc%2Fpasswd", ""),
    (f"javascript://duckduckgo.com/l/?uddg={ENCODED}", ""),
    (f"file://duckduckgo.com/l/?uddg={ENCODED}", ""),
    ("/l/?uddg=", ""),
    ("https://[invalid", ""),
    (f"https://duckduckgo.com.example.net/l/?uddg={ENCODED}",
     f"https://duckduckgo.com.example.net/l/?uddg={ENCODED}"),
])
def test_search_link_normalization_accepts_only_http_results_and_exact_ddg_hosts(
    href, expected,
):
    assert webtools._ddg_url([("href", href)]) == expected


async def test_protocol_relative_search_results_survive_parsing_and_deduplication():
    body = "".join(
        f'<a class="result__a" href="//duckduckgo.com/l/?uddg='
        f'https%3A%2F%2Fexample.com%2F{page}">{page}</a>'
        for page in ["hours", "location"]
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text=body),
    )) as client:
        results = await webtools._duckduckgo(client, "official library hours", 8)
    assert [item["url"] for item in webtools._dedupe(results)] == [
        "https://example.com/hours", "https://example.com/location",
    ]


@pytest.mark.parametrize(("status", "body", "error_fragment"), [
    (202, '<form id="challenge-form">Verify this request</form>', "challenge"),
    (200, '<div class="anomaly-modal">Choose the matching images</div>', "challenge"),
    (200, "<html><p>Search page format changed.</p></html>", "unrecognized"),
])
async def test_unavailable_web_responses_are_reported_in_search_errors(
    monkeypatch, status, body, error_fragment,
):
    async def empty(*args):
        return []

    for name in [
        "_arxiv", "_openalex", "_crossref", "_europe_pmc", "_semantic_scholar",
    ]:
        monkeypatch.setattr(webtools, name, empty)
    requested = []

    def respond(request):
        requested.append(request.url.host)
        return httpx.Response(status, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        document = json.loads(
            await webtools.web_search(client, "official library hours")
        )
    assert document["results"] == []
    assert requested == ["html.duckduckgo.com", "lite.duckduckgo.com"]
    assert any(
        "web:" in error and error_fragment in error.lower()
        for error in document["errors"]
    )


async def test_challenged_html_search_can_still_use_a_working_lite_response():
    def respond(request):
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(202, text='<form id="challenge-form"></form>')
        return httpx.Response(200, text=(
            '<a class="result-link" href="https://example.com/hours">Official hours</a>'
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        results, errors = await webtools._gather_leg(
            "web", webtools._duckduckgo, client, "library hours", 8,
            webtools.SearchRunState(),
        )
    assert [item["url"] for item in results] == ["https://example.com/hours"]
    assert errors == []


async def test_explicit_no_results_page_is_not_reported_as_provider_failure():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text=(
            '<div class="no-results__message">No results found for this query.</div>'
        )),
    )) as client:
        results, errors = await webtools._gather_leg(
            "web", webtools._duckduckgo, client, "no matching pages", 8,
            webtools.SearchRunState(),
        )
    assert results == []
    assert errors == []
