"""Generator accounting.

The generator has no research behind it, but it reports two numbers that
are easy to get wrong and that a reader will trust: how much of the prompt
the server had already computed, and what the model actually said when it
routed its output into a non-standard field.
"""

from __future__ import annotations

import json

from recollect.engine.generator import (
    Generator,
    GeneratorSettings,
    _absorb_metrics,
    new_generation_trace,
)

SETTINGS = GeneratorSettings(base_url="http://127.0.0.1:8000/v1", model="test")


def _trace():
    return new_generation_trace(
        settings=SETTINGS,
        system_prompt="system",
        context_block="<recent_context/>\n\n<retrieved_stm/>",
        user_message="hello",
    )


def test_prompt_length_is_processed_plus_cached():
    """llama.cpp's `prompt_n` is the new work, not the prompt length.

    Reading it as the total is what produced a reported cache hit ratio of
    161%: 145 reused against a "total" of 90 that had already excluded
    them.
    """
    trace = _trace()
    _absorb_metrics({"timings": {"prompt_n": 26, "cache_n": 146}}, trace)

    cache = trace.prompt_cache
    assert cache.processed_tokens == 26
    assert cache.cached_tokens == 146
    assert cache.prompt_tokens == 172
    assert cache.cache_hit_ratio == 146 / 172
    assert 0.0 <= cache.cache_hit_ratio <= 1.0


def test_cache_ratio_never_exceeds_one():
    """Whatever a server reports, the ratio stays a ratio."""
    trace = _trace()
    _absorb_metrics({"usage": {"prompt_tokens": 90}}, trace)
    _absorb_metrics({"timings": {"cache_n": 145}}, trace)
    assert trace.prompt_cache.cache_hit_ratio == 1.0


def test_missing_timings_report_nothing_rather_than_zero():
    """A server that does not report cache stats must not look like a miss."""
    trace = _trace()
    _absorb_metrics({"choices": []}, trace)
    assert trace.prompt_cache.cache_hit_ratio is None
    assert trace.prompt_cache.prompt_tokens is None


def test_usage_supplies_token_counts():
    trace = _trace()
    _absorb_metrics(
        {"usage": {"prompt_tokens": 400, "completion_tokens": 37}}, trace
    )
    assert trace.tokens_out == 37
    assert trace.prompt_cache.prompt_tokens == 400


def test_prompt_puts_the_stable_preamble_before_the_rebuilt_memory():
    """Ordering is a measured cost, not a style choice.

    The memory block changes every turn by design, so everything before it
    is the only part a prefix cache can keep. Putting the preamble second
    would forfeit that too.
    """
    from recollect.engine.generator import Generator

    generator = Generator(SETTINGS)
    messages = generator.build_messages(
        system_prompt="PREAMBLE",
        context_block="MEMORY",
        user_message="QUESTION",
    )
    assert [message["role"] for message in messages] == [
        "system",
        "system",
        "user",
    ]
    assert messages[0]["content"] == "PREAMBLE"
    assert "MEMORY" in messages[1]["content"]
    assert messages[2]["content"] == "QUESTION"


def test_empty_context_block_is_omitted_entirely():
    """A first turn has no memory; it should not carry an empty wrapper."""
    from recollect.engine.generator import Generator

    generator = Generator(SETTINGS)
    messages = generator.build_messages(
        system_prompt="PREAMBLE", context_block="", user_message="QUESTION"
    )
    assert [message["role"] for message in messages] == ["system", "user"]


# -- request payload: the tools path must not change anything else ----------


def _mocked_generator(sse_body: str):
    """A Generator whose transport replays one body and records the payload."""
    import httpx

    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, content=sse_body.encode("utf-8"))

    generator = Generator(SETTINGS)
    generator._client = httpx.AsyncClient(
        base_url=SETTINGS.base_url.rstrip("/"),
        transport=httpx.MockTransport(handler),
    )
    return generator, payloads


BASE_PAYLOAD_KEYS = {
    "model",
    "messages",
    "stream",
    "max_tokens",
    "temperature",
    "stream_options",
    "chat_template_kwargs",
}

PLAIN_SSE = (
    'data: {"choices":[{"delta":{"content":"ok "}}]}\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
    "data: [DONE]\n"
)


async def test_payload_without_tools_is_exactly_the_historical_shape():
    """Callers that never delegate must send the same request as before."""
    generator, payloads = _mocked_generator(PLAIN_SSE)
    try:
        chunks = [
            chunk
            async for chunk in generator.stream(
                [{"role": "user", "content": "hi"}], trace=_trace()
            )
        ]
    finally:
        await generator.aclose()

    assert [chunk.text for chunk in chunks] == ["ok "]
    payload = payloads[0]
    assert set(payload) == BASE_PAYLOAD_KEYS
    assert "tools" not in payload and "tool_choice" not in payload


async def test_payload_with_tools_adds_only_tools_and_tool_choice():
    """The research path advertises tools; nothing else moves."""
    tools = [{"type": "function", "function": {"name": "web_search"}}]
    generator, payloads = _mocked_generator(PLAIN_SSE)
    try:
        [
            chunk
            async for chunk in generator.stream(
                [{"role": "user", "content": "hi"}], trace=_trace(), tools=tools
            )
        ]
    finally:
        await generator.aclose()

    payload = payloads[0]
    assert set(payload) == BASE_PAYLOAD_KEYS | {"tools", "tool_choice"}
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "auto"


THINK_LEAK_SSE = (
    'data: {"choices":[{"delta":{"content":"<thi"}}]}\n'
    'data: {"choices":[{"delta":{"content":"nk>hidden reasoning"}}]}\n'
    'data: {"choices":[{"delta":{"content":"</think>Clean answer</thi"}}]}\n'
    'data: {"choices":[{"delta":{"content":"nk>"}}]}\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
    "data: [DONE]\n"
)


async def test_stream_strips_think_blocks_and_split_orphan_delimiters():
    generator, _ = _mocked_generator(THINK_LEAK_SSE)
    trace = _trace()
    try:
        chunks = [
            chunk
            async for chunk in generator.stream(
                [{"role": "user", "content": "hi"}], trace=trace
            )
        ]
    finally:
        await generator.aclose()

    visible = "".join(chunk.text for chunk in chunks)
    assert visible == "Clean answer"
    assert trace.response_text == visible
    assert "hidden reasoning" not in visible
    assert "think" not in visible.lower()


TOOL_SSE = (
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a",'
    '"type":"function","function":{"name":"alpha","arguments":""}}]}}]}\n'
    'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_b",'
    '"type":"function","function":{"name":"beta","arguments":""}}]}}]}\n'
    'data: {"choices":[{"delta":{"tool_calls":[{"index":1,'
    '"function":{"arguments":"{\\"b\\": "}}]}}]}\n'
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":"{\\"a\\": 1}"}}]}}]}\n'
    'data: {"choices":[{"delta":{"tool_calls":[{"index":1,'
    '"function":{"arguments":"2}"}}]}}]}\n'
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n'
    "data: [DONE]\n"
)


async def test_streamed_tool_call_fragments_reassemble_by_index():
    """Arguments arrive in pieces, possibly out of order, over several deltas.

    Keying by the delta's `index` is what reassembles one call out of its
    fragments; append-order would splice the two calls together.
    """
    generator, _ = _mocked_generator(TOOL_SSE)
    trace = _trace()
    try:
        [
            chunk
            async for chunk in generator.stream(
                [{"role": "user", "content": "hi"}],
                trace=trace,
                tools=[{"type": "function", "function": {"name": "alpha"}}],
            )
        ]
    finally:
        await generator.aclose()

    assert trace.finish_reason == "tool_calls"
    assert trace.response_text == ""
    calls = {(call.id, call.name, call.arguments) for call in trace.tool_calls}
    assert calls == {
        ("call_a", "alpha", '{"a": 1}'),
        ("call_b", "beta", '{"b": 2}'),
    }
