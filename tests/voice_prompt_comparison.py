"""Interleaved, opt-in prompt comparison using captured verified memory bytes."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from recollect.api import _VOICE_INSTRUCTIONS
from recollect.config import RecollectConfig
from recollect.engine.date_context import current_date_context
from recollect.engine.generator import (
    Generator,
    GeneratorSettings,
    new_generation_trace,
)
from recollect.engine.subagent import run_subagent_tool

CANDIDATE = (
    "You are speaking with the user in a live voice conversation. "
    "Give the useful answer first. For an ordinary question, aim for twenty "
    "to forty-five words in one to three short sentences, then stop and let "
    "the user take their turn. Do not add an offer to help or a follow-up "
    "question unless you need clarification to answer. Treat remembered "
    "replies as context, not examples of how long to speak. "
    "When the user asks for detail, multiple steps, exact wording, or a "
    "specific format, fulfill that request completely even if it takes longer. "
    "Preserve requested quotations, negation, amounts, units and distinctions; "
    "do not shorten away their meaning. Ask a brief clarifying question "
    "instead of guessing an ambiguous change. If a transcribed time or amount "
    "is unclear, confirm it rather than silently changing the value. "
    "Use conversational prose without headings, tables, or lists unless "
    "requested. Say amounts naturally, such as four hundred dollars per month. "
    "Mention source names briefly; include exact links or code on screen when "
    "the user requests them. Finish naturally without truncating a sentence."
)

CASES = {
    "comparison", "plants", "tradeoff", "organization", "units", "no_table",
    "detail", "cents", "ambiguous", "memory_beyond_recent", "update_again",
}


async def main(args):
    config = RecollectConfig.from_env()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    rows = [row for conversation in baseline["conversations"]
            for row in conversation["turns"] if row["id"] in CASES]
    settings = GeneratorSettings(
        base_url=config.generator_base_url, model=config.generator_model,
        api_key=config.generator_api_key, timeout_s=config.generator_timeout_s,
        thinking=config.generator_thinking, max_tokens=config.generator_max_tokens,
        temperature=config.generator_temperature,
    )
    generator = Generator(settings)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "method": "Three interleaved repetitions per case with identical recognized "
                  "input and captured verified context. This isolates prompt style; "
                  "it does not rerun ASR, retrieval or TTS. Normal model sampling "
                  "and token budget remain unchanged. Tools remain available.",
        "baseline_instructions": _VOICE_INSTRUCTIONS,
        "candidate_instructions": CANDIDATE,
        "cases": [],
    }
    try:
        for row in rows:
            entry = {"id": row["id"], "input": row["asr"]["transcripts"][0],
                     "runs": []}
            report["cases"].append(entry)
            for repeat in range(3):
                order = ("baseline", "candidate") if repeat % 2 == 0 else (
                    "candidate", "baseline",
                )
                for variant in order:
                    instruction = (CANDIDATE if variant == "candidate"
                                   else _VOICE_INSTRUCTIONS)
                    system = config.system_prompt + "\n\n" + instruction + "\n\n"
                    system += current_date_context(datetime.now(UTC).date())
                    context = row["chat"]["memory_payload"]
                    trace = new_generation_trace(
                        settings=settings, system_prompt=system,
                        context_block=context, user_message=entry["input"],
                    )
                    messages = generator.build_messages(
                        system_prompt=system, context_block=context,
                        user_message=entry["input"],
                    )
                    async for _ in generator.stream(messages, trace=trace,
                                                    tools=[run_subagent_tool()]):
                        pass
                    result = {
                        "variant": variant, "repeat": repeat + 1,
                        "reply": trace.response_text,
                        "words": len(trace.response_text.split()),
                        "generation": trace.model_dump(mode="json"),
                    }
                    entry["runs"].append(result)
                    args.report.write_text(json.dumps(report, indent=2),
                                           encoding="utf-8")
                    print(json.dumps({"case": row["id"], **result}), flush=True)
    finally:
        await generator.aclose()
        report["finished_at"] = datetime.now(UTC).isoformat()
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.report.exists():
        parser.error("Choose a new report path to preserve earlier results.")
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(arguments))
