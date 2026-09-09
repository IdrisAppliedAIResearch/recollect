# Continuous subagent collaboration — independent forward review

Reviewed 2026-09-09 against draft `SUBAGENT_COLLABORATION_PLAN.md` and repository baseline `ce70db5`. This is a no-code review: no application, test, configuration, model, or service changes. Findings below distinguish inspected code from proposed designs and unknown runtime capabilities. The parent agent owns required checks and final disposition.

The draft identifies the important existing constraints correctly: request-owned delegation, two different locks, destructive scratch/session cleanup, separate episodic memory, and one shared model. A small Recollect task coordinator is justified. The remaining risk is promising a communication contract before identifying how the pinned OpenCode agent will actually send and consume its semantic messages. The first implementation slice should prove that contract through main chat, rather than validating a durable queue in isolation.

## Findings requiring plan corrections

### FR-01 — High: prove the semantic message carrier before designing the complete mailbox

**Evidence.** The plan's message contract promises `accepted`, `finding`, `question`, and revision-aware progress. `runner.py:246` only interprets completed/error tool parts; non-tool parts are discarded at lines 270–274. `_post_message` at line 312 submits a single native prompt and waits for its final response. `configgen.py:44` denies OpenCode's native question tool, and `mcp_research.py` exposes only web search and fetch. These inspected mechanisms provide tool activity and final prose, not the proposed semantic protocol.

**Consequence.** An ordered SQLite mailbox cannot by itself make the worker report supported findings or acknowledge that it has incorporated a steering revision. Treating every tool completion as a finding would manufacture semantic progress. Enabling the native question tool without an answer/permission bridge could instead leave the worker waiting indefinitely.

**Correction.** Make a bidirectional carrier demonstration a P0 exit gate:

- First probe native OpenCode text/message events, busy-session prompt delivery, and acknowledgment boundaries. Record exactly when an instruction is received versus incorporated.
- For outgoing structured findings/questions, evaluate one narrowly scoped reporting tool added to the existing MCP server. Its completed tool event could reuse `_apply_event` as the host adapter; a general event bus or second model loop is unnecessary. Bind the report to the coordinator's known task/session, validate its bounded payload, and deduplicate it by the existing tool call ID. A tool's local success is not proof of durable host receipt: demonstrate reconciliation of missed events from native message history before calling delivery reliable.
- Prefer native message delivery for incoming steering if the pinned server supports it. If it does not, explicitly scope a small task-control bridge as new capability work; do not claim tool-boundary steering emerges from the current runner. Do not put trusted steering into freely editable workspace files or infer acceptance from an HTTP 200 alone.

Choose the carrier in P0, implement the minimal message round trip in P1, and test questions plus steering in P3. Runtime feasibility remains unknown; the code limitations above are verified.

### FR-02 — High: make task creation independent of an aborted or retried foreground turn

**Evidence.** `ChatRequest` (`api.py:103`) has only session, message, and input mode; it has no stable client request ID. `_find_run_subagent` (`api.py:818`) creates a new random run ID. `_stream_turn` dispatches research before `commit_turn` (`api.py:796`), and its existing disconnect tests expect neither an episode nor saved turn to remain. The plan deduplicates mailbox messages but does not define the originating conversational request's identity or its relationship to the episode write.

**Consequence.** After research becomes independent, barge-in can occur after task persistence but before the main acknowledgment is saved. Retrying that user request could create another task, or the user could return to an active task whose initiating exchange is absent from history. Task SQLite and episodic storage cannot be treated as one transaction.

**Correction.** In P1, specify a stable originating request/command ID, record the originating user instruction and task association durably before dispatch, and distinguish command acceptance from a completed conversational episode. Define reconnect/retry behavior when the acknowledgment was never delivered. Do not fabricate a completed user/assistant episode to fill the gap. Test disconnect/crash immediately before and after task persistence, remote submission, acknowledgment delivery, and foreground episode commit. Existing mailbox deduplication should be reused for command IDs rather than adding another retry subsystem.

### FR-03 — High: move a minimal conversational slice ahead of artifacts and full notification delivery

**Evidence.** P1 and P2 promise persistent work and conversational model access, but P5 currently owns task snapshots, explicit memory routing, conversation-event history, persistent UI, and notification speech together. Today the main model receives only `run_subagent_tool` (`api.py:562`); there are no exposed task status/steer/cancel tools. The existing run tool describes a synchronous compact result, and `Generator.build_messages` (`generator.py:167`) receives only system, retrieved memory, and the user's message.

**Consequence.** A coordinator and inference admission path can pass endpoint-level tests without proving that main chat knows what is running, tells the truth about acceptance, or avoids redelegating a status question. Discovering these problems after continuity and artifacts would be expensive.

**Correction.** Put a thin text-only conversational integration into P1/P2: asynchronous task acceptance, visible durable task identity, bounded active-task context, status/cancel tool exposure, and the selected episode/history policy for those exchanges. P3 then adds conversational steering. Leave proactive delivery policy, richer task UI, quiet mode, and notification speech in P5. This establishes an actual first vertical slice: delegate → finish acknowledgment → ask unrelated question → ask status → cancel, while the original task remains separately owned.

### FR-04 — Medium: a two-cache hypothesis must include three or more actual prompt families

**Evidence.** The draft correctly preserves OpenCode's native build/general workflow. `configgen.py:17–18` names both agents, enables task delegation at lines 26–29, and gives each its own step cap at lines 104–112. Main chat is a third prompt family. Compaction may add more model requests. Both `model` and `small_model` point to the same provider/model (`configgen.py:66–67`).

**Consequence.** Two cached states may retain main chat plus one worker context while native parent/child alternation continually evicts the other. Two physical inference slots are not automatically two persistent agent caches. The draft mentions child inference, but its performance comparison should explicitly measure this working set.

**Correction.** Add main/build/general alternation, child fan-out as actually supported, and compaction requests to the P0/P2 cache matrix. Measure request identity, cache affinity, eviction, and usable context across those transitions. Compare two-context caching with the actual working set rather than promise one cache per conceptual main/subagent pair. Keep cache affinity metadata in the selected admission adapter; durable task state remains independent.

### FR-05 — Medium: display-only conversation still needs enough temporary context to resolve the user's reply

**Evidence.** The proposed ingestion policy excludes progress and notifications from episodic memory and replaces progress snapshots in place. `SessionManager.prepare_turn` (`session.py:219`) currently reconstructs generation context exclusively from episodic retrieval; `chat_history` at line 366 is a display projection. A saved notification or answered status question therefore does not automatically become input to the next main-agent turn.

**Consequence.** After the assistant says “A and B are the remaining options,” the user may reply “Use the second one.” A changed task snapshot may no longer contain that ordering. Similarly, an unsolicited task question needs a durable association with a short reply such as “yes.” Merely saving the visible text is insufficient.

**Correction.** Include the relevant recently delivered notification/status reply and pending question association in the bounded temporary task context, with task/revision/message references. Use the existing conversation-event records as the source; do not create a second unlimited transcript or episodic copies. Test “that source,” “the second option,” and “yes” after prompted and unsolicited updates, including a reload and a simultaneous newer finding. Define whether an answer is a `steer` message referencing the question or an additional operation; avoid an unnecessary new message type if `steer` suffices.

### FR-06 — Medium: explicitly order shutdown and recovery to avoid waiting forever on the worker

**Evidence.** `SandboxManager.teardown` (`manager.py:427`) first acquires `_invocation_lock`; `close_all` (`manager.py:457`) calls it after stopping the reaper. `api.py:231` currently invokes `close_all` directly during lifespan cleanup. That works with request-owned finite workers, but a server-owned task with no overall deadline can retain the invocation lock indefinitely. The draft describes saving and stopping work on shutdown without spelling out this dependency.

**Correction.** P1 must stop new admission, mark/save owned task state, request worker/child cancellation, await bounded cleanup, and only then call sandbox teardown and close model/web clients. If native abort is unresponsive, the existing container teardown is an infrastructure recovery path after a best-effort checkpoint; report the task interrupted. Test shutdown during model wait, inference, tool execution, blocked input, and checkpoint failure. Also test idle reaper recreation: `/state` is a 256 MiB tmpfs (`isolation.py:130`), so ordinary idle teardown can erase native state just as a crash can. Include native-state capacity exhaustion in the continuity probe without automatically enlarging its limits.

### FR-07 — Medium: define what a global queue promises when the current task has no deadline

**Evidence.** The draft proposes one active delegated task globally plus a bounded queue, while productive work may continue indefinitely. Existing serialization is verified by `test_different_chats_queue_on_the_shared_server` (`test_sandbox_manager.py:236`). Per-request inference fairness provides opportunities to main chat; it does not let a second delegated task acquire the occupied sandbox.

**Correction.** Keep the small FIFO queue if that is the chosen initial product scope, but explicitly state that queued tasks have no guaranteed start time while another task is active. Decide what happens when the owner becomes blocked awaiting user input: either checkpoint/release the worker at a demonstrated safe boundary, or accurately report that other tasks remain waiting. Do not silently add task time-slicing or arbitrary deadlines to solve this. If user-controlled reprioritization or pause/switch is required, make it a separate accepted scope item and test restoration; otherwise preserve it as a later decision.

### FR-08 — Low: preserve existing off-mode tests before requesting permission to rewrite contracts

**Evidence.** The plan proposes feature gating and retaining legacy behavior, but section 7 anticipates replacement of the existing fresh-session and consumer-close tests. Those behaviors can remain valid for the disabled/old workflow. `test_warm_server_gets_fresh_sessions_and_scrubbed_scratch` and `test_research_abort_finishes_before_outer_stream_releases_session` are valuable existing coverage.

**Correction.** Prefer keeping those tests unchanged as off-mode/legacy coverage and add continuous-mode tests at the new coordinator boundary. Change an existing expectation only where an unavoidable shared contract actually changes, then follow AGENTS.md's concrete approval requirement. Do not create an approval dependency for every old lifecycle test in advance.

## Reuse and abstraction check

- Retain `SessionManager` for conversation ownership and existing trace/history storage. A separate task SQLite store is a reasonable proposal for mutable state and transactional deduplication; it does not require replacing file-based traces or modifying `EpisodeStore`.
- Keep the first schema small: task records, ordered messages, notification records only where needed, and artifact metadata/files. Cursors and snapshots are read projections of that state, not separate services. Add no broker, generalized actor hierarchy, or independent event-sourcing layer.
- Extend `OpenCodeRunner`'s session filtering, tool-call deduplication, and final-result adapter. Do not construct a parallel agent execution loop to manufacture semantic progress.
- Retain `SandboxManager`'s lifecycle locks, scrubbing, attestation, and idle reaper. Separate resource ownership deliberately rather than deleting locks to obtain concurrency.
- Reuse `PromptCacheTrace`, HTTP/SSE helpers, voice synthesis/playback, desktop `/api/*` relay, and path/identifier validation. New task streams should use the existing relay-supported transport and be exercised through deployment resource limits; long-lived subscribers must not exhaust admission needed for cancellation.
- A selected model admission adapter needs a narrow trust boundary. Do not give the container the deployment bearer token or broad `/api/*` access to solve inference scheduling.

## Decision and release checklist

These remain product choices or measured gates, not facts established by this review:

1. Native semantic reporting, busy-session steering, restoration, request admission, and cache behavior must be demonstrated on pinned binaries before committing to their implementation shape.
2. Agree queue semantics, initial text artifact formats, status-episode policy, retention/reset behavior, and numerical responsiveness targets. P0 measurements should inform targets, not silently choose what the user will tolerate.
3. Explicitly distinguish no arbitrary task deadline from bounded transport/recovery waits. Both current backend implementations already claim no client wall-clock limit; identify the actual reported failure layer before removing a timer.
4. Keep core concurrency, memory isolation, artifact persistence, and voice outcomes as release gates. Feature flags permit small verified changes but do not make an incomplete workflow releasable.
5. Retain exact version/configuration provenance for repeated hardware measurements. No inference or voice performance was measured in this review, and no runtime capability above should be marked verified because an upstream API page describes it.

Suggested disposition: incorporate FR-01 through FR-06 and FR-08 directly; expose FR-07 as an explicit initial-scope decision. The plan is suitable for implementation planning after those corrections and P0's feasibility evidence, not yet a promise that live steering or two-context caching works.
