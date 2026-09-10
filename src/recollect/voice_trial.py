"""Isolated Qwen display/speech experiment; never reads or writes chat stores."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from recollect.api import _turn_system_prompt
from recollect.config import RecollectConfig
from recollect.engine.context_window import check_context
from recollect.engine.generator import Generator, GeneratorSettings

ASSETS = Path(__file__).with_name("voice_trial_assets")


class SpeechOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Speech text must contain words.")
        return value


class DualOutput(SpeechOutput):
    speech: SpeechOutput


SCHEMA = DualOutput.model_json_schema()
EXAMPLE = {
    "text": "I'd try the smaller change first. We can adjust it after listening.",
    "speech": {
        "text": "Well, I'd try the smaller change first. Then we can listen "
                "and see what needs adjusting.",
    },
}
CONTRACT = """You are Qwen, composing the answer for two presentations in one reply.
Use the same memory and reasoning obligations as above. Establish the answer
from the available conversation, then express that same answer in both fields.
Return exactly one JSON object matching the schema below. No Markdown fences
or commentary outside it. The prose-format instructions above apply inside
the fields; this JSON requirement governs the outer format.

text: the concise, readable answer displayed in chat.
speech.text: the same answer written for Kokoro 82M to speak to this person.
Preserve facts, names, quantities, negations, conditions, uncertainty and useful
questions in BOTH fields. Speech must not invent a memory, action or commitment.
Do not sacrifice a reasoning step or necessary qualification to sound casual.

KOKORO INPUT CONTRACT FOR THIS TRIAL
Our adapter extracts speech.text, normalizes visual formatting and spoken
amounts, and sends ordinary text to Kokoro's English phonemizer. Kokoro does
not consume this JSON or interpret directions like [warmly], [laugh], <pause>
or SSML. Do not emit those, phonetic markup, raw links or pronunciation tags.
There are no model-controlled speed, pitch or voice fields in this schema.
The voice and speed are held fixed outside your response. Ordinary punctuation
is available for phrasing; it is not an exact pause-duration command.

Write speech as a response to the user's actual point, not as a narrated report.
Use contractions, ordinary words and varied sentence lengths where they fit.
A small acknowledgment ("Ah, I see what you mean") can fit a clarification;
"well" or "so" can fit a transition. Use them only when useful in context.
An occasional "um" can fit a real hesitation, but there is no filler quota.
Do not manufacture doubt about known facts, false thinking or repeated mistakes.
A direct answer often needs no opener. Avoid routine praise, automatic agreement
and ending every reply with an invitation or question. Vary wording naturally
without changing the answer. Keep it brief unless accuracy needs more detail.

JSON SCHEMA
{schema}

EXAMPLE OUTPUT (illustrates format and style, not an answer to copy)
{example}
"""


def dual_prompt(baseline: str) -> str:
    return baseline + "\n\n" + CONTRACT.format(
        schema=json.dumps(SCHEMA, ensure_ascii=False, indent=2),
        example=json.dumps(EXAMPLE, ensure_ascii=False, indent=2),
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fact_checks(text: str, case: dict) -> dict[str, bool]:
    """Lexical checks are transparent sentinels, not semantic equivalence scores."""
    return {label: bool(re.search(pattern, text, re.I))
            for label, pattern in case["checks"].items()}


async def complete(client: httpx.AsyncClient, payload: dict) -> dict:
    started = time.perf_counter()
    content, first, finish, usage, timings = [], None, None, {}, {}
    async with client.stream("POST", "/chat/completions", json=payload) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            event = json.loads(line[5:])
            usage = event.get("usage") or usage
            timings = event.get("timings") or timings
            for choice in event.get("choices", []):
                finish = choice.get("finish_reason") or finish
                token = (choice.get("delta") or {}).get("content")
                if token:
                    first = first if first is not None else time.perf_counter()
                    content.append(token)
    return {
        "raw": "".join(content), "finish_reason": finish, "usage": usage,
        "timings": timings, "total_ms": (time.perf_counter() - started) * 1000,
        "ttft_ms": (first - started) * 1000 if first is not None else None,
    }


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for arm in ("baseline", "dual"):
        group = [row for row in rows if row["arm"] == arm]
        summary[arm] = {
            "responses": len(group),
            "valid_responses": sum(row["valid"] for row in group),
            "display_all_checks": sum(all(row["display_checks"].values())
                                      and row["valid"] for row in group),
            "speech_all_checks": sum(all(row["speech_checks"].values())
                                     and row["valid"] for row in group),
            "median_total_ms": statistics.median(row["total_ms"] for row in group),
            "median_ttft_ms": statistics.median(
                row["ttft_ms"] for row in group if row["ttft_ms"] is not None),
            "median_output_tokens": statistics.median(
                row["usage"].get("completion_tokens", 0) for row in group),
        }
    return summary


async def benchmark(
    output: Path, repeats: int, limit: int | None, cases_path: Path,
) -> None:
    config = RecollectConfig.from_env()
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    if limit:
        cases = cases[:limit]
    baseline = _turn_system_prompt(config, datetime.now(UTC), "voice")
    prompts = {"baseline": baseline, "dual": dual_prompt(baseline)}
    for arm, prompt in prompts.items():
        (output / f"{arm}-prompt.txt").write_text(prompt, encoding="utf-8")
    (output / "schema.json").write_text(json.dumps(SCHEMA, indent=2), encoding="utf-8")
    settings = GeneratorSettings(config.generator_base_url, config.generator_model)
    builder = Generator(settings)
    metadata = {
        "created_at": datetime.now(UTC).isoformat(), "repeats": repeats,
        "model": config.generator_model, "thinking": config.generator_thinking,
        "temperature": config.generator_temperature,
        "max_tokens": config.generator_max_tokens,
        "prompt_sha256": {key: digest(value) for key, value in prompts.items()},
        "schema_sha256": digest(json.dumps(SCHEMA, sort_keys=True)),
        "cases": cases, "scope": "Frozen synthetic memory, generation only; "
        "no retrieval, episode ingestion, tool routing or live voice benchmark.",
    }
    rows = []
    try:
        async with httpx.AsyncClient(
            base_url=config.generator_base_url,
            headers={"Authorization": f"Bearer {config.generator_api_key}"},
            timeout=httpx.Timeout(config.generator_timeout_s, connect=10),
        ) as client:
            health = await client.get("/models")
            health.raise_for_status()
            metadata["server_models"] = health.json()
            jobs = [(case, repeat) for case in cases for repeat in range(repeats)]
            random.Random(1209).shuffle(jobs)
            for index, (case, repeat) in enumerate(jobs):
                arms = ("baseline", "dual") if index % 2 == 0 else ("dual", "baseline")
                for arm in arms:
                    messages = builder.build_messages(
                        system_prompt=prompts[arm], context_block=case["memory"],
                        user_message=case["question"],
                    )
                    payload = {
                        "model": config.generator_model, "messages": messages,
                        "stream": True, "stream_options": {"include_usage": True},
                        "temperature": config.generator_temperature,
                        "max_tokens": config.generator_max_tokens,
                        "seed": 12090 + repeat,
                        "chat_template_kwargs": {
                            "enable_thinking": config.generator_thinking,
                        },
                    }
                    if arm == "dual":
                        payload["response_format"] = {
                            "type": "json_schema", "json_schema": {
                                "name": "voice_reply", "schema": SCHEMA,
                            },
                        }
                    prompt_tokens = await check_context(
                        client, payload, config.generator_context_tokens,
                    )
                    result = await complete(client, payload)
                    row = {
                        "case": case["id"], "repeat": repeat, "arm": arm,
                        "memory_sha256": digest(case["memory"]),
                        "request": payload, "checked_prompt_tokens": prompt_tokens,
                        **result,
                    }
                    row["valid"] = bool(result["raw"].strip()) and (
                        result["finish_reason"] == "stop")
                    display = speech = result["raw"]
                    if arm == "dual":
                        try:
                            parsed = DualOutput.model_validate_json(result["raw"])
                            display, speech = parsed.text, parsed.speech.text
                        except ValueError as error:
                            row["valid"] = False
                            row["validation_error"] = str(error)
                            display = speech = ""
                    row.update(display=display, speech=speech)
                    row["display_checks"] = fact_checks(display, case)
                    row["speech_checks"] = fact_checks(speech, case)
                    rows.append(row)
                    log_path = output / "responses.jsonl"
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write(json.dumps(row, ensure_ascii=False) + "\n")
                    print(json.dumps({"completed": len(rows), "total": len(jobs) * 2,
                                      "case": case["id"], "arm": arm,
                                      "valid": row["valid"],
                                      "seconds": round(row["total_ms"] / 1000, 2)}),
                          flush=True)
    finally:
        await builder.aclose()
        (output / "metadata.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summarize(rows), indent=2),
                                        encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--cases", type=Path, default=ASSETS / "cases.json")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output directory; recorded trials are immutable.")
    if args.repeats < 1 or (args.limit is not None and args.limit < 1):
        parser.error("Repeats and limit must be positive.")
    args.output.mkdir(parents=True)
    asyncio.run(benchmark(args.output, args.repeats, args.limit, args.cases))


if __name__ == "__main__":
    main()
