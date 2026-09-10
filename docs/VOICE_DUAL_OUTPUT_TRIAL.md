# Qwen display/speech JSON trial

This prototype was not selected for production. The final approach keeps one
Qwen response with short conversational guidance and fixed sentence pauses;
see [VOICE_LISTENING.md](VOICE_LISTENING.md). The results below describe the
recorded pilot, not a claim about later reruns with a revised base prompt.

## Result

The isolated prototype runs: Qwen produces a display response and a speech
response in one schema-constrained generation, and Kokoro synthesizes the
speech response. The prompt contains the complete JSON schema, an example
output, the memory obligations, and an explicit description of the supported
Kokoro input. No production prompt, chat store, retrieval implementation,
model setting, or speech endpoint was changed.

The first memory pilot found a concrete display/speech disagreement. It does
not establish that this format preserves reasoning quality and should not be
used as justification for enabling it by default. Nine actual recordings are
available in the listening trial, with the exact generated text visible.

## Contract and implementation

The shape of one response is:

```json
{
  "text": "I'd try the smaller change first. We can adjust it after listening.",
  "speech": {
    "text": "Well, I'd try the smaller change first. Then we can listen and see what needs adjusting."
  }
}
```

Both objects forbid extra properties. Both text fields are required strings
of 1–4,000 characters; local validation additionally rejects whitespace-only
strings. The actual Pydantic-generated JSON schema, including its nested
definition, is embedded verbatim in the prompt and supplied to llama-server
as `response_format: json_schema`. Invalid or truncated results are recorded,
not silently repaired or retried until an acceptable answer appears.

Qwen receives the existing system prompt, current voice instructions, and
date context before the additional contract. The contract distinguishes the
outer JSON format from prose inside the fields. It requires the same facts,
names, quantities, negations, conditions and uncertainty in both outputs.
Speech-writing guidance allows contextual acknowledgments, contractions,
transitions and occasional hesitation without a filler quota.

For this first trial, `speech.text` is ordinary English prose and punctuation.
Our adapter extracts it before the existing spoken-text normalization and
Kokoro phonemization. The prompt explicitly says that Kokoro does not consume
JSON, SSML, bracketed emotions or narration instructions. Pronunciation markup,
model-controlled speed and pitch are not supported by this trial's schema.
This is a scoped test of Qwen's wording, not a claim that every documented
Kokoro frontend feature is available in our installed ONNX wrapper.

The initial transport probe found that llama-server rejects the unanchored
regular-expression constraint emitted by an earlier schema draft. That
constraint was removed from the schema and replaced by local nonblank
validation before the benchmark. No model response was rewritten to pass.

Implementation:

- `src/recollect/voice_trial.py`: schema, example, prompt and paired runner.
- `src/recollect/voice_trial_assets/cases.json`: synthetic memories,
  questions, lexical checks and written answer criteria.
- `src/recollect/voice_trial_assets/auditions.json`: three conversational
  scenarios for listening, separate from the memory pilot.
- `src/recollect/voice_trial_listening.py`: validates recorded JSON and
  renders its speech field using fixed Kokoro settings.
- `tests/test_voice_trial.py`: rejects unusable structures, preserves
  truncated raw output, and verifies that TTS uses the validated speech field
  rather than the displayed answer or stale derived data.

## Memory pilot design

Eight authored memory scenarios were each run three times under both prompts:
48 generations, including 24 dual-output objects. Cases cover a clarification,
a corrected appointment, budget arithmetic, combined constraints, a missing
fact, a multi-step responsibility handoff, a negative preference, and unresolved
contradictory notes. No user conversations were opened or copied.

Each pair has identical memory bytes and user question. Hashes confirm that
each case's memory remained identical across all six generations. Cases and
repetitions were shuffled with a fixed schedule; the first arm alternated by
pair. Seeds 12090–12092 were matched between arms. Seeds do not guarantee
bit reproducibility or equivalent sampling under different grammars.

The live local model was `Qwen3.8-27B-UD-Q4_K_XL.gguf`. Both arms retained
the configured temperature 0.7, output budget 4,096 tokens, and
`enable_thinking=false`. That last setting was already the deployment setting;
it was not changed for this experiment. Context preflight retained the entire
memory and reserved the same output allowance for both arms.

The comparison measures the combined effect of the extra instructions,
dual output and schema grammar. It does not isolate which of those factors
caused a difference. Frozen supplied memories test generation from memory;
they do not benchmark retrieval, packing, embedding, episode ingestion,
long conversations, tool routing, or future recall of these dual responses.

## Observations

| Measurement | Current voice prompt | Dual-output prompt |
| --- | ---: | ---: |
| Completed responses | 24/24 | 24/24 |
| Schema-valid dual responses | Not applicable | 24/24 |
| Display responses matching every lexical sentinel | 23/24 | 23/24 |
| Speech responses matching every lexical sentinel | 23/24 | 23/24 |
| Median generation completion time | 740.66 ms | 1,866.64 ms |
| Median first content token | 353.36 ms | 411.80 ms |
| Median output tokens | 27.5 | 92.5 |
| Median checked prompt tokens | 565.5 | 1,328.5 |

Times cover the streaming generation call, including any server scheduling;
they exclude the separate context preflight, synthesis and browser playback.
The first content token for the dual arm can be JSON punctuation, not useful
speech. The adapter waits for a complete valid object before synthesis.
These are short-memory measurements under one local workload, not general
latency guarantees. The dual contract added 763 prompt tokens in these cases.

The lexical numbers above are deliberately labeled as lexical sentinels,
not accuracy scores. Reviewing all 48 responses exposed their limitations:

- **Constraints, repetition 1, dual:** the displayed answer correctly chose
  Birch but said Cedar was “too expensive.” Cedar cost $90 against a $125 cap;
  it failed because it required cloud access. The speech answer correctly
  gave cloud access as the reason and did not repeat the false price claim.
  Both passed the lexical checks. This is a substantive supporting-fact error
  and a cross-field disagreement in one of 24 dual objects.
- **Missing fact, repetition 2, dual:** the answer appropriately admitted
  missing the bicycle's serial number. Its curly apostrophe made the ASCII
  lexical pattern miss it. Inspection confirmed U+2019, not corrupt encoding.
  The raw result and original score were retained.
- **Handoff, repetition 2, baseline:** the answer correctly named Noor but
  did not explain the covering arrangement, so the explanation sentinel
  failed. The question requested the person, so this is not by itself a wrong
  answer. Some other handoff replies inferred gender or being away, and one
  dual reply loosely described the normal rule as suspended. Those additions
  were not necessary to answer the question.
- **Negative preference:** all responses accepted decaf, consistent with
  the user's stated preference. Baseline responses also described decaf as
  “without the caffeine” or “without the stimulant,” an overstatement beyond
  the supplied memory. Selecting the right option does not validate every
  supporting phrase.
- **Clarification:** one dual answer added fixed speed to fixed voice. Fixed
  speed was an experiment instruction rather than a remembered user request.
  This illustrates potential mixing of presentation instructions with the
  recalled conversational answer.
- Appointment corrections and budget calculations had the expected requested
  answers in all repetitions. Missing serial numbers and uncertain door codes
  were not replaced by invented definite answers.

Fourteen of 24 dual objects had byte-identical display and speech strings.
Identical wording is reasonable for a simple fact, but the schema alone does
not create a distinctive speaking style. The observed discrepancy is a reason
for further evaluation; this small pilot does not establish a general regression
rate or a statistically reliable advantage for either prompt.

## Listening trial and variance

Three separate dialogues cover a clarification, disagreement and the next
practical step. Each was generated twice with both prompts. The page includes
the first baseline response and both dual responses per dialogue: nine clips,
selected by repetition index before judging their quality. The unused second
baseline remains in the local generation record. No generated wording was
manually improved before synthesis.

All clips use the same Kokoro 82M artifact, 75% Heart / 25% Bella voice blend,
speed 1.0, and production chunking. The blend is a controlled setting carried
from earlier listening, not a newly established winner. CUDA was enabled in
the synthesis session; ONNX Runtime also assigned some operations to CPU.
No additional silence or loudness normalization was applied. The nine WAVs
contain 128.66 seconds of mono 24 kHz audio with no saturated PCM samples.
Audio hashes, duration, normalized speech text and synthesis timings are
recorded in the manifest.

The “Another response” choice is variation from a new Qwen generation given
the same question, memory and prompt. It is not pitch jitter or a different
voice. Four of the six dual dialogue objects had identical display/speech
strings. The next-step case varied substantially: one response suggested a
simple factual question, while another proposed a longer sequence of tests.
That changes the proposed content as well as its phrasing. It should not be
mistaken for controlled acoustic variance. The longer response also shows
that the request for brevity is not reliably obeyed yet.

The page shows the preceding conversation, both text fields, raw Qwen output,
and a comparison with the current prompt. Feedback stays in device storage
until the listener downloads it. The listening result is still pending; no
naturalness improvement is claimed.

## Before any production rollout

The next benchmark should use held-out cases and longer memory blocks,
including repeated corrections, negation and missing information. Score
supporting claims and cross-field agreement, not just final choices or JSON
validity. Compare a shorter contract and fewer redundant explanations as
separate experiments rather than editing this run's prompt or scores.

A production design must also define which representation becomes the next
episode. A reasonable proposal is readable `text` as the canonical reply for
memory, with the raw JSON and spoken realization retained as explicit trace
data. Feeding both versions back as separate conversational facts would
duplicate content and consume additional memory budget. That proposal needs
an isolated multi-turn ingestion/recall benchmark before adoption.

The existing shadow verification establishes memory construction agreement;
it does not prove the model's two answers are semantically equivalent. It
must remain unchanged. Tool routing and intermediate work status should also
remain separate from the final display/speech response contract.

## Records and rerunning

Intentional local experiment records are retained under:

- `var/listening/issue-12-dual-20260909`: all 48 requests and raw responses,
  exact prompts, schema, cases, metrics and unchanged lexical scores.
- `var/listening/issue-12-dual-dialogues-20260909`: all 12 dialogue generations.
- `var/listening/issue-12-dual-listen-20260909`: nine audio clips and static page.
- `var/listening/issue-12-dual-pilot-20260909` and
  `var/listening/issue-12-dual-pilot2-20260909`: initial compatibility probes,
  excluded from the benchmark.

```powershell
uv run --no-sync python -m recollect.voice_trial --output var/listening/dual-new-run --repeats 3
uv run --no-sync python -m recollect.voice_trial --output var/listening/dual-new-dialogues --repeats 2 --cases src/recollect/voice_trial_assets/auditions.json
uv run --no-sync python -m recollect.voice_trial_listening --source var/listening/dual-new-dialogues --output var/listening/dual-new-audio
```

Use fresh output paths; the runners refuse to overwrite prior recordings.
The page's benchmark prose describes this recorded run, not future runs.
Update the report and page together if publishing a different experiment.
