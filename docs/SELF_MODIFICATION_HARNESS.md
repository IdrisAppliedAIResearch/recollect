# Self-modification harness implementation status

This is an implementation work log, not a preregistration amendment or a claim
that the live experiment is ready. The original protocol, checkpoint protocol,
and amendment 01 remain unchanged; [amendment 02](SELF_MODIFICATION_AMENDMENT_02.md)
prospectively supersedes their elapsed-time cutoffs.

## Planning provenance

[The readable planning record](SELF_MODIFICATION_PLANNING_CONVERSATION.md) and
its JSON companion capture all 15 user/assistant messages from the architecture
question through the capture/implementation instruction: seven user messages,
six final answers, and two commentary messages. Message text was extracted from
the original local thread record, not reconstructed from a summary.

On 2026-09-12, a separate source comparison checked that every user/assistant
message in the selected source interval was captured exactly once, in order,
with identical text, role, phase, timestamp, UTF-8 byte length, and SHA-256.
Tool traffic, internal reasoning, and environment/instruction messages are not
part of this conversational capture. The detached receipt hashes both files.
Repository tests check their integrity and that the readable rendering contains
every exact message. Hash checks preserve the source-verified capture; they do
not independently prove the authenticity of the original local thread.

## First implementation slice

The initial modules in `src/recollect/selfmod/` implement an offline,
deterministic pre-submission foundation (`contracts.py` and `development.py`):

- Immutable original-task contract with explicit requirement IDs and evidence
  descriptions. Plans reference those IDs; plans cannot redefine completion.
- Explicit change policy over byte snapshots. Protected bytes come from the
  trusted baseline, never from an agent's attempt to restore them. Reject unsafe
  paths, unexpected changes, unapproved deletions, and path aliases.
- Plan -> fresh forward review -> implementation -> development checks -> fresh
  code review -> candidate-ready gates. Reviews bind exact plan/artifact digests,
  cannot be self-approved, and are invalidated by revisions. Require every frozen
  development check; reject missing/extra/failed checks.
- Controller-supplied finite time/revision limits and instance fencing. A worker
  failure ends the primary path; no refresh can reset eligibility or the budget.

The subsequent user-directed [no-quota decision](SELF_MODIFICATION_ROLES.md)
removes development-update/review and model-call/token ceilings from the current
implementation. Amendment 02 subsequently removes agent/attempt time cutoffs;
submission and provider retry counts remain.
the historical planning capture and validation records are unchanged.

Forward review refined the implementation to retain ordinary development-test
failures as revisable feedback, inherit the original attempt deadline, cover
every contract requirement, reject entire forbidden snapshots, and bind receipts
to controller-assigned identities and a revision epoch. An epoch change prevents
reusing old approval even when a later edit restores identical bytes. Creation
grants include descendants of the named directory but never authorize overwriting
an existing protected file. Renames require separate authorized delete/create
operations; case-only aliases are rejected.

Read-only independent code review found no blocking finding in this slice under
the stated trusted-host and complete-snapshot contracts. Validation on 2026-09-12:
Ruff passed; 68 targeted tests passed; full pytest reported 1,158 passed,
4 existing skips, and 2 warnings in 83.58 seconds. No runtime readiness is claimed.

This slice does not run candidate code, mount filesystems, accept agent RPCs,
persist CP2, deploy workers, or decide whole-experiment PASS. Snapshot checks
are not a security sandbox. Structured reviewer reports remain model judgments;
their identity must be authenticated by the future trusted runtime, not supplied
as authoritative claims by the modifier.

## Offline checkpoint/controller slice

`journal.py`, `checkpoints.py`, and `controller.py` add an append-only SQLite
evidence archive, materialized CP0-CP6 bundles, independent byte/chain readback,
one-shot simulated scheduling, candidate/retest accounting, and the timed CP5
endpoint. Persistence or verification failures close primary progression.
Recovery is failure accounting only, including damaged or interrupted evidence;
it never reconstructs a passing attempt from the archive.

The [implementation contract](SELF_MODIFICATION_CHECKPOINT_IMPLEMENTATION.md)
describes trust assumptions, ordering, fault cases, and storage limitations.
These APIs accept trusted fixture observations and produce simulation results,
not live experiment PASS. They are not wired to the development controller,
real agents, privileged routing, or independent provider collectors yet.

Independent forward and code review identified timing, infrastructure-retest,
and interrupted/damaged accounting edge cases; fixes and regression tests were
re-reviewed with no remaining blocking findings in this offline scope. Validation
on 2026-09-12: Ruff passed; the final full suite reported 1,298 passed, 4 existing
skips, and 2 warnings in 128.22 seconds. This slice adds 140 tests, including real
child-process termination and fault injection. Preregistration and planning-record
hashes remain unchanged. These are software/process tests, not a hardware
power-loss qualification or a live capability result.

## Remaining runtime work

The [offline containment profile](SELF_MODIFICATION_CONTAINMENT.md) now specifies
the disposable worker/watchdog boundary. The
[host fixture executor](SELF_MODIFICATION_EXECUTOR.md) adds journaled lifecycle
ordering and separately authorized diagnostic refresh using an injected runtime.
The [local Docker adapter](SELF_MODIFICATION_DOCKER_RUNTIME.md) now implements
bounded CLI transport and async cancellation finalization, tested with a fake CLI
and benign local child processes. The containment profile now has 14 passing live
Docker cases. A further 12-case [production-path qualification](SELF_MODIFICATION_DOCKER_RUNTIME.md)
exercises the actual adapter/executor, cancellation, response-loss reconciliation,
bounded diagnostic refresh, and exact evidence transfer to simulated CP2/CP6.
Those tests remain fixture qualification, not modifier inference or deployment.

The [host-local development integration](SELF_MODIFICATION_INTEGRATION.md) now
wires verified CP1 release, one-shot role capabilities, owned executor handoff,
development gates and linked CP2/CP6 evidence. The subsequent
[isolated role runners](SELF_MODIFICATION_ROLES.md) add bounded one-shot model
requests and separate networkless plan/reviewer/modifier/check containers, with
frozen inputs and authenticated owned-executor results. This is host-brokered
structured inference, not native OpenCode or an interactive agent tool loop.
Checkpoint outcomes remain simulations; no target trial is authorized.

| Planning requirement | Remaining implementation / verification |
| --- | --- |
| Deterministic controller and checkpoints | Qualify real authenticated collectors and the host-local development integration; qualify installed storage/SQLite durability; freeze runtime identities |
| Containment and refresh | Fixture containment and production-path fault matrix qualified; freeze qualified role profiles; add model-server cancellation/reconciliation before concurrent scheduling |
| Controlled candidate construction | Fixture quiescent capture and protected-byte reconstruction qualified; wire immutable deployment artifacts and dependency locks |
| Review due process | One-shot fresh reviewer contexts, read-only containers, stable findings/resolutions and owned reports implemented without token/call/update/review-count quotas; integrate normal agent loop/compaction and freeze its profile |
| Three concurrent streams | Three pinned lanes, slot settlement and linked overlap check implemented with fixtures (2026-09-15); real three-slot server qualification awaits user approval to reconfigure the model server |
| A/B deployment | Bundle digest, create/copy/commit image build, copy-out launch verification and CP4-gated journaled router implemented (2026-09-15); integrate with the task coordinator's sandbox launch |
| Original task defines done | Provider broker, read-only verifier, replay, cleanup, attribution and gap trigger implemented against fixtures (2026-09-15); user-performed Google authorization, private calendar inputs and A's gap-report channel remain |

Review severities and resource/byte policies must be frozen in the runtime manifest
before CP0; amendment 02 prohibits agent-work time/token/call ceilings. No prebuilt calendar adapter or target
rehearsal is included. No live services, account connection, calendar mutation,
publication, or experiment execution is authorized by this implementation log.

## Current observational timing policy

The user removed work-time and experiment-time cutoffs after the one-shot role
slice. Amendment 02 records this prospectively; its detached SHA-256 is
`cb0c85b79df7272a58308fd608f78d44ff877371c1aae7b0e0d0d5e1fc231175`.
All five original registration/planning file hashes remain unchanged. New active
controller configurations require `timing_amendment`; historical bundles remain
inspectable without gaining active execution authority.

Controller/development, model HTTP, native CLI and container startup/execution
use explicit unbounded timing. CP5 still records the actual endpoint only after
receipt readback and marker durability, including elapsed nanoseconds; CP6 retains
the observations. No elapsed/quiet-time threshold decides success or failure.
Clock continuity, original-task correctness, review, scope, provenance and
explicit cancellation remain required. Candidate/provider retry counts remain.

PID 1 independently observes attachment hangup without consuming release bytes.
Three-second cleanup/final-drain timing starts only after trusted settling or
observed supervisor exit. Final protocol framing/EOF and namespace quiescence
remain required. An unpropagated disconnect or uncooperative cancellation can
remain unresolved; no age-based kill or claim of confirmed settlement substitutes
for evidence. Retain busy authority fences until settlement.

Independent forward/code review and focused rereviews closed completion-framing,
terminal-deadline preservation and exit-ordering findings. New deterministic
checks cover explicit null/missing/mixed release modes, multi-day work/protocol
clocks, cancellation and clock loss, active registration requirements and
historical inspection. Live qualification covers an 11-second startup delay plus
61 seconds of silent work, real role containers, and explicit disconnect while
the supervisor is SIGSTOP-paused. These are synthetic qualifications, not a third
model probe or a Calendar result. The native continuous agent and ordinary app
transports must be integrated under this timing policy before a target trial.

Final post-review validation on 2026-09-12: Ruff `All checks passed!`; full pytest
with both Docker opt-ins and the separate model-probe opt-in off reported
`1723 passed, 2 skipped, 2 warnings in 378.39s (0:06:18)`.
Qualification archives/XML are retained under `.agent/unbounded-*`. Test-owned
containers and external input roots were removed by verified fixture teardown;
no matching leftovers remained. Existing Recollect/Qwen services stayed healthy
on the same PIDs. No configuration, model probe, Calendar, commit or push changes.

## Iterative development prerequisite (2026-09-14)

The [role lifecycle driver](SELF_MODIFICATION_ROLES.md) now handles forward-review
rejection, failed checks and code-review revisions automatically through the
existing owned executor. Candidate absence is explicit before implementation,
and forward review assesses proposed work. It stops at READY with no automatic
submission/activation. Ownership remains held through inter-role handoffs,
cancellation and final-delivery eligibility checks. Every driver decision and
failure is journaled and included in CP2/CP6 evidence. No agent-work limits were
added and no original registration or planning bytes changed.

This is a prerequisite within remaining-work item 1, not completion of native
continuous-agent integration. The existing OpenCode runner has a different
security boundary; it was not attached to the self-modification controller or
given broader permissions. Native tool use, compaction and agent-directed
replanning remain outstanding, along with the other live-experiment work above.

Final post-review validation: Ruff `All checks passed!`; full pytest with both
Docker opt-ins reported `1735 passed, 2 skipped, 2 warnings in 410.82s (0:06:50)`.
The new scripted lifecycle used 12 distinct real containers and included a rejected
plan, a failed real check, a rejected code review and successful revisions before
READY. Offline tests additionally cover retained findings, multi-day observations,
repeated revisions, competing claims and cancellation/revocation at handoff edges.
These results do not establish a real-model lifecycle or target capability.
Evidence, including the initial invalid test-fixture failure, is retained under
`.agent/driver-*` and the associated XML files. No real-model probe, Calendar
action, service/configuration change, dependency installation, commit or push ran.
All 43 live Docker cases passed. No test-owned selfmod containers or shared input
roots remained after teardown. The two skips were the existing Windows symlink
privilege case and the separately gated real-model probe.

## Native OpenCode compatibility prerequisite (2026-09-14)

The separate [native session adapter](SELF_MODIFICATION_NATIVE.md) now exercises
real native read/edit, retained sessions, explicit compaction and continuation
through a scripted provider in a network-disabled container. It remains entirely
outside controller receipt/approval/deployment paths. The ordinary app runner and
existing fixture execution profile are unchanged.

Pinned timeout omission is accepted without introducing a chunk timer. Host
requests/history and partial failures are journaled; cleanup/relay cancellation
retain ownership through settlement. Exact source permissions remain enforced by
OS ownership. Automatic-pruning completeness and the observed native per-response
token cap are explicit unresolved experiment blockers, not claimed successes.
This slice does not complete native production execution or the target trial.

Final post-review validation: Ruff `All checks passed!`; full pytest with both
Docker opt-ins reported `1765 passed, 2 skipped, 2 warnings in 426.60s (0:07:06)`.
All 44 live Docker cases passed, including the native read/edit/manual-compaction
and continuation case. The 29 focused native protocol/relay/cleanup tests also
passed. Fresh-context review cleared the diagnostic scope after the final fixes.
The two skips remain Windows symlink privilege and the separately gated real-model
probe; the two warnings remain dependency deprecation/forward-reference warnings.
Final XML is `.agent/native-final-full.xml`; all `.agent/native-*` evidence is
retained, including failed compatibility attempts and the earlier full run.
No matching selfmod test containers or external shared fixture/native/runtime
input directories remained after teardown. All six registration/planning hashes
are unchanged. No model, Calendar, service/configuration, dependency, commit or
push changes were made.

## Native broker, durable history and admission foundation (2026-09-14)

The [native integration notes](SELF_MODIFICATION_NATIVE.md) distinguish this joint
slice from the still-unqualified production executor. Actual pinned native
read/edit and both manual/automatic compaction now run through the host model
broker in the network-disabled diagnostic fixture. The broker removes all three
top-level token-cap fields while preserving other request fields and native
compaction capacity metadata. Text/tool-only validation blocks media-fetch
delegation without rewriting code, tool arguments or Qwen reasoning text.

The trusted diagnostic supervisor received explicitly approved read-only private
filesystem access; the worker retains zero effective, permitted and ambient
capabilities. Committed native events, including original tool output, are
archived in bounded pages/fragments. After writer freeze, finalization rereads the
entire prefix against host-held event identities and hashes. Failed semantic
capture preserves raw database/WAL evidence before removal; uncertain capture or
launch ownership retains the container and inputs. No raw diagnostic is labeled
complete semantic history.

The new host-local admission seam consumes the real opaque execute grant and
freezes contract, plan and findings under the existing controller lease. It
deliberately cannot admit a candidate or issue a success receipt; closing this
foundation fails the primary path. Production execution, immutable candidate
handoff, independent native reviewers and upstream quiescence remain outstanding.

Independent forward/code review closed five findings: EOF-close cancellation,
task/authority updates hidden by later restoration, media-fetch delegation,
mutation behind the durable history cursor, and failure/ambiguous-launch evidence
loss. Focused author runs reported admission37, broker316 and history83 passing
tests. The final launch/transport fault suite reported `16 passed in 0.59s`.

Final post-review validation: Ruff `All checks passed!`; full pytest with both
Docker opt-ins reported `2232 passed, 2 skipped, 2 warnings in 508.78s (0:08:28)`.
All 46 live Docker cases passed, including the three native cases and
failed-history raw DB/WAL readback.
The skips remain Windows symlink privilege and the separately gated real-model
probe; the warnings remain the existing dependency deprecation/forward reference.
Final XML and diagnostic artifacts remain under `.agent/native-joint-final*`;
earlier failed/passing `.agent/native-*` evidence is retained. Teardown left no
containers or matching external selfmod input roots. All six immutable hashes
were reverified unchanged. No real-model/Calendar trial, service/configuration
change, dependency installation, commit or push occurred.

## Native terminal source-capture work (2026-09-14)

The initial checkpoints below retain their failed and incomplete results;
subsequent repair and qualification results follow chronologically.

The new fixed-path collector and host verifier bind immutable source bytes to a
native run, authority, terminal stop, and finalized history. The collector uses
PID-bound terminal signals, every-thread credential checks and descriptor-relative
no-follow source reads with inode/metadata continuity. It does not return an
execution receipt or change controller admission. The existing fixture supervisor
is unchanged. The new native capture profile adds root `KILL` for terminal
shutdown, not an agent-work timeout; database access remains read-only.

Independent review resolved host inventory/type/limit/inode issues and manifest
encoding/authority mismatches. Host-focused tests reported `45 passed in 1.51s`;
an additional actual-admission-context test was subsequently added but has not yet
run. Worker-focused tests reported `118 passed, 1 skipped in 0.29s`; that skip is
POSIX descriptor traversal on Windows, separately exercised by the live cases.

First live qualification reported `1 failed, 5 passed, 3 deselected in 142.92s`.
Exact native source capture and four source-tamper cases passed. The history fault
failed during injection because root correctly lacked database write access.
The test-only injection was changed to the existing unprivileged database owner;
the second run reported `1 failed, 4 passed, 4 deselected in 108.92s`. Injection
succeeded, but collector rejection was `Native SQLite read failed: attempt to
write a readonly database`, not the expected prior-row identity mismatch. All four
source faults passed their specific rejection-message checks. Retained manifests
show WAL/shared-memory sidecars absent in this failing case, unlike successful
terminal capture; the read-only reopening behavior needs further diagnosis.

Work paused under the repository's two-failed-attempt rule before a third attempt.
No assertion or permission was weakened. Full-suite verification has not run for
this slice, and it is not marked complete. Evidence remains under
`.agent/native-capture-*`. Teardown confirmed no containers or matching external
input roots remain; the six immutable registration/planning hashes are unchanged.

### Read-only SQLite repair follow-up (2026-09-14; full qualification pending)

The user authorized resuming the investigation. The terminal collector now reads
a root-private temporary copy of the stopped database and its WAL/rollback
journal. SQLite can create its own sidecars without writing the original native
state. Live reading, supervisor permissions and the fixture supervisor are
unchanged. No immutable-mode bypass was introduced. The helper checks source
identity/metadata and inventory continuity and cleans scratch on success/failure.
The existing 32 MiB evidence mount can exhaust copy capacity; hot-journal recovery
requiring database writes is deliberately refused. Production ownership and
candidate receipts remain unimplemented.

Fresh review preferred this copy approach and identified ancestor permission
continuity and scratch-capacity reporting, both now addressed. Focused capture,
worker and initial terminal tests reported `175 passed, 1 skipped in 2.04s` after
correcting a fixture import and Windows path/descriptor ctime mismatch. Linux
retains its full metadata comparison. Additional terminal fault coverage passed
15 cases, including retained committed WAL/uncommitted tail, changed-size and
same-size mutation, disappearing sidecars, I/O failures, Linux ctime rejection
and hot-journal refusal. However, a new Windows replacement injection failed
twice with `PermissionError`, including after the copy descriptor closed:
`1 failed, 15 passed in 0.53s`. That investigation is paused under the repository
rule; its expectation has not been relaxed and no third attempt has run.

Live qualification completed `7 passed, 3 deselected in 166.87s (0:02:46)`.
Both retained-WAL and clean-checkpoint capture succeeded. All five tamper cases
passed their exact rejection checks, including `Prior row identity mismatch`.
The clean-checkpoint and history-tamper cases independently reproduced the
original read-only SQLite error before capture; original database/sidecar hashes
and metadata were unchanged afterward, and temporary copies were gone. Latest
Ruff check is clean. Full-suite verification remains pending; this slice is not
marked Done. Failed and passing diagnostics remain under `.agent/native-capture-*`.

### Windows fixture repair and regression verification (2026-09-14)

After user approval to resume, the remaining replacement fault was traced to the
test database fixture's unclosed SQLite connection. SQLite's transaction context
commits or rolls back but does not close the connection. The fixture now uses
`contextlib.closing` around that context, including failure cleanup. The two
terminal tests that own additional connections use the same explicit lifetime.
No production code or fault-rejection expectation changed in this repair.

Two regression cases retain the connection reference and verify that it is
closed after both successful setup and setup failure, so garbage collection
cannot mask the defect. The original Windows replacement fault now reaches and
passes its exact identity-rejection assertion. Fresh independent review found
no blocking issues with the fixture fix. Focused validation reported Ruff
`All checks passed!` and `265 passed, 1 skipped in 7.15s`.
Evidence is retained under `.agent/native-capture-fixture-closed*`.

Final full-suite verification with both Docker opt-ins: Ruff `All checks passed!`;
pytest `2421 passed, 3 skipped, 2 warnings in 674.51s (0:11:14)`.
All 53 live Docker cases passed, including all ten native cases. The three skips
are Windows symlink privileges, POSIX-only descriptor traversal (covered in live
Linux tests), and the separately gated real-model probe. The two warnings remain
the existing Starlette/httpx deprecation and settings forward-reference warning.
Final evidence remains under `.agent/native-capture-final-full*`, with prior
failed/passing capture evidence preserved. Teardown confirmed no containers or
matching external selfmod input roots remained. All six immutable registration
and planning hashes match. The terminal capture prerequisite is verified;
production native executor/receipt, upstream settlement and current-authority
candidate handoff are still outstanding. No real-model/Calendar trial, service
change, permission expansion, dependency installation, commit or push occurred.

## Production native executor, scalable evidence, lanes, deployment and provider scaffolding (2026-09-15)

Implementation and fixture/live-Docker qualification, not a registration, CP0,
target trial or real-model probe. Every earlier failed attempt remains preserved.

### 1. Production native executor and authenticated handoff

PID 1 (`native_supervisor.py`) now owns the only helper-creation path. A
`LaunchGate` registers before spawning, never holds a lock across creation, closes
irreversibly on any terminal event and reaps only under its kill lock. Terminal
settlement waits for in-flight creation, kills every helper, reaps root history
readers before the collector (whose census would reject a live root peer) and
reaps unprivileged relays afterwards. A failed settlement skips the collector and
retains the namespace. Host-issued, strictly monotonic operation IDs drive two
lanes: the fixed native HTTP relay launched as UID/GID 65532 with no groups, and
the root SQLite history reader. Every data frame waits for host credit; helper
stdout is re-framed as data and never parsed as control. Readiness is a
connect-only probe. Launch credentials are recorded only while the helper is
provably unreaped. The relay sets `PR_SET_DUMPABLE` 0 as defense in depth; its
bytes are the worker's own API data, so same-UID tampering is not a control bypass.

The host (`native_runtime.py`) attests and attaches the namespace, demultiplexes
control, model, HTTP and history frames through one reader without cross-lane
blocking, and pumps model exchanges into `NativeModelBroker`. Waits wake on host
failure or the bound terminal-collection frame; sends never hang behind a stopped
PID 1 reader. Before collection the host issues no Docker exec, including for
readiness and history. `finish()` requires idle work, proves whole-session
projection/event agreement, fences, verifies collector stop, finalizes history
at an unchanged watermark, captures and verifies source, and returns a
`NativeCandidate`. `NativeAdmission.handoff()` accepts it once from the owned
adapter only, reconstructs it through development gates and mints a
`NativeReceipt`; the lease is retained until `close()` confirms namespace removal
and upstream settlement. Closing without handoff archives `/evidence`, preserves
raw database/WAL/SHM bytes after collector-confirmed stop, removes the verified
namespace and fails the primary path. Unconfirmed collection retains the namespace.
`IntegratedDevelopment.execute_native` and `run_until_ready(native_executor=)`
put this modifier into the existing checks/fresh-review/submit flow.

An independent fresh-context review found no safety-invariant violation and 12
liveness/race/evidence findings; all were fixed and covered by tests.

Receipts: `.agent/native-runtime-live-20260915-082409.xml` (5 live cases: handoff;
driver with real container checks, fresh scripted code review and CP2 submit;
live pruning; close without handoff; fence during a model exchange). Preserved
failures: `native-runtime-live-20260915-080207` (ECHILD on an already-reaped
helper, fixed in `LaunchGate.wait`) and `native-driver-live-20260915-080600`
(test fixture attribute bug).

### 2. Complete, scalable evidence

`Journal.append` binds each write to the owned head (exact schema, head row and
hash, no orphan rows) and reads back only the new record, so cost no longer grows
with archive length. Full prefix verification remains `verify()` at checkpoint,
export and finalization; a modified older record is caught there, not trusted.
Closed native session/history/broker/runtime journals are copied into controller
sidecars bound by exact anchor in the settlement record and exhausted at CP2
submission and CP6 accounting; a tampered sidecar blocks CP2 and is accounted.
`verify_projection` requires the whole native projection, including pruned tool
parts, to equal committed event state. Live pruning qualification confirmed the
pinned binary's rule: compacted parts reach later model context as cleared while
the event log retains the original output.

### 3. Three concurrent inference lanes (fixture level)

`ModelAdmission(slots=3)` gives conversation, worker and modifier exclusive lanes
pinned to server slots 0, 1 and 2 through host-added `id_slot`; workers cannot
choose a slot and the modifier broker adds its slot only after request validation.
`SlotSettlement` requires the pinned slot idle before dispatch and polls, without
deadline, until it is idle after completion or cancellation; only then does the
broker record `pinned_slot_idle_confirmed` and report upstream settlement.
`concurrency.run_concurrency_check` streams generic non-target probes with no token
cap, samples `/slots` with host monotonic time, and counts generation only where a
linked slot's single task advances decoded tokens inside its request window. The
registered one-second simultaneous criterion, correctness and durations (no
cutoffs) are recorded. Serialized, prompt-only or incorrect runs fail. The user's
running server reports one slot; the opt-in real-server test has not run.

### 4. Immutable A/B deployment (scaffolding)

`SubagentBundle` binds the candidate tree, a required dependency lock, the base
image ID and launch metadata by digest. `BundleImages` builds from the local base
by create/copy/commit (no pull or rebuild) and verifies labels plus copied-out
bytes before serving. `DeploymentRouter` journals pinned task routing and epochs:
existing tasks keep A, new tasks bind to B during activation, the original task's
continuation stays blocked until the controller seals CP4, failure rolls back to
A, drained state is observable, and an unsealed activation recovers to A.
Receipt: `.agent/deployment-live-20260915-084050.xml`. Task-coordinator/sandbox
launch integration remains.

### 5. Provider access, verifier and gap trigger (fixture level)

`ProviderBroker` forwards only exact Calendar event routes per principal with a
host credential never recorded: A and B share worker access, the target action's
gate is lifecycle-controlled, inserts must carry the frozen deduplication ID with
at most three dispatches per phase, verifier access is read-only, cleanup is
limited to verified events and fixture principals cannot use a live broker. Every
operation is journaled with attribution bindings and redacted body hashes.
`CalendarVerifier` performs complete marker searches, provider-ID reads,
field-level comparison, 1 s/2 s transient read retry yielding unknown on
exhaustion, replay verification and cleanup; `attribute` fails any missing,
pre-activation or ambiguous write. `gap_trigger` accepts only A's emitted
structured report. No Google account, credential, network or calendar write was
used. User-performed authorization, the private calendar identity/time zone and
event inputs, and A's generic gap-report channel remain.
