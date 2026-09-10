# Kokoro listening suite (issue #12)

This documents the listening experiments and the selected production changes
for issue #12. The standalone kit generators do not open a conversation store,
start chat/ASR models,
capture a microphone, or alter `.env`. Audio files and browser ratings are
intentional evaluation artifacts. They contain the fixed sample prose, not
private conversation history.

## Selected behavior

After listening to fixed 250 ms, fixed 500 ms, and duration-based 250–1000 ms
options, the user selected **fixed 500 ms between sentences for now**. Production
renders sentences separately and appends exactly half a second of zero PCM
after each nonfinal sentence. It keeps bounded native calls inside long
sentences, without adding pauses at those internal splits. Streaming and replay
use the same renderer. This is an explicit listener preference, not a measured
general improvement in naturalness or fatigue.

Qwen keeps the original memory/system prompt and produces one response. A short
voice-only addition asks for conversational paragraphs, usually one to three
sentences, contractions and natural punctuation variety, without headings or
point labels. Detailed answers retain the normal token budget. The dual-output
JSON prototype is preserved as an experiment, not enabled in the chat path.

The final pause audition held the voice at 75% Heart / 25% Bella and speed 1.0.
Production currently retains `RECOLLECT_VOICE_NAME`; the blend remains available
in the listening tools. The source changes require an ordinary server restart
to become active; generating a kit does not change a running server.

## Final audition record

The private [pause comparison](https://recollect-listening-room.muzafferozen123.chatgpt.site/pause-options/)
contains three identical Qwen replies under each pause policy. Earlier pages
remain at `/simple-prompt/`, `/sentence-pauses/`, `/punctuation/`, `/qwen-kokoro/`
and `/round-2/`. Access is limited to the site's owner. The static site's
deployment history is separate from this repository; WAVs and ratings are
intentional local artifacts under ignored `var/listening/`.

The final kit is `var/listening/issue-12-pause-options-20260909`. Its manifest
records the exact sentences, file hashes, PCM stem hashes, frame boundaries and
pause schedules. All nine files reuse cached mono 16-bit, 24 kHz sentence PCM;
only zero samples at sentence joins differ. Fixed 500 ms means 12,000 added
frames per join, with no trailing pause. The 250 ms files were verified
byte-identical to the preceding sentence-pause audition.

| Reply | Sentences | Varied pauses in ms | Selected pauses in ms |
| --- | --- | --- | --- |
| Making a simple plan | 4 | 256, 1000, 250 | 500, 500, 500 |
| Explaining a tradeoff | 3 | 1000, 250 | 500, 500 |
| Thinking something through | 5 | 250, 761, 1000, 345 | 500, 500, 500, 500 |

The variance experiment mapped each reply's nonfinal sentence durations
linearly to 250–1000 ms, rounded to whole milliseconds. Equal durations would
receive 625 ms. This changes total silence as well as its distribution, unlike
the budget-matched round-two experiment below. It is not a production policy.

Before these comparisons, punctuation-only changes still sounded too abrupt
to the listener. The installed audio-only Kokoro ONNX graph does not provide
phoneme timings, and its native pause options do not guarantee a pause at every
sentence in separately trimmed application chunks. Therefore the final change
inserts silence at known sentence boundaries instead of appending `...` to
periods. It does not attempt to splice inside existing generated speech.

The simplified-prompt audition produced three plain paragraphs without headings;
two exceeded the suggested three sentences. This illustrates the prompt's soft
constraint. Neither this small sample nor the isolated
[memory pilot](VOICE_DUAL_OUTPUT_TRIAL.md) establishes a general memory-quality
result. Live turn-taking, first audible latency and sustained listening comfort
remain follow-up evaluation work.

## Use the prepared kit

Open its `index.html`, or serve **only the kit directory** on a loopback port.
It contains all audio and page assets; internet access and Recollect are not
required. Use headphones or speakers at a comfortable, consistent volume.

1. Enter your listening setup if useful.
2. Play A and B, then rate naturalness, clarity, and comfort from 1 (very low)
   to 5 (very high). Comfort means pleasant listening without effort or fatigue.
3. Choose A, B, a tie, or neither, and optionally explain what you heard.
4. Save and continue. The last comparison uses a longer passage; take breaks.
5. Export ratings. Return another day and start a new session for a fresh order.

Draft ratings save automatically when browser storage is available. Each new
session retains prior sessions and shuffles short-comparison order and A/B
assignments independently. Long listening stays last. Playback is exclusive:
starting one recording pauses the other. Navigating away stops the recording.
The player permits replay and seeking; reaching the end is a convenience gate,
not proof of attentive listening. Exports record playback events and reveals.

Revealing settings is optional and recorded. Concealment is for informal
listening, not cryptographic blinding: the static manifest contains identities.
Exports contain all sessions, exact profile assignments, synthesis metadata,
and clip hashes. Keep the whole kit together. File-URL browser storage can vary;
export before closing, or use a stable loopback URL for repeated sessions.
Discard removes the selected session from saved progress. Undo restores the
most recently discarded session until another discard or a page reload.

## What is compared

There are 11 pairs and four texts: a short reply, a conversational explanation,
names/numbers/technical terms, and a longer passage.

| Factor | Reference | Candidate |
| --- | --- | --- |
| Phrasing, four texts including long | Pre-issue-12 first-120 / later-240 character chunking | Sentence groups up to 420 characters |
| Voice, three pairs | Configured baseline voice | `af_bella`, `am_michael`, `am_puck` |
| Pace, two pairs | 1.0× | 0.95× or 1.05× |
| Pauses, one pair | No extra boundary silence | 180 ms after completed sentence groups |
| Blend, one pair | Configured baseline voice | 75% baseline, 25% `af_bella` |

All factors after phrasing use the sentence-grouped reference. This is an
experimental reference, not an assertion that grouping improves speech. Only
one factor changes per pair. The short text may have identical segmentation
under both strategies; it is a useful low/no-difference check. If the configured
baseline is already one of the candidate voices, that pair is also a control.

Historical baseline generation calls the shared `spoken_text` and `_speech_pieces`
functions, with the configured voice, US English, and the installed Kokoro
defaults, without the new production sentence splitting or 500 ms pauses.
The `current` grouping identifier in these experiments means that historical
baseline. Sentence grouping is limited to this fixed corpus, not a general
sentence tokenizer. Long sentences use the existing bounded split fallback.
The explicit pause experiment inserts silence only between complete sentence
groups; it does not assume a standard ONNX model supplies phoneme durations.

## Generate another kit

From the repository root, with the existing pinned environment and assets:

```powershell
uv run --no-sync python -m recollect.voice_listening --output var/listening/issue-12-new
```

Use a new directory each time. The generator refuses to overwrite existing
audio. It loads TTS only, follows the same CUDA selection and fallback refusal
as production, warms once, and records model/voice hashes, package version and
source hash, device/providers, model outputs, generation order, per-chunk
synthesis times, duration, peak amplitude, and clipped sample counts. The saved
voice setting determines the baseline. Model assets are never downloaded.

## Evidence and limits

Clips are assembled WAVs, preserving synthesized joins but excluding live
transport and browser scheduling gaps. They are not recordings of the live
conversation pipeline. Timings describe warm offline synthesis under the
machine's concurrent workload, not first audible playback or chat latency.
There is no loudness normalization or artificial simulation of network stalls.
The longer passage supports a brief fatigue comparison, not a long-term study.
Repeated listening uses the same saved waveforms; repeat generation separately
if investigating synthesis variability.

These samples help shortlist settings. They do not establish an improvement.
After repeated user feedback, evaluate any preferred candidate in live playback,
including start delay, gaps, interruption, intelligibility, and sustained comfort,
before claiming broader quality gains or closing the remaining evaluation work.

Upstream references:
- [Kokoro voice guidance](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)
- [ONNX voice blending example](https://github.com/thewh1teagle/kokoro-onnx/blob/main/examples/with_blending.py)

Model-free regression checks:

```powershell
uv run --no-sync pytest tests/test_voice_listening.py
node --test tests/listening_ui.test.cjs
```

## Round two: blend and controlled variance

Generate with `--round 2 --output var/listening/issue-12-round2-new`.
Eight pairs compare the blend on current chunking, authored boundary placement,
extra pause amount, pause variance on two texts, overall speed, subtle pace
variance, and a combined candidate against the original on the longer text.
The last comparison changes several factors and cannot attribute a preference.

The fixed-corpus units deliberately split selected clauses and sentences.
Within each unit, production bounded chunking remains in use. This changes
context relative to original chunking; the boundary-control pair tests that
effect. It is not a general segmentation implementation for the live app.

Fixed added silence is 100 ms at clauses, 220 ms at sentences and 380 ms at
paragraphs. The varied version uses preceding spoken duration, including
earlier clauses in that sentence, bounded to 1–10 seconds. It centers durations
within each boundary class and applies up to ±90% of its base pause across
that nine-second range. Centering and integer sample allocation preserve each
class's total silence exactly. A class with only one boundary stays fixed.
There is no extra silence after the final utterance.

Pause-only pairs share exact cached 16-bit PCM speech, without resynthesis,
crossfades, normalization, or pitch changes. Stem hashes and frame offsets let
the comparisons be audited. Added silence is distinct from pauses already
inside Kokoro's waveform. The installed audio-only ONNX graph does not supply
phoneme alignment; we do not insert silences inside already rendered phrases.

Pace tests reuse the 1.0× candidate's pause schedule. One compares steady 1.0×
with 1.05×. Another compares steady 1.025× with native per-unit rates between
1.01× and 1.04×: longer units receive slower rates, centered so the word-weighted
mean is 1.025×. This matches the mean rate setting, not total rendered duration.
The profile uses the same schedule on every replay, with no random jitter.
These are experimental policy choices, not measured optimal values.

## Qwen prompt comparisons

The research rationale is in [VOICE_CONVERSATION_RESEARCH.md](VOICE_CONVERSATION_RESEARCH.md).
Generate original-versus-current voice-prompt recordings with:

```powershell
uv run --no-sync python -m recollect.voice_punctuation_listening --output var/listening/prompt-new
```

This uses the original voice instructions from Git revision `c5a3e00` and the
current source prompt, with the same synthetic context for each arm. It needs
the configured Qwen server and local Kokoro assets, and uses the historical
no-added-pause renderer so prompt effects remain separate from pause policy.
Prompts, raw responses and audio metadata are saved with each run. Generations
can differ; the prepared site's recordings stay unchanged.
