# Continuous-task runtime evidence

Measured locally on 2026-09-09 against the implementation based on `ce70db5`.
These are mechanical observations, not a claim of improved answer quality or a
long-duration reliability guarantee. See [operation and configuration](SUBAGENT_TASKS.md).

## Deployment inspected

- Windows, RTX 5090, 32607 MiB VRAM, NVIDIA driver 610.74.
- llama-server `b10360-90e6a9131`, executable SHA-256
  `125e0938a280cba46c803a60178e51826203e030abacce03367a22108720f7ac`.
- Qwen3.8-27B-UD-Q4_K_XL.gguf from the installed Hugging Face snapshot
  `f1bfb127c64f7072bdd2cad55f258b9c8b2910fe`.
- One model, GPU offload **66/66 layers**, q8_0 key/value cache, flash attention,
  Jinja templates, loopback port 8000. Single-slot context 32768; candidate
  two-slot launch uses total context 65536, preserving 32768 per slot.
- Whisper large-v3-turbo on CUDA float16 and Kokoro CUDA loaded and exercised;
  CPU Vosk/Silero and the unchanged in-process CPU embedder remain separate.
- Doctor and the serving process both matched the research embedding sentinel
  `baecf77627380f36f75a69c4454b064d886133f04255c5e5b4d3f24f00e7c4b8`.
- Docker Linux engine, rebuilt `recollect-opencode-sandbox:1.18.18`, image config
  digest `a921458188154193b0ce54bb93f1859e89cdc0d363f9bdb2dec7738c366faa0a`.

An initial model launch answered health requests but lacked CUDA dependencies.
That CPU run was discarded before benchmarking. The corrected launch used the
existing launcher's scoped CUDA DLL path and verified native GPU-offload logs.
Docker initially failed on a stale socket; reopening Docker Desktop restored the
engine. Neither a host OpenCode fallback nor an environment reinstall was used.

## Native OpenCode protocol

Two isolated, manager-owned runs exercised the actual pinned container and Qwen.
The second deliberately disabled event consumption, requiring native history
reconciliation to recover reports.

| Check | Observed result |
| --- | --- |
| Steering during active native work | Revision 1 then revision 2 accepted in the same parent session. |
| Semantic report carrier | Actual `report_message` calls recovered as accepted/finding/question messages. |
| Native child agent | Parent used a general child to read the seeded file. |
| Revised artifact | `result.md` contained `revision-two` followed by the seed `7`. |
| Checkpoint ordering | Native session status had no busy work before export; scratch was empty after cleanup. |
| Restore | A fresh native session read the saved file. |
| Cancellation while awaiting input | Mailbox cancellation stopped the restored task and retained its file. |
| Lost event channel | The same behavior passed using native message history alone. |

The complete probe sequences took 24.35 and 20.92 seconds. In the first run,
nine model requests used 5585–7745 prompt tokens, with recorded inference times
of 158–3770 ms. These two sequences establish protocol behavior for the exercised
paths; productive continuation at the native step cap, repeated no-progress
blocking, shutdown races, and overflow failures also have deterministic tests.
The opt-in real Docker containment/lifecycle suite passed both tests.

## Scheduling, cache, and voice probe

The reusable [probe script](../scripts/probe_task_inference.py) warms GPU voice
without microphone capture or conversation writes. It alternates **synthetic**
main/build/general/compaction prefixes, then starts a 512-token background output
and a 64-token foreground output 150 ms apart. It also synthesizes and transcribes
a fixed sentence while inference runs. Each profile ran three repetitions.
These family labels do not claim to reproduce the full native OpenCode prompts.

| Measurement | One slot | Two slots |
| --- | --- | --- |
| Foreground whole model response, three trials | 9.655 / 9.688 / 9.686 s | 1.388 / 1.399 / 1.397 s |
| Foreground model first token, three trials | 8.632 / 8.652 / 8.656 s | 0.139 / 0.152 / 0.152 s |
| Background whole response, three trials | 8.609 / 8.636 / 8.630 s | 8.783 / 8.822 / 8.775 s |
| Peak sampled GPU memory | 22575 MiB | 23947 MiB |
| TTS synthesis during inference | 406–452 ms | 188–196 ms |
| Whisper transcription during inference | 582–628 ms | 584–641 ms |

Memory was sampled about every half-second, so these are observed peaks, not an
allocation guarantee. Both profiles returned the expected fixed transcription.
Voice timings are synthesis/transcription durations, not microphone-to-speaker
latency. Three samples do not establish a service-level p95 target.

Initial cold prefill for the roughly 3800–4000-token prompts took 1.24–1.37 s.
An immediate return to main reused 4010 tokens and processed four; a return to
build reused 3775 and processed four. Changing the batch marker did not preserve
all families equally: observed main/build reuse could fall to 251/16 tokens,
while general/compaction reused 3263/3424. Two permanently warm prompts therefore
must not be assumed. The implementation reuses the model server's existing
prompt cache and records observed hits; it adds no application KV-cache manager.

A separate two-slot stress run used **30426 foreground prompt tokens** and
**30191 background prompt tokens**, with output caps of 64 and 2048 respectively.
The foreground first token took 14.806 s and its whole response took 16.122 s;
background completion took 50.744 s. Peak sampled VRAM was 23863 MiB, with TTS
1069 ms and ASR 903 ms. No OOM occurred. Large competing prefills still cause
substantial delay; two slots do not make long uncached contexts instantaneous.

The application now supports an opt-in two-lane admission policy: at most one
foreground request and one background request. Native child fan-out cannot
consume the conversation lane. Startup verifies the actual server slot/context
profile, and the deployed template/tokenizer checks each request's envelope.
Overflow fails explicitly; verified RECENT memory is never shortened to fit.
The default remains one slot; changing the profile is an explicit deployment
setting, not an automatic GPU-memory guess.

## Application checks and remaining limits

Live checks use an isolated data directory, not the user's saved conversations.
Initial end-to-end checks produced a Markdown file, accepted revision 2, created
a linked revision with a distinct downloadable hash, and preserved the previous
version. A different conversation's download returned 404. A retried originating
request returned the existing task in 24 ms without a duplicate dispatch.
Cancellation was acknowledged in 9 ms and observed terminal in 317 ms.

The actual initial chat acknowledgment took 5.553 s and an unrelated arithmetic
reply took 0.822 s. These are application response timings, distinct from the model
first-token numbers above: chat buffers drafts to filter internal tool syntax.
Notifications advanced separately while the conversation count stayed fixed.
A live pure-status query exposed missing explicit routing; continuous replies
now require a typed dialogue/status decision before memory is committed.

After the fix and application restart, three natural status variants completed
in 4.210 / 3.954 / 3.747 s with `committed=false`; the episode count stayed at
three. A mixed status request containing a new fact committed once in 5.224 s.
The original and revised artifact hashes still matched. A new required-routing
delegation completed and produced another valid Markdown download. Notification
speech returned two 24 kHz WAV chunks: first chunk 238 ms, total 378 ms, without
audio playback, microphone capture, or additional conversation entries.

Initial implementation validation output:

```text
All checks passed!
952 passed, 1 skipped, 2 warnings in 80.26s (0:01:20)
# pass 121
# fail 0
✓ built in 93ms
```

The full Python run enabled the real Docker tests. Existing tests were not
rewritten. The remaining warning categories are Starlette's HTTPX integration
deprecation and a Pydantic settings forward-reference warning. Test services
were stopped afterward, restoring the initial idle application/model state;
Docker Desktop remained running. The saved environment was unchanged at that stage.

The connected UI automation surface reported no available browser. UI behavior
is covered by the client tests and production build; visual browser interaction
and live microphone barge-in were not verified in this environment. This work
does not establish multi-day soak reliability, real OOM recovery under every
driver condition, guaranteed cache residency, or a latency SLA for arbitrary
prompt lengths. Unit tests exercise bounded failure and recovery branches;
the measurements above identify which runtime behaviors were actually exercised.

## Delivery and memory follow-up

Subsequent local use exposed a file-location answer being classified as ordinary
memory and task acknowledgments being ingested. Routed replies now require
explicit substantive `memory_reply` text for ingestion; task starts and controls
default to display-only. A file-location question is answered directly from the
artifact records. An isolated Qwen check exercised file/progress questions,
document delegation, a mixed request with a user fact, and an ordinary explanatory
answer. Operational turns were not committed; the latter two retained their
substantive responses. An empty memory selection exposed in that check now means
no memory rather than dropping the visible reply, covered by regression tests.

Verified artifacts can also be copied to a configured desktop Downloads directory.
Tests cover existing-name collisions, immutable revisions, retries after restart,
and failed delivery with the archive still available. The desktop launch now uses
Qwen port 8001; the earlier benchmark port above records the original experiment.
The local continuous-mode and Downloads settings were enabled after these changes.

Pre-PR validation on Windows:

```text
All checks passed!
969 passed, 3 skipped, 2 warnings in 70.73s (0:01:10)
# pass 121
# fail 0
✓ built in 90ms
```

This final run used the normal suite; the two Docker opt-in tests are included in
the earlier Docker-enabled result. Full startup again verified Docker, Qwen GPU,
the embedding sentinel, and warmed Whisper/Kokoro CUDA. Services remain running.

## Frozen conversational replies after restart

The user subsequently reported voice replies staying on thinking and canceled
them after about 30 seconds. The API, Qwen on port 8001, embedding sentinel, and
warmed GPU voice were healthy. An isolated `Hello` reproduced the problem without
microphone input: native `tool_choice=required` generated repeated prose for
62.61 seconds, exhausted 4,096 tokens, and returned no tool call. Optional native
tool choice returned a greeting promptly, but cannot enforce the explicit memory
decision needed by continuous chat. Stronger prompting alone was insufficient:
a full voice-mode chat probe still stalled on `Can you hear me?`.

Required foreground routing now constrains the complete response with a JSON
schema selecting exactly one of the existing operations and its arguments. The
generator validates the envelope and converts it into the existing tool-call
representation, keeping internal JSON out of chat output. The context guard
checks the same augmented prompt and schema sent to inference. Optional native
tools and prose synthesis after a task result retain their previous protocol.

An isolated full chat-path run against the real Qwen model and pinned in-process
embedder completed all seven requests in voice mode. Greetings and a hearing
check took 1.339 / 0.868 / 0.999 seconds; a new fact and its recall took 0.990 /
1.008 seconds; document-task acceptance and a status question took 4.259 / 3.924
seconds. Seven visible exchanges produced two memory episodes and one queued
task. Greetings and work updates were display-only; the fact was retained and
correctly recalled. This probe did not start its queued worker, capture a
microphone, or play audio, and used separate temporary storage. These measured
requests do not establish a latency guarantee for arbitrary conversations.

Validation after the repair:

```text
All checks passed!
977 passed, 3 skipped, 2 warnings in 71.95s (0:01:11)
```

Regression cases cover fragmented structured replies, invalid/truncated
operations, no internal JSON emitted as user prose, release of the model slot
after errors, unchanged optional/no-tool generation, and matching context-guard
and inference payloads. Existing task-memory and shadow checks also pass.
