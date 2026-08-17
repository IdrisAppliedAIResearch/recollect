"""Generator accounting.

The generator has no research behind it, but it reports two numbers that
are easy to get wrong and that a reader will trust: how much of the prompt
the server had already computed, and what the model actually said when it
routed its output into a non-standard field.
"""

from __future__ import annotations

from recollect.engine.generator import (
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
