"""The deployed tokenizer bounds inference without rewriting verified memory."""

import json

import httpx
import pytest

from recollect.engine.context_window import check_context


@pytest.mark.parametrize("context_tokens,accepted", [(76, True), (75, False)])
async def test_context_exact_boundary_preserves_payload(context_tokens, accepted):
    payload = {
        "messages": [{"role": "user", "content": "unchanged memory"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "max_tokens": 10,
    }
    calls = []

    def server(request):
        calls.append(request.url.path)
        if request.url.path == "/apply-template":
            assert json.loads(request.content) == payload
            return httpx.Response(200, json={"prompt": "exact template"})
        assert request.url.path == "/tokenize"
        assert json.loads(request.content) == {
            "content": "exact template",
            "add_special": True,
        }
        return httpx.Response(200, json={"tokens": [1, 2]})

    async with httpx.AsyncClient(
        base_url="http://model/v1",
        transport=httpx.MockTransport(server),
    ) as client:
        if accepted:
            assert await check_context(client, payload, context_tokens) == 2
        else:
            with pytest.raises(ValueError, match="verified memory was not truncated"):
                await check_context(client, payload, context_tokens)
    assert calls == ["/apply-template", "/tokenize"]
    assert payload["messages"][0]["content"] == "unchanged memory"


@pytest.mark.parametrize("failure", ["template", "tokens", "http"])
async def test_missing_accounting_fails_closed(failure):
    def server(request):
        if failure == "http":
            return httpx.Response(503)
        if request.url.path == "/apply-template":
            return httpx.Response(
                200,
                json={} if failure == "template" else {"prompt": "a"},
            )
        return httpx.Response(200, json={"tokens": None})

    async with httpx.AsyncClient(
        base_url="http://model/v1/",
        transport=httpx.MockTransport(server),
    ) as client:
        error = httpx.HTTPStatusError if failure == "http" else ValueError
        with pytest.raises(error):
            await check_context(client, {"max_tokens": 10}, 4096)
