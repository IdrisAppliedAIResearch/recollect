# Continuous subagent collaboration — implementation plan

Status: reviewed draft for agreement, 2026-09-09. Independent no-code forward review completed; corrections and remaining decision gates are recorded below. Planning only; no implementation or runtime changes.
Repository baseline: `ce70db5`. Covers issues [#7](https://github.com/IdrisAppliedAIResearch/recollect/issues/7), [#8](https://github.com/IdrisAppliedAIResearch/recollect/issues/8), [#9](https://github.com/IdrisAppliedAIResearch/recollect/issues/9), [#10](https://github.com/IdrisAppliedAIResearch/recollect/issues/10), and [#11](https://github.com/IdrisAppliedAIResearch/recollect/issues/11). #13 was incorporated into #10; voice naturalness (#12) is separate.

## 1. Outcome and constraints

The user can delegate substantial work, keep talking, ask about findings, steer the same task, receive usable files, and revise them later in the same conversation. The main agent remains the conversational interface. Tool execution and internal agent messages never become main-chat memory episodes.

Agreed direction:

- Main and subagent exchange explicit messages through durable, task-scoped mailboxes. Delivery does not automatically create a conversational turn.
- No arbitrary overall task deadline. Distinguish productive long work, blocked work, explicit cancellation, and execution failure; retain useful partial work.
- Main chat gives truthful prompted and restrained proactive updates. Speech interruption stops playback, not the task.
- Task context, findings, and artifacts survive completed invocations and environment recreation. Other conversations cannot inherit them.
- One shared Qwen model. Measure scheduling, batching, VRAM, and possible reuse of two prompt contexts with GPU voice loaded before selecting the runtime configuration.
- Preserve `episodic`, strict shadow verification, research constants, CPU in-process embedding, and the pinned environment. Task context is additional generation input, not a change to retrieval or its character budget.

Explicit exclusions: a general multi-agent framework, external message broker, another chat model, HTTP embeddings, arbitrary host file access, unrestricted shell tools, changes to voice model quality, or an OpenCode upgrade as a shortcut.

## 2. What the code actually does today

| Area | Evidence and consequence |
| --- | --- |
| Conversation ownership | `api.py::_stream_turn` holds the per-session lock across retrieval, first generation, the full research run, synthesis, and commit. A background worker alone will not fix inference contention. |
| Model ownership | `AppState.model_slot` is shared by `Generator.stream` and `SandboxManager`. `begin_invocation` acquires it for the entire OpenCode run, including child work and tool waits. |
| Cancellation | `voice/client.ts` interrupts the reply's AbortController on speech onset. The HTTP response owns the turn iterator; closing it reaches runner cleanup and the OpenCode abort endpoint. This is a concrete cancellation path consistent with #7; the original incident still needs reproduction. |
| Sandbox lifecycle | `SandboxManager` reuses one container but creates a new OpenCode session and scrubs `/workspace` before and after each invocation. `finish_invocation` deletes the OpenCode session. Keeping files without changing ownership would break isolation. |
| Timeout | `runner.py::_REQUEST_TIMEOUT` has no read, write, or pool deadline. `sandbox_steps` defaults to 24. Generated provider configuration has `timeout=False`, a 600,000 ms chunk timeout, and a 120,000 ms MCP timeout. Main generation and web tools have other bounds. The reported timeout layer is unknown; do not invent an overall timer to remove. |
| Context capacity | `configgen.py` advertises 200,000 context tokens and 32,768 output tokens, while the saved llama-server launch uses a 32,768-token context. This mismatch must be resolved from measured effective capacity, including output and tool overhead, before continuous work is enabled. |
| Existing event adapter | `OpenCodeRunner` consumes `/event`, filters parent and child sessions, deduplicates completed tool calls, and emits `SubagentStep`/`SubagentResult`. Its bounded queue currently couples backpressure to the consumer; its error JSON omits saved observations even though steps/sources exist in memory. |
| Main memory | `SessionManager.commit_turn` appends one user/assistant pair. Delegation preambles and raw tool histories are already excluded. `chat_history` reconstructs visible turns from saved traces, including failures. There is no general assistant-only notification history. |
| Voice and client | Speech resolves a verified completed turn by ID. There is no notification speech source. UI research workspace is transient and attached to the requesting turn; `sending.current` gates sends. Desktop client already relays `/api/*` HTTP streams. |
| Existing reuse | `SubagentStep`, `SubagentResult`, `SubagentTrace`, date context, safe final-answer repair/fallback, `PromptCacheTrace`, generator token/cache metrics, SSE helpers, voice synthesis/playback, path validation, container attestation, and fake-model lifecycle tests. |
| Compatibility | Legacy backend remains the configuration default, although the saved production setup uses OpenCode. `/v1` streaming and non-streaming paths currently generate ordinary replies without delegation. Do not assume either already supports the new workflow. |

## 3. Minimal proposed design

### Task ownership and storage

Add a small task coordinator owned by `AppState`, with explicit startup/recovery/shutdown. A delegated task outlives its initiating HTTP response. Retain the existing lock around a normal conversational turn and its durable write; task execution never holds that lock for its lifetime. Do not solve ordering by removing all locks.

Use task records and an ordered mailbox separate from `EpisodeStore`. Proposed persistence is one small SQLite task database per Recollect conversation, with transactions for message IDs, revisions, checkpoints, and delivery cursors. Store immutable artifact versions as regular files alongside it. This is new application state, not a modification of episodic storage. Confirm this choice against the repository's preference for inspectable files; JSON export can be added only if a concrete inspection need warrants it.

Initially propose one active delegated task globally, matching the existing singleton sandbox, with a bounded FIFO queue of separate tasks. Queued research has no guaranteed start time while another task is active; inference fairness for main chat does not make the sandbox available to another research task. Proposed first-release behavior: blocked-for-input work retains ownership until answered or canceled, and queued tasks visibly explain that wait. Checkpoint-and-release, user reprioritization, and task time-slicing require a separate scope decision and proven restoration. Main conversation can run between or alongside the active task's model requests according to the selected scheduling policy. OpenCode's existing internal build/general workflow remains intact. No generalized agent hierarchy is needed.

Each record needs conversation ID, durable task ID, objective/revision, backend session association, parent/follow-up relationship, state, latest bounded progress summary, checkpoints/findings, source references, artifacts, failure details, and timestamps. Keep execution states separate from result completeness: queued/running/blocked/cancel-requested/completed/canceled/interrupted, with a partial-result marker where applicable. Restarted work is not labeled running until reconciled with its owner.

### Message contract

| Direction | Operations |
| --- | --- |
| Main to task | start, steer, request status, continue, cancel |
| Task to main | accepted, progress, finding, question, blocked, result, canceled |

Messages have an ID, task/conversation ownership, sender, sequence, operation, instruction revision, related message ID, timestamp, and bounded payload/reference fields. Persist before acknowledging receipt. Treat transport receipt, accepted direction, and completed work as different events. Use unique IDs and transactional deduplication for retries; do not promise exactly-once remote execution when delivery is uncertain.

Give the originating conversational request a stable client request ID as well. Persist the original user instruction, request-to-task association, and command receipt before remote dispatch, even if the foreground reply is interrupted. Task storage and episodic commit are not one transaction: reconnect must show an accepted task whose conversational acknowledgment never completed without fabricating a completed episode. Reuse command deduplication for retry; reconcile uncertain submission before resending. Test crashes and disconnects on both sides of persistence, remote submission, acknowledgment, and foreground commit.

The carrier for these semantic messages is a P0 feasibility gate. Current `_apply_event` ignores non-tool parts, native `question` is denied, and the MCP server has only search/fetch; none proves finding reports or revision acceptance. Probe native text/message events and busy-session delivery first. For structured outgoing reports, evaluate one small reporting tool in the existing MCP server, adapted through existing tool-call events and IDs. The coordinator binds reports to its known task/session rather than trusting model-supplied ownership. Local tool completion is not durable host receipt: demonstrate recovery of missed reports through backend message history. For incoming steering, prefer demonstrated native delivery; if unavailable, explicitly design a small task-control bridge. Do not treat HTTP success as incorporation, use writable workspace files as a trusted control channel, or build a second agent loop. Keep native questions disabled unless the response path is implemented; a task question can be answered by `steer` referencing its message ID.

Update progress snapshots in place; retain a bounded diagnostic/message history outside episodic memory. Preserve critical steering, cancellation, blocker, and result messages. Coalesce routine progress; a disconnected or slow UI cannot block task execution or lose a final result. Reconnection uses a cursor plus the current snapshot; stale cursors recover from a snapshot. Unacknowledged delivery after a crash is reconciled against backend state before resending.

A steering revision supersedes only conflicting earlier instructions. A result records the revision it actually used. If steering arrives after completion, create an explicit follow-up on the same saved work. Clarify ambiguous task/artifact references. Cancellation is a coordinator operation and must not wait for a model generation to stop work once identified.

### Main-agent context and memory

Provide a bounded task snapshot and selected newly relevant messages as temporary generation input. Keep the verified retrieval payload byte-identical and record the supplemental context's actual size/references separately in the generation trace. Account for the total token envelope, including tools, output reserve, main memory, and task context. Do not silently trim RECENT or change the published packing mechanism.

Treat task findings and tool-derived text as evidence, never higher-priority instructions. Fetch specific evidence on demand through task-scoped access; do not replay the mailbox or raw tool log into the main prompt. Reuse the existing compact handoff and output-repair behavior where appropriate.

Proposed ingestion policy for agreement:

- Ordinary user exchanges, substantive steering, and discussion of results retain the existing user/assistant episode contract.
- Proactive progress and completion notifications live in a separate conversation-event projection, visible after reload, with zero episode writes.
- Pure progress/status exchanges should also be display-only; mixed requests that contain substantive conversation remain normal turns. Determine this from the resolved operation, not a brittle phrase list. The exact rule must be settled before implementation.
- A completed result is durably available immediately in task storage/history and becomes a bounded handoff on the next relevant real user turn. Do not fabricate a user message or mutate an already committed episode to memorize an unsolicited result.

Keep existing trace-derived history intact and merge new task/notification events by stable IDs and ordering. Do not rewrite old histories into a new transcript database. Preserve the invariant that episode counts represent actual committed pairs, not all visible rows.

Temporary context must also include the relevant recently delivered status/notification text and pending question association, bounded and referenced by task, revision, and message ID. A replaced progress snapshot alone cannot resolve “the second option,” “that source,” or “yes.” Reuse the saved conversation-event records and what was actually delivered, including after reload; do not create an unlimited second transcript. A newer finding must not silently change the referent of the user's reply.

### OpenCode continuity and artifacts

Extend `SandboxManager` and `OpenCodeRunner` instead of replacing them. Reuse native sessions, messages, event filtering, child tracking, abort, and compaction if the pinned server demonstrably supports the required behavior. A Recollect mailbox is a small durable adapter around those facilities, not a second agent loop.

Keep one warm container with only the current invocation's scratch workspace exposed. A host-side per-conversation archive is never broadly mounted into it. At a safe quiescent boundary, validate and checkpoint that task's allowed files and findings; restore only the selected conversation/task's data into scrubbed scratch for a follow-up. Native session reuse/export/import is an investigation item: no assumption that OpenCode state in tmpfs survives container replacement. If exact native continuation is unavailable, restore files and a structured task checkpoint into a new session and report that distinction.

Do not switch workspace owners while OpenCode or any child still has execution rights. If retained native state exposes an earlier conversation, destroy that state before switching owners. Container isolation and attestation remain mandatory; no extra host mounts, shell access, or automatic host fallback.

Initial proposed artifact formats: UTF-8 TXT, Markdown, CSV, and JSON through existing sandbox edit tools. Rich document formats need separate capability work. Export only validated regular files, with ownership, size/total quota, symlink/reparse/hardlink, traversal, and stable-copy checks. Serve by opaque artifact/version IDs under the existing API boundary, as downloads, not arbitrary host paths. Publish immutable versions atomically; a failed revision leaves the previous version usable. Record content hash, size, media type, task, and instruction revision.

Proposed retention: retain published artifacts, task checkpoints, and source references until explicit reset/delete, subject to an explicit bounded storage policy; prune verbose diagnostic history separately. Reaching capacity produces a recoverable storage blocker, never silent deletion of user artifacts. Reset invalidates pending messages and restores isolation; delete requires its own explicit user action. Final quotas and UI wording remain decisions.

### Scheduling, batching, and caching

Message delivery, task scheduling, and model inference are distinct. Do not equate asynchronous HTTP requests, batched tool calls, server batching, and two cached prompts.

The key unknown is how to interleave main-chat inference with every model request OpenCode makes, including native child calls. Its current traffic goes directly to Qwen. Removing the invocation-wide lock without controlling that traffic does not establish foreground priority or fairness.

Probe an existing backend request hook first. If no suitable hook exists, evaluate one narrow authenticated model ingress into the same Qwen server, used by OpenCode and main chat, to admit requests and measure queue wait. It is not an embedding path or arbitrary HTTP proxy; it must not expose main-chat APIs or host credentials to the container. Build no generic scheduler framework. Select this only if needed to implement the measured policy.

Compare the current single-slot baseline with per-request fair scheduling, and then two-slot continuous batching if supported and safe. Two slots must share the same loaded weights; measure their effective per-slot context and KV allocation rather than assuming each retains 32k tokens. Evaluate server-supported prompt/KV reuse separately from slot concurrency, including possible RAM-backed save/restore and its RAM/transfer costs. The actual working set includes at least main-chat, OpenCode build, and OpenCode general-agent prompt families, plus compaction requests and any observed native child fan-out. Two conceptual agents do not imply only two cached prompts. Measure affinity and eviction across that working set. Prefix reuse may be limited because main memory is rebuilt every turn and date/voice guidance can differ.

Foreground preference must leave research opportunities; background tool waits should not occupy the only model lease. Do not promise preemption of an in-flight generation. Measure worst-case delay from long background generations, bounded per-generation output, cancellation behavior, and starvation under frequent speech. If the target latency cannot be met without changing the model/context constraints, surface that as a decision rather than call queueing equivalent to responsiveness.

### Progress, failures, and voice

A meaningful finding, artifact, decision request, or blocker can request a main-agent update. Coalesce events, suppress stale/redundant updates, and honor quiet mode. Progress answers use the latest accepted task revision and real saved evidence. Tool activity alone is not proof of a finding. A slow but healthy task is not failed solely due to elapsed time.

Retain bounded connection/tool/recovery waits. Diagnose generation idle timeout, model queue wait, tool timeout, step cap, dead event stream, container OOM, and browser disconnect separately. Checkpoint before cleanup; persist evidence when it is produced, not only in a finalizer. A dead event stream requires reconnection/status reconciliation even if the long message request is still alive. Repeated identical failures trigger bounded recovery and then a blocked state with saved findings. Step caps remain execution checkpoints, not permission to falsely claim completion or repeatedly restart forever.

Separate playback abort, foreground response abort, task cancel, voice stop, and application shutdown. Extend speech to consume a server-owned saved notification ID as a separate response kind, reusing synthesis, gating, playback, and text cleanup. Do not weaken the existing verified-turn route to speak arbitrary client text or fabricate a retrieval verification for a notification. On barge-in, obsolete speech is discarded while the task continues.

Proposed disconnect behavior: browser/device disconnection leaves owned tasks running while the server is healthy; no unattended audio. Reconnect shows current state without repeating completed work or duplicate announcements. Server shutdown first stops admission, records/checkpoints owned work, requests worker/child cancellation, and waits for bounded cleanup; only then call `SandboxManager.close_all` and close generator/web clients. `teardown` currently waits on the invocation lock, so calling it first could wait forever on a server-owned task. If abort does not respond, terminate only the owned container after best-effort checkpointing and mark work interrupted. Restart offers explicit continuation rather than silently rerunning potentially non-idempotent tools. Test ordinary idle reaping as well as crashes: native `/state` is a bounded 256 MiB tmpfs and neither survives container recreation nor has unlimited capacity.

## 4. Implementation sequence and completion gates

| Phase | Concrete scope | Exit evidence |
| --- | --- | --- |
| P0 — verify mechanics | Capture pinned OpenCode API/version and llama-server build/help/effective configuration. Reproduce cancellation and identify the reported timeout layer if possible. Demonstrate a semantic report/steering round trip, distinguishing receipt from acceptance; probe child visibility, native state restoration/capacity, compaction, and inference admission. Resolve context/output mismatch. | Capability matrix with observed/unsupported/unknown entries; selected message carrier, scheduling and persistence approach; main/build/general cache working-set evidence; no claims based only on latest docs. |
| P1 — persistent task slice | Add minimal task storage/coordinator and message schema. Implement the P0 carrier, stable originating request IDs, server-owned start/status/cancel, bounded queue, cursor replay, crash reconciliation, checkpoint-on-failure, and ordered shutdown. Add thin text conversation integration: asynchronous acceptance, durable visible task identity, status/cancel tools, bounded task context, and initial history/episode routing. | Task survives initiating response closure; accepted task remains visible if the acknowledgment aborts; explicit cancel cleans up; duplicate commands do not create duplicate tasks; no tool/message episodes; two conversations stay isolated. Feature remains disabled for normal use until later gates. |
| P2 — conversational model access | Implement the P0-selected request admission path; scope model leases to inference rather than full research. Complete the text vertical slice with truthful queued/waiting state, main-chat status handling, and inference fairness. | Delegate → finish acknowledgment → unrelated question → status → cancel through actual main chat during OpenCode/child work; no duplicate delegation/deadlock/starvation in tested inference workloads; measured operating envelope with GPU voice. |
| P3 — steering and continuity | Connect durable mailbox to verified OpenCode message boundaries. Preserve native context where safe and checkpoints where restoration is needed. Add revision acknowledgment, late steering, follow-up association, and resource/step-cap recovery. | Start → progress → steer → complete → continue, plus busy-session and late-result races; restart and cross-conversation isolation tests. |
| P4 — usable artifacts | Add safe archive/export/restore and versioned artifact API; adjust delegation capability wording for supported files. Integrate early checkpoint hooks with P1/P3. | Create/download/revise each supported format; interrupted export and failed revision preserve old version; malicious paths and wrong conversation IDs rejected; recovery survives container replacement. |
| P5 — conversational delivery | Extend the P1/P2 text slice with proactive update policy, persistent UI workspace, notification speech, quiet mode, and explicit reply-to-question/referent handling. Reuse the existing task context/history routing, desktop relay, and voice plumbing. | One real voice scenario covers start, barge-in, unrelated question, prompted update, proactive update, steering, artifact, revision, quiet mode, and cancel. Reload and client reconnect preserve correct state and short-reply referents. |
| P6 — release checks | Repeated hardware evaluation, security/lifecycle sweep, migration compatibility, documentation/configuration updates, and cleanup. Enable only after outcome and performance gates pass. | Full required suite/build plus Docker tests and recorded hardware/voice evidence. Rollback retains artifacts and task data; old mode does not interpret new tasks as runnable. |

P0 is the first gate because unsupported steering or poor model responsiveness can invalidate later architecture. P1/P2 form the first vertical slice. Persistence must precede removing destructive cleanup; notification speech depends on explicit history and memory semantics. Rich file formats and multiple simultaneous delegated tasks are later scope, not prerequisites.

## 5. Hardware experiment and release evidence

No runtime probes are performed as part of this planning task. During implementation, preserve the launch baseline and run agent-owned experiments in an agreed test window, with disposable conversations and documented cleanup. Do not restart or reconfigure the user's active conversation to collect a benchmark.

Capture exact executable/version/hash, model and quantization, launch arguments, effective context/slot limits, cache settings, CUDA providers, driver/runtime, and memory before/after warm-up. The nearby llama.cpp source tree has server documentation but no Git metadata; its documentation is not proof of the installed binary's capabilities.

Exercise short and near-limit main/task prompts, growing task context and compaction, long background output, main/build/general alternation and observed native child fan-out, slow/failing tools, frequent foreground turns, quiet mode, cache hits/misses/eviction, cancellation, and restore. Attribute cache observations to the actual request/prompt family, not only “main” versus “subagent.” Keep Whisper CUDA float16 and Kokoro CUDA loaded and exercise ASR/TTS during research; keep the embedder on CPU.

Report repeated-run median/p95/worst observed foreground queue wait, time to visible answer and first audio, steering acknowledgment latency, explicit cancellation latency, background tokens/time and completed-work throughput, prompt processed/cached tokens, prefill time, peak GPU VRAM, host RAM/cache, Docker memory, OOMs, and recovery behavior. Reuse `PromptCacheTrace`; missing metrics are unknown, not zero. App-level timing must include time spent waiting for a lease, which current generation timings do not establish.

Compare baseline, candidate fair single-slot scheduling, and candidate two-slot/cache configurations. Use fixed workload scripts and multiple repetitions; report sample sizes. Exact accounting and artifact hashes can be asserted; generated answer quality requires repeated assessment. Before selecting a configuration, agree numerical foreground/voice latency targets, maximum tolerable research slowdown, and measured memory reserve. No invented performance promise or unmeasured VRAM allocation counts as acceptance.

## 6. Outcome and edge-case acceptance matrix

| Scenario | Observable result |
| --- | --- |
| Hundreds of tool calls; repeated automatic progress updates | Zero tool-call/internal-message/proactive-update episodes. A bounded snapshot gives truthful status and evidence references. |
| Speech onset, “stop talking,” ambiguous “stop,” unrelated question | Playback stops immediately; same task continues; ambiguity does not implicitly cancel research. |
| Explicit task cancel during queued work, inference, tool call, or final export | Correct task canceled with accurate acknowledgment; children stopped; saved work retained; no late resurrection. |
| “What have you found?” before findings; repetitive or failed research | Honest waiting/working/blocked distinction; no invented progress or blind infinite retries. |
| Several corrections, ambiguous reference, steering at completion | Latest accepted revision is visible; clarification where needed; stale result labeled; explicit follow-up rather than silent duplication. |
| New independent task while another runs | Bounded queue with visible ownership; no replacement; foreground and research both get service. |
| Tool timeout, event disconnect, context exhaustion, model/container OOM, crash | Saved findings survive; actual failure layer reported; retries bounded; no claim of task completion without evidence. |
| Create then revise artifact; unsupported format; failed download/export | Working versioned file or specific limitation; previous usable version protected; no arbitrary host path access. |
| Browser/device reconnect and server/container recreation | Correct current state, retained artifacts/checkpoint, deduplicated deliveries, explicit interruption/continuation semantics. |
| Barge-in or crash between task creation and main acknowledgment | Stable request ID resolves to the same task; original intent remains visible; no fabricated committed episode or duplicate remote dispatch. |
| “Use the second option,” “that source,” or “yes” after a display-only update | Relevant delivered message/question is present in bounded temporary context; correct task/revision resolved even after reload or newer findings. |
| Shutdown during model wait/inference/tool/checkpoint; idle reaper; native-state full | Admission closes first, owned work stops before teardown lock acquisition, partial checkpoint limits are reported, retained work can be restored. |
| Conversation switch, forged IDs, reset, quota/full disk | No cross-conversation files/messages; reset invalidates stale work; storage failure is explicit and does not erase published results. |
| Proactive update during user speech; frequent questions; quiet mode | No overlapping stale speech or repetitive announcements; current preferences win; research not starved. |
| Two caches/slots under long prompts plus GPU voice | Measured capacity/latency and recovery; no second model or CPU voice fallback; cache eviction never loses durable task state. |
| Existing chats and old traces | Readable without migration of episodic history; strict verification preserved; `/v1` existing behavior unchanged unless separately specified. |

## 7. Tests, compatibility, and change control

Extend existing suites rather than build a second testing framework: `test_chat_lifecycle`, `test_chat_history`, `test_generator`, `test_sandbox_runner`, `test_sandbox_manager`, sandbox security/isolation/Docker tests, voice tests/harnesses, deployment/client tests, and `ui/tests`. Add focused persistence/mailbox/artifact tests where those modules are new. Most tests use fake generators and clocks; opt-in Docker and real hardware checks remain separate.

Existing tests intentionally require fresh scratch/session state and abort-on-consumer-close (`test_sandbox_manager`, `test_sandbox_runner`, `test_chat_lifecycle`). Preserve them unchanged as off-mode/legacy coverage wherever that contract remains valid; add continuous-mode tests at the new coordinator boundary. If an unavoidable shared contract changes, identify the exact expectation and replacement before editing. AGENTS.md §5 requires asking before rewriting/deleting tests not authored in this task; obtain approval only for that concrete necessary change, not for all lifecycle tests in advance. Retain isolation, child cleanup, cancellation, and durable-write coverage. No shadow-test weakening.

Task streams use HTTP/SSE already supported by the desktop relay. Exercise slow subscribers, multiple tabs, reconnect storms, cursor expiry, and resource admission limits; long-lived listeners must not exhaust capacity needed for status or cancellation. Task snapshots and cursors are projections of the same persisted state, not additional services or a generalized event-sourcing layer.

At each completed implementation slice run `uv run --no-sync ruff check .` then `uv run --no-sync pytest`; if UI changes, run its tests and `npm run build`. Docker gates must explicitly report enabled/skipped state. Use the existing Python 3.13 environment without resyncing. Additive task schemas need a version and safe forward-compatibility error, plus old-history fixtures. Stage behavior behind an explicit deployment setting; preserve legacy behavior rather than pretending unsupported continuous features work there. This release targets OpenCode; expanding legacy or `/v1` behavior is a separate decision.

No push or PR until task completion and a session request to publish. Keep current services and saved histories intact. Delete owned test conversations, scratch files, temporary profiles, and probe captures; retain only intentional evidence artifacts with documented provenance.

## 8. Decisions to settle and risks

1. Confirm initial one-active-task-plus-FIFO-queue scope (including no start-time promise and blocked-for-input ownership), supported text file formats, and OpenCode-only continuous mode.
2. Agree pure status exchange ingestion and when a completed result becomes a remembered user/assistant exchange. Avoid duplicate memory from notifications and normal replies.
3. Set retention/reset semantics, artifact/diagnostic storage bounds, and whether published versions remain until explicit deletion.
4. Set measured responsiveness targets and permissible tradeoffs after P0 evidence. Busy-session steering, two-context caching, and native state restoration remain hypotheses until probed on pinned binaries.
5. If a shared lifecycle test must change rather than remain valid off-mode coverage, approve its concrete replacement before editing, per repository rules.

Highest risks: OpenCode cannot accept steering at the required boundary; request admission cannot provide acceptable foreground latency; effective context limits are misconfigured; persistence accidentally exposes other conversations; failed delivery duplicates remote work; progress turns pollute episodic memory; artifact extraction races active writes; long-running task state exceeds RAM/disk; UI speech conflates notification and retrieval trust.

Official API references are investigation leads, not pinned-runtime proof: [OpenCode server API](https://opencode.ai/docs/server/) documents session messages, async prompt submission, status, abort, and event facilities; [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) describes slot/cache controls. Confirm exact behavior using the installed versions in P0.

## 9. Forward review

Completed by a subagent after the first draft was written. See [the independent review](SUBAGENT_COLLABORATION_REVIEW.md) for evidence and prioritized findings. The reviewer inspected code and proposed corrections; no implementation or hardware probe was performed.

| Finding | Disposition in this revision |
| --- | --- |
| FR-01: no proven semantic message carrier | Accepted. P0 must prove it; section 3 gives minimal native/MCP adapter options and receipt-versus-acceptance semantics. |
| FR-02: foreground abort/retry can orphan or duplicate task creation | Accepted. Stable originating request IDs, durable intent/association, and explicit crash windows added to P1. |
| FR-03: first conversational integration was too late | Accepted. Task tools, bounded context, and initial history/memory routing moved to P1/P2; P5 extends that slice. |
| FR-04: actual cache working set exceeds two conceptual agents | Accepted. Main/build/general and compaction transitions explicitly enter the probes. |
| FR-05: display-only updates can lose short-reply referents | Accepted. Include bounded delivered-message/question context and add referent acceptance cases. |
| FR-06: shutdown can wait indefinitely on invocation lock | Accepted. Coordinator stops owned work before sandbox teardown; idle reaping and native-state capacity added to recovery tests. |
| FR-07: unlimited active task makes queue start time unbounded | Exposed as a product decision. Proposed initial FIFO behavior is explicit; time-slicing is not silently added. |
| FR-08: preserve old-mode lifecycle tests | Accepted. Add continuous-mode coverage first; only unavoidable shared-contract rewrites require concrete approval. |

Review supports proceeding to the P0 feasibility gate, not assuming live steering or any caching configuration already works. This remains a draft until the product choices in section 8 are agreed.
