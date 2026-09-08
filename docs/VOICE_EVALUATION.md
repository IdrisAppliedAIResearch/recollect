# Voice conversation evaluation

This report evaluates the local conversation path described in [VOICE.md](VOICE.md):
microphone audio, wake detection, transcription, verified memory retrieval, local
generation, Kokoro synthesis, and browser playback. It also checks what happens
when a person pauses, interrupts, corrects themselves, or encounters a failure.

The 2026-09-08 evaluation completed **80 full speech turns across six isolated
conversations**, plus 20 acoustic cases and 66 separate prompt-comparison
generations. All 80 full turns retained verified payload/report agreement,
committed completion, and completed synthesis. Content correctness did not hold
throughout: critical recognition errors, incorrect time corrections, failed
literal repetition, and unsuccessful research remain. A physical microphone
was unavailable, so human conversation comfort is not established.

## Evidence and limits

Keep these forms of evidence separate:

| Evidence | Establishes | Does not establish |
|---|---|---|
| Deterministic tests with controlled events and audio | State transitions, cancellation races, resource cleanup, persistence rules, exact formatting | Recognition quality or comfort with an actual microphone |
| Real local models with generated input audio | Recognition of those recordings, model replies, verified retrieval, speech generation, measured processing times | Human pronunciation coverage, room noise tolerance, speaker echo cancellation, perceived naturalness |
| Browser interaction | Visible states, focus, layout, controls, transcript presentation, playback integration | A microphone journey if the input device was simulated or unavailable |
| Human conversation through the intended microphone and output device | That person's observed turn timing, intelligibility, interruptions, and room behavior | General quality or an improvement across other speakers and environments |

A successful generated-audio run is not a claim that the human conversation is
comfortable. Record qualitative observations with their scenario and evidence;
do not claim a quality improvement from one run. A missing test is **not tested**,
not a pass. A setup failure is **blocked**, not a conversation result.

## Reproducible procedure

1. Record the revision and uncommitted changes, evaluation date, model identities,
   actual Kokoro execution provider, generator settings, voice configuration,
   hardware, and whether the models and caches were already warm.
2. Use isolated sessions and an isolated application instance against the real
   local services. Preserve existing user sessions and service processes. Do not
   change the memory mechanism or relax shadow verification to complete a run.
3. Run the deterministic suite before attributing failures to model behavior.
   Use the opt-in live evaluator for expensive model scenarios; ordinary tests
   must remain runnable without model downloads or a running generator.
4. Exercise both continuous conversations and fresh sessions. Include a sequence
   longer than the configured recent-memory window, then ask about early facts,
   corrections, and unrelated topics. Save the actual retrieved episode IDs.
5. Record the input, final transcript, reply, trace verification, commit outcome,
   timings, speech output length, and errors for each case. A failed case should
   retain enough evidence to identify the stage that failed.
6. Review the browser states separately, then perform the microphone cases with
   the intended device when available. Record browser, microphone, output device,
   whether headphones were used, and relevant background sounds.
7. Rerun demonstrated defects after a focused fix. Retain the original result
   beside the rerun; do not replace a failure with the successful retry.
8. Remove temporary audio, evaluation sessions, logs, and containers after the
   reusable report is saved. List any intentionally retained artifacts.

### Running the evaluation

These are opt-in commands against the installed local models and generator.
Ordinary pytest does not collect the live runner. Run from the repository root
and choose a new report filename to preserve existing evidence.

```bash
uv run --no-sync python -m tests.voice_live_evaluation --report docs/voice-evaluation-YYYY-MM-DD.json --acoustics
```

The current default includes **79 conversation turns across five groups**
(`memory`, `precision`, `clarification`, `research`, `repair`). `--acoustics`
adds 20 direct-listener cases; it does not add 20 chat turns. To reproduce the
original 67-turn baseline selection or run only the repair prompts:

```bash
uv run --no-sync python -m tests.voice_live_evaluation --group memory --group precision --group clarification --group research --report docs/voice-baseline-YYYY-MM-DD.json --acoustics
uv run --no-sync python -m tests.voice_live_evaluation --group repair --report docs/voice-repair-YYYY-MM-DD.json
```

The separate three-repetition prompt comparison uses a completed baseline report:

```bash
uv run --no-sync python -m tests.voice_prompt_comparison --baseline docs/voice-baseline-YYYY-MM-DD.json --report docs/voice-prompt-comparison-YYYY-MM-DD.json
```

## Measurements

Use monotonic elapsed times. State where each measurement begins and ends.
Report sample count, failures, median, upper percentile, and range where enough
comparable samples exist. Keep cold initialization, warm ordinary replies, and
delegated research separate.

| Measurement | Boundary and interpretation |
|---|---|
| Transcription delay | Last input speech sample to the final transcript event; includes configured silence detection. Accelerated PCM delivery must be identified and cannot represent conversational wall time. |
| Generation delay | Chat submission to first token and to terminal completion; include verified retrieval and reported prompt/cache timings where available. |
| First speech chunk | Speech request to first complete WAV chunk received; this is synthesis/transport readiness, not audible playback. |
| Response wait | Last input speech sample to actual browser playback start, only when both are measured on a comparable clock. Do not substitute a server-only timing. |
| Output length | Assistant words/characters, spoken-text characters, chunk count, and total audio duration. Describe verbosity using the actual prompt and reply. |
| Interruption delay | Sustained input speech onset to playback stopping; separate speech detection delay from client cancellation delay. Generated event tests establish the latter only. |
| Memory/persistence | Exact verified payload result, delivered episode IDs, commit flag, and stored-turn count. Replay and spoken controls must not create turns. |

There is no declared latency or naturalness target yet. Publish observations
instead of labelling an unmeasured response “instant,” “smooth,” or “better.”

## Scenario checklist

Each result needs a scenario ID, evidence type, status, and supporting output.
The table defines coverage; it does not claim these cases have been executed.

| ID | Scenario | Acceptance evidence |
|---|---|---|
| V01 | Ordinary speech before the wake phrase; wake phrase alone; wake plus request in one breath | No pre-wake chat; trailing request words retained; correct waiting/listening state. |
| V02 | Five or more follow-ups without another wake phrase | One microphone session; every completed request submitted once; no stale reply playback. |
| V03 | Short answer, clarification, pronouns, and a topic change followed by a return | Actual transcripts and answers retained for review; no assumed understanding based only on HTTP success. |
| V04 | Name, preference, correction, negation, and explicit “do not remember this” discussion | Record how the existing memory mechanism behaves; distinguish recognized input from model compliance. No new deletion guarantee is implied. |
| V05 | Conversation extending beyond recent memory, then early-fact recall | Exact verification and retrieved episode IDs; record answer and relevant stored corrections. |
| V06 | Amounts, currencies, decimals, ranges, dates, units, acronyms, and proper names | Compare intended input to ASR text; inspect speech text for changed meaning; original answer remains inspectable. |
| V07 | Concise default reply, then an explicit request for detail | Record actual word count and audio duration; detail is allowed without an arbitrary speech cutoff. |
| V08 | Research success and a research failure with a final explanation | Sources/tool steps and failure state remain truthful; only a successful completed final explanation is eligible for speech. |
| V09 | Hesitation, a short thinking pause, filler words, and a long uninterrupted request | Record actual boundaries; a hard recording limit pauses for explicit review and never sends a partial request automatically. |
| V10 | Interrupt during generation, synthesis, decoding, active playback, and a gap between chunks | Old work is cancelled; late results cannot speak; the next final transcript is queued once after the previous chat settles. |
| V11 | Mute before wake, while dictating, during generation, and during playback; then unmute | Input stops immediately; active replies can continue while muted; acknowledgement restores the previous waiting/listening state. |
| V12 | Stop reply, replay a completed reply, replay while muted, and replay while beginning a new request | No additional inference for replay; no new stored turn; treatment of the unfinished request is visible and intentional. |
| V13 | Exact spoken commands and ordinary sentences mentioning the same phrases | Commands are handled locally; phrases discussed inside a longer sentence remain ordinary requests. |
| V14 | TTS failure, generation failure, stream truncation, socket loss, microphone loss, and browser suspension | Healthy listeners recover from reply failures; broken input/transport stops safely; errors explain recovery and unsent-text behavior is recorded. |
| V15 | Captured-text review: send, discard, attempt correction, connection loss, and Stop voice | No automatic submission; each explicit action has an unambiguous result; check whether unsent text can be recovered. |
| V16 | Keyboard-only controls, narrow window, a long transcript, and reading older replies during streaming | Controls remain reachable; focus is visible; text is readable; unexpected scroll or lost draft context is reported. |
| V17 | Real microphone with headphones, then intended speakers/background noise | Record false wakes, missed starts, unwanted interruptions, intelligibility, and user observations; generated audio is insufficient evidence. |
| V18 | Stop voice, switch session, or leave the page during work | Microphone and playback stop; no old reply enters another session; stored completed turns remain consistent. |

## Product review candidates

These began as source-review observations on 2026-09-08. Two were reproduced
with controlled client events and fixed. Their presentation was **not tested in
an actual browser microphone journey**. The remaining candidates are checks,
not claims that all need a new feature.

| Candidate | Current code evidence | Check or decision needed |
|---|---|---|
| Unsent words disappeared on a fatal voice failure | Before the fix, `VoiceClient.stop` reset the partial and long-review text. Regression cases reproduced this for microphone/socket loss and a follow-up waiting to enter chat. | Fixed: unexpected failure transfers unsent words once into an editable draft in the originating session, preserving existing typed text. Already-submitted requests are excluded and nothing is sent automatically. V14/V15 browser handoff was not tested. |
| Replay consumed an unfinished request | Before the fix, Replay was available during dictation and suppressed that utterance's final transcript. Tests reproduced speech-onset, partial, revised-empty, and endpoint boundaries. | Fixed: Replay is unavailable while unsent speech is being captured; the recognized request survives. V12 actual browser interaction was not tested. |
| Recording-limit review gave conflicting guidance | Unmute is disabled while review is pending, but the composer hint previously said “Unmute mic to speak again.” The transcript is read-only. | Fixed the hint to name “Send captured text” and “Discard and resume.” V15 browser review and any need for editing during review remain untested. |
| Reading earlier replies can be interrupted by scrolling | `Chat` scrolls to the bottom on every `exchanges` update, including streamed content. | V16: inspect an older answer during a long response and observe whether the view stays where the reader put it. |
| A silent synthesis wait appears as “Thinking” | The client uses the same thinking phase before chat completion and before the first decoded speech chunk. | V07/V16: measure the gap after text finishes and assess whether the visible state explains it. |

Implementation references: [voice client](../ui/src/voice/client.ts),
[chat controls](../ui/src/components/Chat.tsx),
[voice lifecycle](../ui/src/voice/useVoice.ts), and
[UI regression tests](../ui/tests/voice-client.test.mjs).

After these fixes, the combined UI check reported **103 tests passed, 0 failed**,
and the TypeScript/Vite production build passed. New
[draft-handoff regressions](../ui/tests/voice-draft.test.mjs) cover one-time
consumption, preservation of typed text, and session boundaries. These results
do not replace the full repository verification or a browser microphone test.

## Recorded evaluation

### Baseline: 2026-09-08

The [saved baseline report](voice-evaluation-2026-09-08.json) completed from
17:04:13 to 17:14:32 UTC using the opt-in
[live evaluator](../tests/voice_live_evaluation.py). It contains **67 completed
speech round trips across four isolated conversations**, plus 20 acoustic cases.
This is a pipeline completion count, not 67 correct answers.

The generator was `Qwen3.8-27B-UD-Q4_K_XL.gguf` at temperature 0.7. Kokoro reported
`CUDAExecutionProvider`; the real in-process embedder's sentinel matched the
research identity. Wake was “hey idris,” endpoint silence was 1.4 seconds, and
RECENT was 32 episodes. This baseline predates the subsequent voice-style
experiment and weekday-context fix. Models stayed loaded across turns; input
audio was synthesized before each request, so these are not cold-start latency
measurements. The report does not identify the physical GPU model.

| Conversation | Recorded turns | Stored turns at completion |
|---|---:|---:|
| Memory, corrections, and varied everyday topics | 46 | 46 |
| Numbers, units, exact repetition, and requested detail | 12 | 12 |
| Clarification, references, and session boundaries | 6 | 6 |
| Current and historical research requests | 3 | 3 |

All **67** turns reported a committed completion, identical authority/shadow
payload and report fields, and a completed speech stream. The four stored-turn
counts matched their recorded conversation lengths. Recognition matched the
normalized reference words exactly in **50 of 67** cases. Normalization ignores
case and punctuation; this count does not measure semantic correctness.

#### Timing and reply length

“Ordinary” below means the 63 non-research turns other than the explicitly
requested six-step detailed answer. Percentiles use nearest rank. Every timing
is the boundary named in the table, not a claim about perceived waiting time.

| Ordinary replies, n=63 | Median | 95th percentile | Maximum |
|---|---:|---:|---:|
| Reply words | 46 | 130 | 170 |
| Generated output audio | 14.379 s | 45.397 s | 57.045 s |
| Chat submission to completed reply | 1.887 s | 4.255 s | 5.041 s |
| Speech request to first complete WAV chunk | 0.200 s | 0.238 s | 0.268 s |

The 170-word paper-book/audiobook comparison produced 57.045 seconds of speech;
the desk-organization answer produced 164 words and 49.088 seconds. Neither
request asked for a long explanation. The separate six-step dinner-party request
explicitly asked for examples and a pitfall: its 326 words and 101.845 seconds of
audio should not be treated as an unwanted-verbosity failure merely for being long.

For those 63 ordinary turns, median reported prompt prefill was 333.796 ms.
The median per-turn cached-token share was 52.8%, with a range of 5.5%–86.6%.
Those cache observations establish reuse in this run, not a measured improvement
over an uncached control. The two delegated research turns took 320.625 and
39.249 seconds end to end and are excluded from the ordinary timing table.

Only `clarification/two_values` used real-time PCM pacing: its final transcript
arrived 1.398 seconds after the generated input ended. All other conversation
input was accelerated. The run did not measure physical speaker onset,
microphone-to-speaker response wait, or perceived interruptions.

#### Content and memory findings

| Recorded case | Observation | Assessment |
|---|---|---|
| `memory_beyond_recent` | With 42 stored episodes, the answer correctly recalled the corrected $1,600 rent ceiling and September 18 appointment at 4:15 PM Central. The retrieved block includes early turns 1–4. | Beyond-RECENT retrieval and this answer succeeded. |
| `preference_beyond_recent` | With 43 stored episodes, the answer recalled vegetarian meals and no mushrooms; turn 4 appears in the retrieved block. | This preference recall succeeded. |
| `update_again` / `confirm_update` | Intended “four thirty PM” became ASR “for thirty pm”; the model committed 3:00 PM and repeated 3:00 in the following answer. | Correction failed. The wrong time appears before speech formatting; successful retrieval does not prevent generation from changing a value. |
| `cache` / `analogy` | “What is a cache, in plain language?” became “what does the cash in plain language.” The reply asked about cash/rent; the next analogy request had no useful referent. | A recognition error disrupted two conversational turns. |
| `precision/cents` | Exact ASR preserved “fifty cents per request, not fifty dollars.” The requested exact repetition omitted “not fifty dollars” and added a preface. | Amount preserved, literal-repeat instruction failed before synthesis. |
| `clarification/ambiguous` | After a $40 budget and six guests, “change it to twenty” was assigned to the budget without asking. The next user message happened to confirm that interpretation. | Observed guess, not evidence of reliable ambiguity handling. |
| `new_session_boundary` | The separate clarification session said no rent ceiling had been mentioned there and recalled only its own dinner budget/guest count. | This session-boundary answer succeeded. |

The two beyond-RECENT cases delivered all 42 and 43 stored episodes: 32 recent
plus 10 and 11 semantic episodes, with zero dropped. The long-term blocks used
2,999 and 3,853 characters out of 32,000. They demonstrate the path outside RECENT,
but do not establish recall under tight retrieval-budget pressure. Full traces
and episode IDs are retained under `retrieval_evidence` in the JSON report.

#### Research outcomes

| Recorded case | Actual execution | Assessment |
|---|---|---|
| `current_lookup` | 34 tool steps; partial result with “malformed or missing final JSON.” The answer gave no library hours or arrival time after 320.625 seconds. The task added an incorrect weekday and guessed a domain whose fetched page identified Atikokan Public Library. Search observations included unrelated academic results. | Requested research answer failed. The completed, verified fallback and speech stream do not turn this into a successful lookup. |
| `research_followup` | Seven tool steps and a structurally valid subagent result, but no closing time or applicable day after 39.249 seconds. The reply supplied an unsupported contact number. | Requested information still absent; valid result structure is not factual success. |
| `historical_lookup` | The reply gave the correct 2020 Austin population, 961,855, and named the Census Bureau, but this turn had no subagent or tool lookup. | Correct fact against the independent [Census table](https://data.census.gov/table/DECENNIALPL2020.P1?q=Austin+city%2C+Texas+Populations+and+People); the requested lookup/source verification was not performed. |

The research follow-up said `512-831-3151`, which was absent from its observed
tool results. The independent [official library locations page](https://library.austintexas.gov/locations)
lists Central Library's phone as **512-974-7400**. The primary-source reference
checks below remain separate from the platform's failed discovery path.

#### Acoustic cases

The direct-listener acoustic suite recorded **16 of 20 cases passing its stated
expectations**. The four failures were:

- `wake_same_breath_us_male`: the generated `am_adam` voice did not activate the
  intended wake phrase or produce the request.
- `confusable_address`: “Hey, address this package to my office” falsely activated
  the listener and produced “this package to my office.” An active client would
  treat that final transcript as a request; the acoustic helper itself did not
  submit it to chat.
- `multiplication_and_power`: the intended word “six” became “sex,” failing the
  critical-term check.
- `name_time_abbreviations`: “Dr. Rao's ETA is 4:15 PM Central” became “dr rouse
  eater is for fifteen pm central,” failing the name/time term checks.

Short “yes” and “no,” a 0.8-second pause within a sentence, two turns without
another wake phrase, lower-gain speech during simulated playback state, and
synthetic hiss/click rejection met their expectations. Across 15 final transcripts,
the audio-clock endpoint interval was 1.265–1.435 seconds, median 1.403 seconds.
Those intervals use generated segment ends, not phoneme annotations or real-time
browser transport. These results do not justify lowering interruption thresholds
or claim coverage of human accents, real room noise, or echo cancellation.

#### Browser evidence, remaining work, and cleanup

Chrome rendered the actual isolated conversation at 28 persisted turns; its
transcripts and replies were visible. This establishes rendering of the live
session only. A later reload of the production UI with asset
`index-oZS-Xpvx.js` followed by **Enable voice** displayed: “No microphone found.
Connect a microphone or headset, then enable voice.” Voice remained off. That
checks the visible missing-device recovery path; physical microphone input,
speaker playback, perceptual comfort, keyboard/narrow-layout journeys, and the
draft-recovery DOM handoff remain untested. The temporary Chrome evaluation tab
was closed.

The two UI fixes above passed deterministic regressions and the production
build. The baseline JSON reports `temporary_data_removed: true`; it retains
traces and measured results, not the temporary conversation database or generated
audio. Final repository verification is recorded below. No overall conversation-
quality pass or human microphone acceptance is claimed from this baseline.

### Additional repair conversation

The [repair report](voice-repair-evaluation-2026-09-08.json) completed from
17:21:47 to 17:22:17 UTC with the retained baseline voice instructions and CUDA
Kokoro. It contains **12 additional complete speech turns in one new isolated
conversation**, bringing the subtotal before the research recheck to **79 full
speech turns across five conversations**. All 12 repair turns were committed,
had identical verified payload/report fields, and completed synthesis; the store contained 12 turns at
completion. Nine of the 12 normalized transcripts matched exactly.

These prompts test conversational attempts to repair earlier failure patterns.
They do not establish that the earlier model or recognition defects were fixed.

| Repair sequence | Actual result | Assessment |
|---|---|---|
| Computer-cache clarification, then another analogy | “I mean a cache in a computer” became “i mean occassion a computer.” The model asked about “occasion”; the next analogy request again lacked a useful referent. | Explicit domain context did not repair this recognition failure. |
| Explicit 4:30 appointment and recall | “Four hours and thirty minutes PM” was recognized correctly, stored as 4:30 PM, and recalled as 4:30 PM. | This explicit phrasing succeeded in one sequence. It does not fix the prior “for thirty” interpretation. |
| Correction to 5:45, then recall | The transcript changed “at” to “that” but retained “five forty five pm.” Both the update and subsequent recall said 5:45 PM. | This correction sequence preserved the intended value. |
| Fifty cents, not fifty dollars | The initial statement retained both amounts and the negation in the reply. | This contrast was preserved. |
| Emphatic request to repeat all words, including the contrast | The model gave a 98-word recap of the entire conversation, with 33.152 seconds of speech, instead of repeating the requested phrase. | Stronger wording did not repair literal-repeat compliance. |
| Books/movies question followed by one-word “Books” | “Ask me” was recognized as “asked me,” leading to a needless memory disclaimer, but the model still offered the choice. “Books” was then recognized and treated as the chosen topic. | Short reply and referent worked; the earlier tense error affected the framing. |
| Change topic to sky color, then request ten words | The topic changed. The final answer was “The sky is blue because sunlight scatters blue light most.” | The explicit word limit was met: exactly ten words. |

The repair report also records `temporary_data_removed: true`. The 20 acoustic
cases and 66 prompt-comparison generations remain separate evidence sets; they
must not be added to the full speech-turn count.

### Final research recheck

The [research recheck](voice-research-recheck-2026-09-08.json) completed from
17:26:53 to 17:30:16 UTC in a sixth isolated conversation, adding one turn for a
final total of **80 full speech turns**. The request was transcribed exactly.
After **197.397 seconds and 36 tool steps**, research again ended partial with
“malformed or missing final JSON.” It provided neither the requested hours nor
an arrival time. The verified fallback was stored and synthesized into 4.181
seconds of audio. The transport and speech completed; the research task did not.

The recheck exposed the general-web search failure explicitly: a search result
reported a DuckDuckGo verification challenge, while its returned results were
academic sources. A separate direct host query earlier returned three results,
including the official site, in 1.099 seconds. That one successful discovery
query does not establish a working end-to-end lookup or explain every earlier
failure. The search parsing/challenge fixes are not a claim that this research
answer was repaired.

Read-only inspection also found an extraction-coverage gap. The official
`/locations` fetch returned HTTP 200 and 3,363 extracted characters, marked
untruncated, with holiday closures and printing information but no regular
Central Library hours. The official/archived Central Library fetches returned
2,787 untruncated characters about parking, transit, tours, and printing, again
without the hours. Increasing the requested character allowance did not supply
them. The web tool uses article-focused extraction, and a nonempty result
prevents its plain-text fallback. Raw HTML was not retained, so the exact cause
— DOM filtering, dynamic content, or differences in delivery — is unproven.

Full pytest/Docker verification and later production warm-up overlapped this
recheck. Its duration must not be compared with the earlier 320.625-second
failure as evidence of a performance improvement. The recheck records
`temporary_data_removed: true`.

### Paired voice-style experiment

The [saved prompt comparison](voice-prompt-comparison-2026-09-08.json) ran from
17:16:07 to 17:18:14 UTC: **11 cases × three interleaved repetitions × two
instruction variants = 66 generations**. Each pair used the same recognized
input and captured, verified context. The
[comparison runner](../tests/voice_prompt_comparison.py) kept model sampling and
the token budget unchanged. It did not rerun ASR, retrieval, or TTS, so these are
prompt-generation observations rather than new end-to-end voice trials.

The candidate added an ordinary-reply target of 20–45 words, discouraged routine
offers of further help, and explicitly requested preservation of negation and
clarification of ambiguous changes. The baseline voice instructions already
requested one to three short sentences with detail when asked.

| Observed measure | Baseline instructions | Candidate instructions |
|---|---:|---:|
| Ordinary samples, excluding the detail case | 30 | 30 |
| Median ordinary reply words | 65 | 50.5 |
| Maximum ordinary reply words | 214 | 119 |
| Exact cents/dollars negation preserved | 2 of 3 | 0 of 3 |
| Beyond-RECENT rent/appointment recall correct | 3 of 3 | 3 of 3 |
| Appointment correction stated as erroneous 3:00 PM | 3 of 3 | 3 of 3 |
| Ambiguous “twenty” assigned to budget without clarification | 3 of 3 | 3 of 3 |
| Requested six steps, example, and pitfall present | 3 of 3 | 3 of 3 |

All 66 generations completed without a reported generation error. That does not
resolve the content failures: every candidate cents reply was only “fifty cents
per request,” omitting “not fifty dollars.” One candidate appointment answer
asked about 3:00 versus 3:30 only after first stating the erroneous update; it
never offered the intended 4:30. The detail answers retained their requested
structure, with baseline lengths of 265, 271, and 196 words and candidate lengths
of 250, 230, and 138 words.

**The candidate was not adopted; the baseline voice instructions remain.**
Ordinary word counts were lower in these samples, but literal meaning and
correction handling did not meet the requirements. The result does not support
a quality-improvement claim, a change in sampling/token limits, or an inferred
audio-latency reduction without synthesizing and measuring those replies.

### Independent research expectations

These checks were performed separately by the evaluator on 2026-09-08. They are
reference facts for assessing the recorded Recollect answers, not evidence that
Recollect found or used these sources.

- The [official Austin library locations page](https://library.austintexas.gov/locations)
  lists Central Library's regular Tuesday hours as 9 AM–8 PM. September 8, 2026
  is a Tuesday. Ninety minutes before that regular closing time is 6:30 PM.
  A date-specific closure or exception was **not confirmed**, so the regular
  schedule alone does not establish the exact hours for that date.
- The [Census 2020 population table for Austin city, Texas](https://data.census.gov/table/DECENNIALPL2020.P1?q=Austin+city%2C+Texas+Populations+and+People)
  gives a population of **961,855**. Keep that historical reference year intact
  when evaluating a request about the 2020 Census.

## Prioritized next steps

1. **Preserve critical input and corrections.** Evaluate wake and transcription
   alternatives against held-out positive/confusable phrases, names, times,
   amounts, and negation. Require value-preservation checks and confirmation of
   unclear changes before treating a correction as accepted. The missed male
   wake, false “address” activation, cache failures, and 4:30-to-3:00 change are
   concrete acceptance cases. Do not reduce speech thresholds based on these
   recordings alone.
2. **Make research completion depend on usable evidence.** Check discovery failure,
   page-content coverage, and unsupported fallback details independently. A valid
   JSON result, a source count, or a completed spoken apology must not imply the
   requested lookup succeeded. Reproduce the missing-hours extraction with raw
   page evidence before choosing a fix, then rerun an actual sourced answer.
3. **Shorten ordinary answers without losing requested meaning.** Keep exact-repeat,
   contrast, ambiguity, and requested-detail cases as acceptance checks. The
   rejected prompt candidate demonstrates why smaller word counts alone are
   insufficient. Do not replace this work with speech truncation.
4. **Complete real microphone acceptance.** Connect the intended device and test
   wake activation, pauses, quiet speech, real room noise, output echo, and
   interruptions during generation and playback. Repeat with headphones and the
   intended speakers. The current missing-device path works, but physical voice
   flow and perceived delay have not been measured.
5. **Check the remaining UI journeys.** Exercise keyboard controls, narrow layouts,
   long-request review, recovered-draft editing, and reading old replies while
   another answer streams. Confirm the corrected review hint and assess forced
   scrolling and the unexplained synthesis wait against those observed journeys.

## Final verification and runtime state

The final verification run reported these output excerpts:

```text
Ruff
All checks passed!

Backend pytest
486 passed, 2 skipped, 1 warning in 43.29s

Live Docker checks
2 passed in 10.92s

UI tests
# tests 103
# pass 103
# fail 0
# duration_ms 196.0175

Production UI build
41 modules transformed.
✓ built in 96ms
```

The final build produced `index-5MGI23hw.js`, and a subsequent HTTP check confirmed
the running server serves that asset. Recollect was restarted as
process 2896, CUDA speech was warmed, and health plus embedder-sentinel checks
returned true. The existing generator process 20008 was preserved. The baseline,
repair, and research-recheck reports each record temporary-data cleanup; the
saved JSON reports, reusable runners, and this assessment are intentional
artifacts. The temporary Chrome evaluation tab was closed, and the final Docker
check found no running Recollect subagent containers.

The evaluation is complete with the defects and untested physical-microphone
conditions above explicitly retained. Passing repository tests and completed
speech streams do not certify the correctness of every generated answer.
