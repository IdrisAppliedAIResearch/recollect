# Subagent behavior audit — 2026-09-10

This change makes research conversational by default, preserves explicit file
requests and revisions, and keeps supervision responsive while native work runs.
**The overall factual audit remains unresolved. No commit, push or PR was made.**
The experimental LLM reviewer stage was removed at the user's request. There is
no replacement review stage, extra reviewer inference, or reviewer approval gate.
The live model is nondeterministic. The observations below establish specific
behaviors in these samples, not a general improvement in research accuracy.

## Implementation

- Native OpenCode build/general prompts remain intact. The initial handoff routes
  to a small reporting skill; research and file workflows load only when needed.
  Files are created only on request. Only reported deliverables are copied to
  Downloads; workspace scratch is archived separately. Both relative paths and
  native `/workspace/` report paths identify the same validated artifact.
- Main announcements relay actual findings, selected sources, and limitations.
  The existing main-agent narration has a four-second deadline and falls back
  to the recorded report. Reporting favors spoken prose.
- Updates are driven by subagent reports, with no periodic activity announcements.
  Status and explicit messages to an owned worker use saved state directly.
  Messages have durable revisions; acknowledgment is separate from delivery.
  Steering pauses native parent/child execution and resumes the saved session.
- The speech queue protects findings from routine progress replacement. Incoming
  reports may coalesce while delivery is occupied; a silent worker creates no
  notifications. Historical heartbeats are excluded from substantive main context.
- Explicit research commands that receive only an acknowledgment get one recovery
  call. With no existing task, that call can only start work. Empty revision text
  falls back to the user's actual message. Task promises/request narration are
  excluded from memory while separate factual sentences remain eligible.
- Research skill guidance covers source identity, comparisons, units and dates,
  failed retrieval, and evidence limits. `web_fetch(view="page")` can recover
  factual cards omitted by article extraction without scripts/navigation.
- Repeated content, output-limit changes, errors and skill metadata do not count
  as new research. After six research calls add no evidence, native execution is
  paused once and asked to synthesize supported findings. Repeating that pattern
  again blocks with saved work. The checkpoint guard remains as a second bound.
- A validated parent result receipt completes the handoff even if native closing
  prose is empty. Child/stale receipts cannot finish the current revision, and
  native errors still prevent success.
- Report text/revision limits are now exposed in the MCP schema, with actionable
  errors and instructions to summarize a long file instead of repeatedly sending
  more than 4000 characters.
- A missing instruction acknowledgment gets one reminder without redoing saved
  work. Main status and notification-delivery challenges read recorded activity;
  a recorded notification does not prove audio playback.

No changes were made to episodic, shadow verification, embedding identity or
main/subagent token settings.

## Live setup and reproduction

The harness uses the real main router, coordinator, Qwen model, native OpenCode,
research tools and notifications. Session data and Downloads are isolated under
`.agent/behavior-audit-*`; the harness removes its external Docker sandbox on
exit. It never creates test conversations in the user's production store.

Test configuration: Qwen3.8-27B-UD-Q4_K_XL, saved 131072-token context, one shared
inference slot, main output 4096 and worker output 2048; pinned CPU embedder with
matching research sentinel. Docker image `recollect-opencode-sandbox:1.18.18`
contains the checksum-pinned ripgrep 15.1.0 needed by native skill loading on the
read-only/noexec sandbox. Full production launch also verified Whisper CUDA
float16 and Kokoro CUDAExecutionProvider. The audit feeds voice-mode text, not
microphone audio; live ASR/TTS playback is outside its assertions.

```powershell
uv run --no-sync python tests/live_subagent_audit.py --case replay
uv run --no-sync python tests/live_subagent_audit.py --case supervision
uv run --no-sync python tests/live_subagent_audit.py --case cases
uv run --no-sync python tests/live_subagent_audit.py --case files
uv run --no-sync python tests/live_subagent_audit.py --case evidence
uv run --no-sync python tests/live_subagent_audit.py --case routing
```

Inspect the printed directory's `results.json`, worker messages and actual files.
Failed automated behavior checks exit nonzero. Passing assertions still require
human review for factual claims and scope. Delete scratch after recording results.

## Observations and limitations

The original audits exposed unsolicited files, content-free main summaries,
research promises without dispatch, a lost Tuesday revision, operational memory,
unsupported research and a repeated Apollo fetch loop. These motivated the
implementation and regression cases above.

The first supervision runs exposed additional boundaries: valid final reports
with empty native closing prose, delayed steering while a parent waited for
children, and a recovery call selecting steering when there was no task. An
explicit file revision edited the saved file correctly but reported an absolute
workspace path that was not selected for download. These are now covered by
regressions and follow-up live runs.

| Follow-up sample | Observed behavior |
| --- | --- |
| Supplied-fact Markdown creation/revision | Monday file (62 bytes), then Tuesday file (63 bytes), Casey retained, only two Markdown versions delivered; ordinary note made no file. |
| Strict active supervision | Status 0.20 s, message persistence 0.49 s, revision 2 acknowledged about 4 s later; updates roughly 5 s apart; completed around 86 s; final comparison only Apollo 11/12; no files. |
| No-evidence supervision failure before inline recovery | Same denied download retried until the checkpoint guard blocked around 401 s. This was a failure, not a successful research result. |
| Original eight-turn TRIA replay and competitor follow-up | First request started research; no downloads. Work promise excluded from memory. Worker still falsely claimed no public LinkedIn page; main repeated it. Main also agreed with a false no-work challenge and stored a declined research request. The latter two have deterministic regression fixes; evidence validation remains unresolved. |
| Artemis summary/Markdown/revision sequence | Initial request got unnecessary confirmation instead of work; later file changed scope to Artemis versus Apollo. Markdown delivered, existing Sources section preserved. Oversized result-report retries delayed revision completion. Optional-confirmation routing and numeric tool-limit disclosure were corrected afterward; this full sequence still requires another replay. |
| Ordinary inline note and summary | No task or file created. |
| Apollo CSV | Exactly one 153-byte CSV; two correct ISO launch dates with NASA URLs. Main finding and final notification both stated the dates. |
| Final main-only routing | All seven checks passed: three research starts, explicit file task, Tuesday revision transfer, and two ordinary conversations without work. Workers are intentionally not dispatched in this case. |
| Final rebuilt-image file repeat | Tuesday and Casey verified in delivered Markdown by 37.86 s; two requested versions, no extra format; ordinary note created no file. |

The strict supervision answer verified launch dates/objectives and labeled exact
mission durations and UTC launch times unverified. Its missing facts were not
proof they were unavailable: NASA's [Apollo 11 mission overview](https://www.nasa.gov/history/apollo-11-mission-overview/)
contains precise duration and launch information. Retrieval coverage remains a
limitation. The result was substantive but table-heavy when narration timed out;
the reporting skill now favors concise prose.

Research/source verification is not made infallible by a passing timing test.
The TRIA absence claim is specifically unsupported by failed searches. The
[official Tria site](https://www.triafed.com/) is public; a guessed parked domain
does not identify the company. Guidance and the subsequently removed reviewer
both failed to prevent unsupported claims in live samples. For example,
NASA's Artemis I page calls SLS and Orion's flight their maiden flight, while
its [EFT-1 history](https://www.nasa.gov/mission/exploration-flight-test-1/)
documents Orion's 2014 test. The live comparison reproduced the mission page's
ambiguous wording. Source inconsistencies, incomplete retrieval, and semantic
review errors remain limitations; passing samples do not certify all research.
The intent/memory guards cover narrow English forms, not all paraphrases. Mixed
conversation outside these direct routes still uses the shared model. Progress
text is factual and may repeat when the worker has no new observation.

## Validation

### Removed reviewer experiment (historical results)

The following runs used an experimental reviewer that is no longer in the code.
Its prompt, generation call, verdict handling, correction loop, evidence snapshot
cache, review-only tests and live-review mode were removed. Ordinary result
receipts, acknowledgment recovery and repeated-content detection remain.

Six controlled live review samples passed: supported facts and user facts were
accepted, while a wrong date, unsupported company-profile absence and a malicious
source instruction were rejected. A correctly scoped retrieval limitation passed.

The original TRIA replay started work and created no files. Its provider searches
timed out. The main chat overstated a guessed domain as the company's domain;
the experiment added verbatim checked-report handoff at that boundary. A later continuation
was blocked by the evidence gate. Checked prior-report reuse was added afterward.

The full Artemis/Markdown/revision/ordinary-writing/CSV sequence passed all six
content and delivery assertions. The initial Markdown task lacked an instruction
acknowledgment and remained partial despite its saved file; the linked revision
completed. A bounded acknowledgment reminder now has a deterministic regression.

The final supervision sample passed all eight timing/control/file assertions:
status about 0.06 seconds, saved steering about 0.09 seconds, current revision
acknowledged, completion at 129.11 seconds, and no files. **Its factual audit
failed.** The reviewer accepted a final report containing Apollo 12's launch as
approximately 04:31 UTC, a purported official duration of 10 days 4 hours
31 minutes, and inconsistent arithmetic from calendar dates. It treated the
"not independently re-verified" caveat as sufficient to allow these claims.
NASA's [mission report](https://sma.nasa.gov/SignificantIncidents/assets/a12_missionreport.pdf)
states 16:22 GMT; its [mission history](https://www.nasa.gov/centers-and-facilities/kennedy/repeat-performance-apollo-12-achieves-second-moon-landing/)
gives 10 days 4 hours 36 minutes 25 seconds. The caveat did not make the numbers
supported or correct.

The yes/no reviewer failed this audit and was removed at the user's request.
Claim-by-claim review was discussed but was never implemented. No replacement
reviewer architecture is authorized. The research accuracy and restart-routing
issues remain unresolved; removing the stage does not establish a factual pass.

The last original replay completed at 306.48 seconds with no files: initial work
started, status and delivery questions used saved records, and cancellation was
not stored as a memory. It returned no verified research before cancellation.
The final request for competitor names offered fresh research instead of starting
it; this is another uncovered routing form. The supplied-fact file repeat finished
at 37.35 seconds, preserving Tuesday and Casey in the revised Markdown and
delivering only two requested versions. The six small verification samples all
passed again (8.92 seconds), despite the larger supervision counterexample above.
All audit processes finished and their external sandboxes were removed.

Historical automated checks before reviewer removal produced:

```text
All checks passed!
1109 passed, 4 skipped, 2 warnings in 90.48s (0:01:30)
3 passed in 13.16s
# tests 124
# pass 124
# fail 0
```

The UI production build passed. All three skills passed validation. The two
Python warnings are existing Starlette/httpx and Pydantic settings warnings.
The Docker image was rebuilt for the report schema change. Production was
refreshed before this evidence-check iteration, with its saved conversation preserved;
Qwen, matching CPU embedding identity and both GPU voice providers are ready.

Live evidence remains under ignored `.agent/behavior-audit-*` while the task is
unfinished. External audit sandboxes are removed by the harness. No test data
was added to production conversations or the user's Downloads directory.


### Reviewer removal validation

The LLM reviewer and all its runtime hooks, verdict retries, cached review
snapshots, and review-only tests are removed. No replacement reviewer was added.
The missing-acknowledgment regression remains with the ordinary worker flow.

```text
All checks passed!
1097 passed, 4 skipped, 2 warnings in 88.59s (0:01:28)
```

The UI production build also passed. No live-model accuracy improvement is
claimed from this removal, and the broader PR remains pending its audit blockers.

### Seven-second cadence

The user changed the active-work update interval from five to seven seconds.
The timer regression verifies two notifications seven seconds apart while worker
inference is unavailable. Earlier live timing observations above used five seconds.
The serving process has not been restarted to load the new interval.

```text
All checks passed!
1097 passed, 4 skipped, 2 warnings in 92.09s (0:01:32)
```

All 124 UI tests and the production build passed.

### Report-driven updates supersede cadence

The user rejected repeated timed activity lines after listening to the TRIA test.
The heartbeat worker and its lifecycle/timer state were removed. Subagent reports
now drive announcements; existing report coalescing does not create notifications
on its own. The reporting skill requests meaningful progress and findings without
waiting for a main-agent poll. The earlier seven-second timer results above are
historical and no longer describe intended behavior.
