# GPU Whisper conversation evaluation

This evaluation separates offline recognition from the live conversation path.
The offline benchmark, 27 paced speech turns across two conversations
(14 Whisper, 13 Vosk), repository gates, and production readiness checks are
complete. The local application is running GPU Whisper and GPU Kokoro.
A physical microphone is unavailable.
Generated audio can establish the recorded fixture behavior, but cannot establish
human turn comfort, room-noise tolerance, or speaker echo cancellation.

## Evidence

| Artifact | Scope |
|---|---|
| [Runtime smoke](whisper-runtime-2026-09-08.json) | Actual CUDA model readiness, warm decoding, and GPU memory snapshot |
| [Paired recognition benchmark](voice-asr-comparison-2026-09-08.json) | 52 identical input fixtures, two repeats, two recognizers: 208 offline decodes |
| [Paced flow runner](../tests/voice_whisper_flow.py) | Real HTTP/WebSocket conversation, independent event collection, and continuously paced PCM |
| [Completed Whisper flow](voice-whisper-flow-2026-09-08.json) | 14 full speech turns, 11 additional flow probes, and temporary-data cleanup |
| [Completed Vosk flow](voice-vosk-flow-2026-09-08.json) | 13 full speech turns, eight applicable flow probes, and temporary-data cleanup |
| [Production readiness](voice-production-ready-2026-09-08.json) | Restarted application, warmed CUDA recognizer/synthesis, and unchanged generator identity |
| [Recorder tests](../tests/test_voice_flow_harness.py) | Model-free checks for asynchronous finals, timing labels, mute, held results, integrity checks, and cleanup |
| [Earlier voice evaluation](VOICE_EVALUATION.md) | Preserved Vosk conversation evidence; a separate protocol and run, not a paired timing baseline |

The candidate is Whisper large-v3-turbo through faster-whisper 1.2.1 and
CTranslate2 4.8.2, using CUDA float16. Vosk retains the wake-phrase path; Silero
decides when speech starts and ends. The local Qwen generator and Kokoro CUDA
remain resident. Recognition runs asynchronously so its results must not delay
microphone controls or interruption detection.

## Runtime and offline recognition

The runtime smoke recorded a 2.9897-second complete voice warm-up after earlier
kernel loading. A 3.4133-second generated recording was decoded three times in
0.1383, 0.1394, and 0.1388 seconds; all three transcripts retained “4.30 p.m., not
3 p.m.” Kokoro reported CUDAExecutionProvider and ASR reported CUDA/float16 ready.
The RTX 5090 snapshot showed 23,506 MiB used and 8,682 MiB free with Qwen resident.
An earlier cold first decode took 12.015 seconds before the integration added
actual inference during warm-up. The later measurements do not establish a
fresh-process cold-start time.

The paired benchmark alternated decoder order, used the same PCM for each
backend, and completed all 208 decodes without a decode exception. Warm offline
latency excludes audio preparation, model loading, warm-up, and conversational
endpoint waiting.

| Recorded metric | Vosk | Whisper |
|---|---:|---:|
| Declared critical forms matched | 120/180 | 158/180 |
| Weighted lexical word error rate | 10.1% | 25.2% |
| Median warm offline decode | 0.661 s | 0.131 s |
| Raw nonspeech decodes containing words | 2/8 | 8/8 |

These are fixture metrics, not semantic accuracy. The lexical word-error score
does not equate spelled numbers with digits, so correct numeric formatting can
increase it. A separate manual review identified eight Whisper false negatives
from equivalent forms such as `$1500`, `15 mm`, and `15 cm`. Accepting those
forms gives 166/180; the original JSON and its 158/180 result remain unchanged.
Four nominal matches remain punctuation-sensitive, leaving 162 clear form
matches, four ambiguous cases, and 14 mismatches. Examples include “Wait, do
not. Send it yet.” and “zero point. Zero five dollars” after inserted pauses.
Neither a punctuation-insensitive match nor its correction proves preserved
intent.

The eight equivalent-form checks are both repeats of
`number_correction__am_adam__clean` and
`number_correction__bm_george__quiet` (`not $1500`), plus two unit checks in both
repeats of `new_units__bf_emma__clean` (`15mm`, `not 15cm`). The four ambiguous
passes are both repeats of `short_reversal__am_adam__pause` and
`small_decimal__am_adam__pause`. This audit changes neither the original report
nor the treatment of homophone, name, or identifier errors.

Whisper produced words in all eight raw silence/noise decodes. That is a
material limitation of unguarded decoding. The live evaluation separately
checks whether the actual Silero-gated listener prevents these recordings from
producing speech events or requests; the offline decoder result is retained
regardless of that outcome.

## Paced conversation procedure

The runner creates a disposable application and session with the real
in-process embedder, strict verified retrieval, local generator, ASR, and Kokoro.
It sends one 50 ms PCM frame at a time at wall-clock pace, including silence
during generation and synthesis. A separate asynchronous collector timestamps
every server event. It waits for a final transcript rather than treating a
control acknowledgment as an ASR completion barrier.

The default Whisper run plans 12 ordinary conversation turns plus one continued
utterance and one explicit captured-text send, for 14 full speech turns. The
bounded test cap is 20 seconds; the production 120-second setting is unchanged.
The live report records actual completed/stored turns and individual failures.

| Scenario | Acceptance criterion |
|---|---|
| Wake once and follow up | Subsequent utterances produce a final without returning to wake waiting |
| More than eight seconds of continuing speech | A nonempty replacement partial appears before input ends; one final follows endpointing |
| 0.8-second pause inside a request | The request remains one utterance with a live partial |
| Pause/unpause during a held real recognition result | Acknowledgments arrive within one second while that result is held; stale words do not reappear |
| Interruption with a held result and playback active | Speech-start arrives before release of the old result; only fresh speech reaches final |
| Speech resumes while its first final is held | One final preserves both portions of the unsent request |
| Silence, seeded hiss, and clicks | Three seconds of each in active and playback states produce no speech-start, partial, transcript, or limit event |
| Hard capture limit | The listener pauses with captured text, emits no automatic transcript, and stores no turn before an explicit action |
| Discard or send captured text | Discard adds zero stored turns; explicit send adds one and completes verified generation and synthesis |
| Every completed speech turn | Committed success, exact payload/report verification, and a complete nonempty speech stream |

The held-result probes run real ASR, then temporarily hold one return value in
the evaluator. They test concurrency and stale-result suppression, not normal
decode latency. Playback controls simulate browser playback state; audio is
decoded and checked as WAV data, but is not played through physical speakers.
The flow runner does not test browser AEC, microphone permissions, or perceived
speech quality. Those require the intended microphone and output device.

Run these opt-in commands from the repository root with new report filenames:

```bash
uv run --no-sync python -m tests.voice_whisper_flow --backend whisper --report docs/voice-whisper-flow-YYYY-MM-DD.json
uv run --no-sync python -m tests.voice_whisper_flow --backend vosk --report docs/voice-vosk-flow-YYYY-MM-DD.json
```

Vosk executes the same ordinary paced conversation, nonspeech, and hard-limit
cases. The held-Whisper-result cases are explicitly not applicable, so its
default plan contains 13 full turns. Do not count those skipped probes as passes.
The reports include temporary-data cleanup status and reject overwriting an
existing report. A failed run must remain alongside any subsequent rerun.

## Measured Whisper conversation

The paced Whisper run completed on 2026-09-08 with **14 committed speech turns**
in one isolated conversation. All 12 ordinary turns and 11 flow probes passed
their stated mechanical criteria. Both exact verification checks held and
synthesis completed for every full turn. The temporary application/session data
was removed. The report records 3,795 PCM frames and no scheduling delay exceeding
the recorder's 50 ms threshold.

| Measurement or scenario | Recorded outcome |
|---|---|
| First nonempty partial, 12 ordinary turns | 0.923–1.012 s from input start |
| Partial before input ends | 11/12; the 0.704 s “Books” recording ended before its first partial |
| Final after input ends | 1.430–1.529 s, including patient endpoint waiting |
| Continuing 11.691 s request | First partial at 0.960 s; one final 1.529 s after input |
| 0.8 s internal pause | One combined request, with a partial during input |
| Normal chat request duration | 0.619–1.119 s |
| First synthesized audio chunk | 0.088–0.206 s after the speech request |
| PCM during each normal chat and synthesis | Nonzero frames recorded in both stages for all 12 turns |
| Six active/playback nonspeech probes | No speech-start, partial, final transcript, limit, or error event |
| Held-ASR pause/unpause acknowledgments | 0.707–0.907 ms while the old real result remained held |
| Fresh speech-start in those held-ASR probes | 0.552 s active / 0.580 s playback, including 0.250 s leading silence |
| Speech resumes before held final | Speech-start at 0.615 s; one combined silver-key/blue-folder transcript |
| Capture-limit discard | No automatic transcript; stored turns stayed 13→13 |
| Capture-limit explicit send | No automatic transcript; one explicit verified speech turn, 13→14 |

The noise result establishes gating for these six synthetic recordings only.
It does not erase the raw decoder's eight nonspeech hallucinations or establish
resistance to real household sounds. The control numbers use the local loopback
socket and deliberately held results; they are not browser/hardware latency.
The speech request measurements establish generated audio availability, not
time until physical playback. The runner waits through a fixed endpoint tail
before submitting chat, so adding the columns would not reproduce the browser's
end-to-end timing.

Actual replies preserved the 4:30 PM appointment, its 5:45 PM correction, and
the corrected recall. They retained the full fifty-cents/not-fifty-dollars
contrast and accepted the one-word “Books” response. The clarification sequence
did not state a new budget; the model asked for that missing value and later
retained $40 and six guests. This is one observed sequence, not a general
recognition or reasoning improvement claim.

Strict response brevity still failed once: “Repeat only the three colors”
received a recap of the folders' contents before “Blue, green, red.” That turn
passes transport, verification, and synthesis while failing the narrower answer
instruction. The capture-limit fixture repeats the same sentence to reach the
cap; Whisper collapsed that repetition into one sentence, so this probe does
not establish verbatim retention of a long varied request.

## Matching paced Vosk run

The Vosk run completed the same 12 ordinary scenarios plus explicit capture-limit
send, for **13 committed speech turns**. All ordinary mechanical checks and all
eight applicable probes passed. The three held-Whisper-result probes were
explicitly not applicable. Capture-limit discard kept 12 stored turns; explicit
send added the thirteenth. Temporary data was removed. The recorder sent 3,370
PCM frames and recorded no scheduling delay exceeding 50 ms.

| Ordinary-turn measurement | Vosk | Whisper |
|---|---:|---:|
| First nonempty partial | 1.208–2.027 s | 0.923–1.012 s |
| Partial while input still playing | 10/12 | 11/12 |
| Final after input ends | 1.270–1.407 s | 1.430–1.529 s |
| First partial for the 11.691 s request | 2.027 s | 0.960 s |
| Six nonspeech gating probes | 6 passed | 6 passed |

Whisper produced earlier partials and slightly later finals in these recorded
runs. The asynchronous final decode follows the endpoint wait. These are
sequential runs using matching prompts, generated voices, pacing, and endpoint
settings, with input audio synthesized separately. The offline benchmark alone
guarantees identical paired PCM. The flow runs are not a repeated or randomized
performance experiment.

Actual Vosk transcripts included `guess` for `guests` and `read on below` for
`red envelope`. Both runs recognized the 5:45 PM correction and recalled it.
In the Vosk run, the earlier “four hours and thirty minutes” statement received
a clarification request; the full cents-contrast repetition received a recap
of the whole conversation. These are observed model responses to the recorded
contexts. They do not isolate transcription as the sole cause or establish a
general quality difference from one conversation per backend. Mechanical passes
continue to mean completed and verified speech delivery, not universal adherence
to the user's request.

## Remaining acceptance work

1. Exercise the intended microphone, browser, and output device with human
   speech, pauses, corrections, room sounds, and speaker echo. The microphone
   path remains untested because no input device was connected.
2. Treat punctuation and numbers as meaning-bearing in future fixtures. Keep
   raw transcripts beside form scores; do not silently normalize away the
   four ambiguous pause cases.
3. Retest wake accuracy separately. The wake recognizer remains Vosk, and the
   earlier evaluation's missed/false wake examples are not resolved by these
   dictation measurements.
4. Continue checking instruction compliance and memory corrections independently
   of mechanical speech success. No voice-prompt quality change follows from
   this single conversation.

## Verification status

The recorder's eight model-free checks passed in 0.82 seconds. Final repository
Ruff reported `All checks passed!`. The final UI run reported:

```text
tests 103
pass 103
fail 0
duration_ms 178.9193
```

TypeScript and Vite completed successfully: 41 modules, built in 90 ms. The
full Python suite reported:

```text
534 passed, 2 skipped, 1 warning in 42.08s
```

The skips are opt-in Docker tests; the warning is the existing Starlette/httpx
deprecation. `git diff --check` was clean.

## Running local configuration

The restarted application on port 8080 (PID 25912) reports Whisper large-v3-turbo
ready on CUDA/float16 and Kokoro using CUDAExecutionProvider. The in-process
embedder sentinel matches the research identity, and the generator is healthy;
Qwen's existing PID 20008 was preserved.

The first listener connection, including warm-up, took 2.8602 seconds; a second
connection took 0.0027 seconds. Both returned the waiting state for “Hey Idris.”
These readiness checks created zero chat turns. They establish service readiness,
not the still-unavailable physical microphone journey.
