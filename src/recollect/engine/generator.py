"""The chat model adapter: OpenAI-compatible, streaming, instrumented.

Deliberately thin. The generator is the one component here with no
research behind it and no reason to be clever - it is a swappable
back end, and everything interesting happens before the prompt is sent.
Anything speaking the OpenAI chat API works: llama-server, Ollama, vLLM,
a hosted endpoint.

Two behaviours are not optional, because both were measured on the target
hardware and both silently break a first conversation.

**Reasoning models leave ``content`` empty.** The carried models route
chain-of-thought into a separate ``reasoning_content`` field and emit
nothing into ``content`` until thinking completes. A client that watches
only ``content`` sees a long pause and then, on a truncated response,
nothing at all - which reads as a broken server rather than a model that
spent its whole budget thinking. So both fields are read and both are
reported, and ``enable_thinking`` is passed explicitly rather than left to
the template's default. Note that ``reasoning_effort`` is accepted and
then ignored by llama.cpp's server; it is not used here.

**Prompt order decides interactivity.** Prefix caching makes cost
proportional to the suffix after the first changed token. Measured at a
~18k-token prompt on this hardware: a stable prefix with the question
appended costs 0.27s warm, while rewriting the memory block each turn
costs 5.9s - 22x. This architecture rebuilds its context block every turn
by design, so it forfeits most of that cache on purpose; that is the
mechanism working, not a bug. What is still free is ordering the prompt so
the *stable* part comes first, which is why the system preamble precedes
the memory block. The realised cache hit is recorded in the trace rather
than assumed.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass

import httpx

from ..trace import GenerationTrace, ToolCallTrace
from .context_window import check_context


@dataclass(frozen=True)
class GeneratorSettings:
    base_url: str
    model: str
    api_key: str = "not-needed"
    timeout_s: float = 300.0
    thinking: bool = False
    max_tokens: int = 1_024
    temperature: float = 0.7
    context_tokens: int | None = None
    require_tools: bool = False


@dataclass
class StreamChunk:
    """One increment from the model."""

    kind: str  # "token" | "reasoning"
    text: str


class GenerationError(RuntimeError):
    """The generator could not be reached or returned an error."""


class _ThinkMarkupFilter:
    """Remove leaked template think blocks without breaking streamed text.

    Some OpenAI-compatible servers leak an orphan closing delimiter even
    when thinking is disabled. A delimiter may be split across SSE deltas,
    so filtering each delta independently is not sufficient.
    """

    _OPEN = ("<think>", "[think]")
    _CLOSE = ("</think>", "[/think]")
    _MARKERS = _OPEN + _CLOSE

    def __init__(self) -> None:
        self._pending = ""
        self._inside = False

    @staticmethod
    def _partial_suffix(text: str, markers: tuple[str, ...]) -> int:
        lowered = text.lower()
        longest = min(len(text), max(map(len, markers)) - 1)
        for size in range(longest, 0, -1):
            suffix = lowered[-size:]
            if any(marker.startswith(suffix) for marker in markers):
                return size
        return 0

    def feed(self, text: str) -> str:
        self._pending += text
        clean: list[str] = []

        while self._pending:
            lowered = self._pending.lower()
            if self._inside:
                matches = [
                    (lowered.find(marker), marker)
                    for marker in self._CLOSE
                    if lowered.find(marker) >= 0
                ]
                if matches:
                    index, marker = min(matches, key=lambda match: match[0])
                    self._pending = self._pending[index + len(marker) :]
                    self._inside = False
                    continue

                keep = self._partial_suffix(self._pending, self._CLOSE)
                self._pending = self._pending[-keep:] if keep else ""
                break

            matches = [
                (lowered.find(marker), marker)
                for marker in self._MARKERS
                if lowered.find(marker) >= 0
            ]
            if matches:
                index, marker = min(matches, key=lambda match: match[0])
                clean.append(self._pending[:index])
                self._pending = self._pending[index + len(marker) :]
                self._inside = marker in self._OPEN
                continue

            keep = self._partial_suffix(self._pending, self._MARKERS)
            emit_to = len(self._pending) - keep
            clean.append(self._pending[:emit_to])
            self._pending = self._pending[emit_to:]
            break

        return "".join(clean)

    def finish(self) -> str:
        tail = "" if self._inside else self._pending
        self._pending = ""
        return tail


class Generator:
    """An OpenAI-compatible streaming chat client."""

    def __init__(
        self,
        settings: GeneratorSettings,
        *,
        model_slot: asyncio.Lock | None = None,
    ) -> None:
        self.settings = settings
        self._model_slot = model_slot or asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout_s, connect=10.0),
            headers={"Authorization": f"Bearer {settings.api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- prompt assembly ---------------------------------------------------

    def build_messages(
        self,
        *,
        system_prompt: str,
        context_block: str,
        user_message: str,
    ) -> list[dict]:
        """Stable preamble first, then the rebuilt memory, then the question.

        The memory block is a separate system message rather than being
        concatenated into the preamble so the cacheable prefix ends at a
        clean boundary, and so a reader of the request can see exactly
        which bytes the memory system contributed.
        """
        messages = [{"role": "system", "content": system_prompt}]
        if context_block:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your memory of this conversation so far:\n\n"
                        f"{context_block}"
                    ),
                }
            )
        messages.append({"role": "user", "content": user_message})
        return messages

    # -- streaming ----------------------------------------------------------

    async def stream(
        self,
        messages: list[dict],
        *,
        trace: GenerationTrace,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Wait for the single local-model slot, then stream a completion."""
        queued = time.perf_counter()
        async with (
            self._model_slot,
            aclosing(self._stream_unlocked(
                messages,
                trace=trace,
                tools=tools,
                max_tokens=max_tokens,
            )) as stream,
        ):
            trace.model_queue_ms = (time.perf_counter() - queued) * 1_000
            async for chunk in stream:
                yield chunk

    async def _stream_unlocked(
        self,
        messages: list[dict],
        *,
        trace: GenerationTrace,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a completion, filling ``trace`` in place as it goes.

        The trace object is mutated rather than returned so a caller can
        forward increments to a UI and still hold the finished accounting
        when the stream ends.

        ``tools`` is an OpenAI tool schema list. When given, the request
        advertises them and the model may answer with ``tool_calls`` instead
        of prose; those are accumulated into ``trace.tool_calls`` (see the
        accumulation note below). Passing nothing reproduces the exact
        request this method has always sent, so callers that never delegate
        are unaffected. ``max_tokens`` overrides the settings cap for this
        call only - the subagent budgets its own generations
        separately from the main model's.
        """
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens
            if max_tokens is not None
            else self.settings.max_tokens,
            "temperature": self.settings.temperature,
            "stream_options": {"include_usage": True},
            # Explicit: the carried templates default to thinking on, which
            # leaves `content` empty. See the module docstring.
            "chat_template_kwargs": {"enable_thinking": self.settings.thinking},
        }
        if tools:
            # Native tool calling: the carried GGUF's chat template has a live
            # tools/function branch (probed against the target server), so the
            # model is asked to select a tool rather than emit JSON in prose.
            # Continuous chat requires an explicit operation or task_reply so
            # status-only replies cannot silently become memory episodes.
            payload["tools"] = tools
            payload["tool_choice"] = (
                "required" if self.settings.require_tools else "auto"
            )
        trace.thinking_enabled = self.settings.thinking

        started = time.perf_counter()
        first_token_at: float | None = None
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        content_filter = _ThinkMarkupFilter()
        # Tool calls stream as *fragments*: the id and name arrive in one
        # delta, then the JSON arguments arrive several chunks later, each
        # carrying only `function.arguments`. Keying by the delta's `index`
        # (falling back to append order for servers that omit it) is what
        # reassembles one call out of its pieces.
        tool_parts: dict[int, dict[str, str]] = {}

        try:
            if self.settings.context_tokens is not None:
                try:
                    await check_context(
                        self._client, payload, self.settings.context_tokens,
                    )
                except ValueError as error:
                    trace.error = str(error)
                    raise GenerationError(trace.error) from error
            async with self._client.stream(
                "POST", "/chat/completions", json=payload
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise GenerationError(
                        f"Generator returned HTTP {response.status_code}: "
                        f"{body[:500]}"
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    _absorb_metrics(event, trace)

                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if choice.get("finish_reason"):
                            trace.finish_reason = choice["finish_reason"]

                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            reasoning_parts.append(reasoning)
                            yield StreamChunk("reasoning", reasoning)

                        content = delta.get("content")
                        if content:
                            content = content_filter.feed(content)
                        if content:
                            if first_token_at is None:
                                first_token_at = time.perf_counter()
                                trace.ttft_ms = (first_token_at - started) * 1_000.0
                            content_parts.append(content)
                            yield StreamChunk("token", content)

                        tool_calls = delta.get("tool_calls")
                        if isinstance(tool_calls, list):
                            _absorb_tool_calls(tool_calls, tool_parts)

                tail = content_filter.finish()
                if tail:
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                        trace.ttft_ms = (first_token_at - started) * 1_000.0
                    content_parts.append(tail)
                    yield StreamChunk("token", tail)
        except httpx.HTTPError as error:
            trace.error = (
                f"Could not reach the generator at {self.settings.base_url}: "
                f"{error}"
            )
            raise GenerationError(trace.error) from error
        finally:
            total_ms = (time.perf_counter() - started) * 1_000.0
            trace.total_ms = total_ms
            trace.response_text = "".join(content_parts)
            trace.response_chars = len(trace.response_text)
            trace.reasoning_text = "".join(reasoning_parts)
            trace.tool_calls = [
                ToolCallTrace(
                    id=slot["id"], name=slot["name"], arguments=slot["arguments"]
                )
                for _, slot in sorted(tool_parts.items(), key=lambda kv: kv[0])
            ]
            if trace.tokens_out and total_ms > 0:
                decode_ms = total_ms - (trace.ttft_ms or 0.0)
                if decode_ms > 0:
                    trace.tokens_per_sec = trace.tokens_out / (decode_ms / 1_000.0)

    # -- health -------------------------------------------------------------

    async def health(self) -> dict:
        """Ask the generator what it is. Never raises; reports instead."""
        try:
            response = await self._client.get("/models", timeout=5.0)
            response.raise_for_status()
            body = response.json()
            models = [entry.get("id") for entry in body.get("data", [])]
            return {
                "reachable": True,
                "base_url": self.settings.base_url,
                "configured_model": self.settings.model,
                "available_models": models,
            }
        except Exception as error:  # noqa: BLE001 - health must not raise
            return {
                "reachable": False,
                "base_url": self.settings.base_url,
                "configured_model": self.settings.model,
                "error": str(error),
            }


def _absorb_metrics(event: dict, trace: GenerationTrace) -> None:
    """Pull usage and llama.cpp timings out of whichever chunk carries them."""
    usage = event.get("usage")
    if isinstance(usage, dict):
        if usage.get("completion_tokens") is not None:
            trace.tokens_out = int(usage["completion_tokens"])
        if usage.get("prompt_tokens") is not None:
            trace.prompt_cache.prompt_tokens = int(usage["prompt_tokens"])

    # llama.cpp reports a `timings` block with the prefill accounting that
    # says how much of this prompt it had already computed.
    #
    # `prompt_n` is the tokens it actually had to process, NOT the prompt
    # length: the cached prefix is excluded. Treating it as the total makes
    # the hit ratio exceed 100% as soon as the cache does any work, so the
    # total is reconstructed as processed + cached.
    timings = event.get("timings")
    if isinstance(timings, dict):
        cache = trace.prompt_cache
        if timings.get("prompt_n") is not None:
            cache.processed_tokens = int(timings["prompt_n"])
        if timings.get("cache_n") is not None:
            cache.cached_tokens = int(timings["cache_n"])
        if timings.get("prompt_ms") is not None:
            cache.prefill_ms = float(timings["prompt_ms"])
        if cache.processed_tokens is not None and cache.cached_tokens is not None:
            cache.prompt_tokens = cache.processed_tokens + cache.cached_tokens


def _absorb_tool_calls(
    tool_calls: list, tool_parts: dict[int, dict[str, str]]
) -> None:
    """Fold streamed tool-call fragments into their per-index slots.

    Each delta carries at most one partial call: often just a further
    fragment of the JSON arguments. Everything that is present is merged in;
    everything absent is simply not yet known.
    """
    for position, part in enumerate(tool_calls):
        if not isinstance(part, dict):
            continue
        index = part.get("index")
        if not isinstance(index, int):
            index = max(tool_parts) + 1 if tool_parts else position
        slot = tool_parts.setdefault(
            index, {"id": "", "name": "", "arguments": ""}
        )
        if part.get("id"):
            slot["id"] = str(part["id"])
        function = part.get("function")
        if isinstance(function, dict):
            if function.get("name"):
                slot["name"] = str(function["name"])
            if function.get("arguments"):
                slot["arguments"] += str(function["arguments"])


def new_generation_trace(
    *,
    settings: GeneratorSettings,
    system_prompt: str,
    context_block: str,
    user_message: str,
) -> GenerationTrace:
    return GenerationTrace(
        model=settings.model,
        base_url=settings.base_url,
        system_prompt_chars=len(system_prompt),
        context_block_chars=len(context_block),
        total_prompt_chars=(
            len(system_prompt) + len(context_block) + len(user_message)
        ),
    )
