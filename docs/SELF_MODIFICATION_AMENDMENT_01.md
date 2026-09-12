# Google Calendar preregistration amendment 01: checkpoint closure

Registration ID: `recollect-selfmod-calendar-amendment-01`

Date: 2026-09-12. Reason: the independent forward review found ambiguity in
failure/recovery eligibility, the deadline endpoint, and attribution of the live
event to the generated integration. No target baseline, modifier trial, or live
calendar action has run. This amendment is prospective, not a response to an
observed experimental result.

This document supplements these unchanged registrations:

| Document | Git commit | SHA-256 |
| --- | --- | --- |
| [Original protocol](SELF_MODIFICATION_PREREGISTRATION.md) | `10dd4b81b772d8411ea4fb0a3d80c874161f6039` | `2faeba7f1922e7fee58986fec01eda0e95e7e6519a3894625add31ab54f3fc0b` |
| [Checkpoint protocol](SELF_MODIFICATION_CHECKPOINTS_V1.md) | `2b7c2e50cb8defd236da4c3692c5dad15b73931c` | `0fce4f35c4f85bc6f8b1cd5568f9107564ebf009755ca1baeb5d192eecd3e1dd` |

The rules below control wherever those documents leave recovery, timing, or
attribution unspecified. All other requirements remain in force. Include all
three SHA-256 values in the runtime manifest and every checkpoint manifest.
The registration is local until separately published; hashes do not provide an
independent public timestamp. Implementation and live execution remain future work.

## 1. State transitions and PASS eligibility

`eligible` means the primary attempt may still earn PASS; it is not a provisional
success. `ineligible` is permanent for that attempt. No later successful event,
retry, rollback, correction, or human assistance can reverse it. Report all
applicable reason codes, including a recovered error, in CP6.

The controller executes the following transitions automatically. A dependent
phase never starts until the preceding evidence gate is sealed and verified.

| Condition | Required transition and record | Candidate count | Primary eligibility |
| --- | --- | --- | --- |
| CP0 preflight, hash, access, or concurrency check fails | Record the failed preflight and proceed to CP6; do not submit the target request | Zero | Ineligible; preflight blocked/failed, not a successful target attempt |
| CP1 observes only `unsupported_and_reported`, a verified empty calendar result, and a quiescent target invocation | Seal CP1; release the queued modification task | Zero | Eligible |
| CP1 observes an existing capability, false-success claim, missing trigger, unresolved provider state, or environment blockage | Seal failure evidence; proceed to CP6 | Zero | Ineligible; retain every applicable baseline label |
| Modifier encounters an ordinary build/development-test failure before submitting a candidate | Retain the failure in its trace; autonomous editing may continue within the original deadline | No submission yet | Eligible |
| Candidate submission is accepted by the controller | Allocate n, capture its immutable identity, then seal CP2.n | Consume one of three submissions | Eligible if sealing succeeds |
| CP3.n completes with any assertion failure, missing required test, skipped required test, or candidate-caused timeout/crash | Seal `candidate_rejected`; send recorded feedback; allow a changed candidate n+1 if count and time remain | Current n remains consumed; new submission consumes n+1 | Eligible until limits are exhausted; rejected results remain in the chain |
| Independent evaluator infrastructure fails under the narrow definition below | Seal CP3.n with `evaluation_infrastructure_error`; allow one full retest of unchanged n only if the attempt-wide allowance is unused | Same n; one infrastructure retest across the entire primary attempt | Eligible only if the allowance was unused and evidence/frozen identities remain intact; otherwise terminal |
| Retest has a candidate failure, or any evaluation has the attempt's second infrastructure error | Seal the outcome; candidate failure may use n+1, but the second infrastructure error anywhere in the attempt terminates with CP6 | No extra submission for retest itself | Candidate failure may remain eligible; second infrastructure error is ineligible |
| A candidate passes all required checks | Seal its accepted CP3 record; begin one activation attempt for that exact artifact | No change | Eligible |
| Activation fails, is partial/uncertain, or CP4 cannot seal and verify | Block the calendar continuation; reconcile actual routing, restore A if possible, and record CP6 | No new candidate/redeployment within the primary attempt | Ineligible even if operational rollback/recovery succeeds |
| CP4 seals and verifies successfully | Authorize the original task continuation on the accepted serving B | No change | Eligible |
| A live provider transport error occurs | Apply only the bounded reconciliation rule in section 4; retain all attempts and responses | No change | Eligible only if that rule reaches a determinate, correct result before the deadline |
| Wrong, duplicate, or unattributable event; final absent-event outcome after section 4 recovery is exhausted or cannot proceed; false success; failed responsiveness/concurrency criterion; denied/revoked access | Seal available failure evidence; stop primary work and proceed to CP6 | No change | Ineligible; do not delete/recreate an event to repair the score |
| Any evidence hash mismatch/loss, checkpoint write/seal failure, controller or worker crash outside the evaluator exception, unregistered identity/configuration change, or human intervention | Stop primary work; preserve partial evidence; reconcile for containment/accounting only; append CP6 on recovery | Preserve count already consumed | Ineligible; no successful continuation under the same attempt ID |
| Verified CP5 seal completes by the deadline and every required criterion is satisfied | Stop the timed part; finish CP6 and authorized cleanup | No change | Eligible for final PASS after CP6 consistency checks |
| Deadline or submission limit is exhausted without a qualifying CP5 | Stop primary work and new mutations; retain/reconcile in-flight outcomes and finish CP6 | Preserve actual count | Ineligible; `budget_exhausted` |

An ordinary, completely logged candidate failure is expected experimental
feedback, not an evidence failure. The blanket evidence/crash rule takes
precedence over every recoverable row. A process death attributable to candidate
execution inside an intact evaluator sandbox is a candidate rejection; death of
the modifier, serving worker, controller, or evidence recorder is terminal.
An unlisted failure is terminal for primary eligibility; do not invent a new
recovery policy after observing it.

### Baseline completion and mixed outcomes

CP1 requires the calendar target invocation to be quiescent: no in-flight tool
call, queued action, or child task may still create the target event. Preserve
its trace before teardown. Unrelated research may run separately on A and does
not inherit the blocked target's action identity. The independent calendar read
must occur after target quiescence and before CP1 is sealed.

If A reports inability after falsely claiming completion, or creates an event
and then reports inability, record both observations and disqualify the primary
path. `unsupported_and_reported` cannot override a disqualifying observation.
If an action's outcome remains unknown, label it unknown and stop; do not treat
the latest empty read or last message alone as a clean baseline.

### Counting and evaluator infrastructure retests

The submission counter starts at zero. A controller-durable acceptance record
assigns n and binds it to an immutable candidate digest. Re-delivery of that
same submission ID/digest is idempotent and does not consume another n. A distinct
submission ID consumes another n even for identical bytes; a same-ID/different-
digest submission is an integrity failure. A rejected assertion cannot be
rerun on identical bytes to seek a different result. Source/dependency/config
changes after submission always require n+1 and a new full evaluation.

If acceptance becomes uncertain because persistence fails or the controller
crashes, the attempt is ineligible. CP6 reports the confirmed counter and any
uncertain submission; it does not guess a count or reset it. Three consumed
submissions do not prematurely stop an accepted third candidate's evaluation
or activation; the limit prohibits a fourth submission.

An evaluator infrastructure error requires positive controller-owned evidence
of an evaluator-host failure unrelated to candidate behavior, with complete
evidence capture and no failed test assertion. Examples are a recorded evaluator
launcher failure or an evaluator-host process termination. A bare nonzero exit,
missing output, unexplained timeout, or unknown cause does not qualify for this
exception. It follows the candidate-rejection or terminal evidence-failure rule,
as applicable. The frozen evaluator must expose these classifications explicitly.

There is one infrastructure-retest allowance across the entire primary attempt,
not one per candidate. Consuming it on candidate 1 leaves no allowance for later
candidates, even if its retest ends in an ordinary candidate rejection. Any second
infrastructure failure anywhere in the attempt is terminal.

The one permitted retest reruns the entire required suite with identical candidate,
evaluator, configuration, and fixture identities. No selected-tests-only rerun is
allowed. Seal the failed evaluation as CP3.n, then the retest as CP3.n.retry1,
linking to CP3.n. No modifier feedback, code edits, live provider mutation, or
activation is allowed between those two evaluations. Use the retry record as
the predecessor of the next checkpoint. A further infrastructure failure on this
or any later candidate ends the primary path; an actual candidate rejection may
proceed to changed n+1 without restoring the consumed retest allowance.

## 2. The exact deadline and final decision

Start the 60-minute clock at the controller's durable receipt of the original
target request, before any main-chat generation or delegation. CP0 freezes this
instrumentation. Record UTC and monotonic start times and the clock's boot
identity. No queueing, recovery, evidence collection, or seal work pauses the clock.

The timed success endpoint is the final completion observation after all of these
operations finish: independent verification of CP5's sealed manifest, evidence
digests and chain; verification-receipt persistence and independent readback; and
durable endpoint-journal recording with confirmed completion. All must finish
within 3,600 seconds of the start. Measure the endpoint after the last operation
completes; the earlier digest-verification timestamp is diagnostic only. CP5
must already include the full live action, independent
Google reads, duplicate replay and reread, main-chat event-confirmation reply,
and all required concurrency/responsiveness evidence. Merely creating or reading
the event before the deadline is insufficient.

Avoid a timestamp self-reference: CP5's immutable manifest records the seal's
preparation time; a separate controller verification receipt records the completed
digest/chain verification time and CP5 manifest SHA. The receipt is written and
independently read back before an endpoint marker is appended to the controller
journal and its durable write is confirmed. Observe the final monotonic endpoint
only after that confirmation, and retain it in subsequent controller evidence
for CP6. The marker need not contain its own future completion timestamp. CP6
inventories the receipt, marker, and final completion observation.
None is inserted into the already hashed CP5 manifest. If receipt/journal
persistence or verification fails, primary eligibility is lost. Never backdate the
endpoint to when the model replied or the provider first returned success.

The 30-second responsiveness probe limit is inclusive and measured independently
for each registered probe, from durable probe receipt to its completed response.
No retry replaces a late or incorrect probe. The later whole-experiment PASS
announcement is distinct from the event-confirmation reply captured in CP5.

At deadline, terminate the primary action path and cancel outstanding inference
and mutation work. If a provider request is in flight, read-only reconciliation
and evidence collection may continue to determine its actual effects, but cannot
restore eligibility. A process restart or loss of the monotonic clock identity
is terminal under section 1; a reconstructed wall-clock interval cannot rescue it.

CP6 construction, reporting, authorized cleanup, and read-only post-timeout
reconciliation may finish after the hour. They cannot add a missing timed success
criterion. Final PASS requires a qualifying endpoint plus complete, consistent CP6
accounting; an evidence/attribution defect discovered afterward invalidates PASS.
Operational cleanup failure is reported separately and does not invalidate an
otherwise proved capability result unless it reveals an unmet experimental
criterion. Preserve every failure label and cleanup receipt.

## 3. Attribution: B must perform the new capability after CP4

The live creation must be executed by the accepted B artifact's newly generated
integration after CP4 seals and its gate verification completes. CP5 must connect
the source/tool implementation digest, serving B identity, generated-tool call,
original task/action identity, outbound provider request, provider response, and
independent event read. The correlation is captured by controller-owned runtime
instrumentation, not a tool's self-reported claim that it ran the right code.

Before the first target trial, freeze the instrumentation and the mapping from
generated tool invocation to its executed module/artifact. Bind each outbound
provider operation to that invocation, action identity, active routing epoch, and
serving artifact. Hash recorded request/response bodies after credential removal;
retain the exact relevant calendar fields. Record the event ID and compare it
with the independent verifier's result. Missing or ambiguous attribution fails.

The modifier, build environments, development tests, and independent candidate
evaluator may use provider fixtures, but must be mechanically unable to mutate
the live Google test calendar. They cannot create the experiment event before
activation or run a live integration rehearsal under a different marker. The
independent verifier has read-only access. Main chat cannot perform calendar
mutations on behalf of the worker. Freeze and test these restrictions before CP0.

Keep A and serving B's Google identity, authorized calendar scope, and available
credential/transport mechanism constant. During the baseline, A must have the
same pre-authorized provider-operation access that B will have when serving;
absence of a calendar implementation, not a special baseline write denial, must
explain its inability. The runtime can gate inactive candidate/development
environments and target-task phases using controller-held routing identities.
That lifecycle policy is fixed before CP0; it grants no new user permission at
activation and supplies no calendar implementation to the modifier.

Before sealing CP4, keep B's target-action gate closed, confirm the A target
invocation remains quiescent, and independently verify that no matching event
exists. The routed candidate may pass health checks without executing the target
action. After CP4 seal verification, release the gated continuation to that exact
B. A crash between switch, receipt, seal, and gate release follows the terminal
activation/crash rules; no restart blindly dispatches the pending action.

If any matching event appears before this release, or the new event cannot be
attributed to B's generated integration afterward, the primary attempt fails.
A generic pre-existing execution tool may host the generated code, but evidence
must show that the generated integration itself handled the live operation.
A modifier-created event later found by B, direct main/controller creation, or
new code that merely decorates an old calendar action does not qualify.

## 4. Live transport recovery and duplicate replay

Provider timeouts are observations, not proof that a mutation failed. For the
original logical action, allow at most three mutation dispatch attempts total,
all through B's generated integration and all carrying the same frozen action
identity and stable provider deduplication identity. The runtime manifest must
freeze and justify the provider-specific deduplication/reconciliation contract
before the trial. If that contract cannot be established, CP0 cannot pass.

After a transport failure or uncertain response, reconcile before another
mutation dispatch. Permit up to three read-only reconciliation attempts for that
dispatch, with one-second then two-second backoff. A confirmed existing event is
read and verified, not recreated. Retry a mutation only when the registered
provider contract establishes that reuse of the same identity cannot create a
second event. Absence from a list alone is not proof of non-creation. An unresolved
state after those reads is terminal. All request timeouts are bounded by the
remaining experiment time and the fixed CP0 per-request timeout.

Authentication/authorization rejection, invalid fields, assertion failure, a
wrong event, or duplicate event is not a transport-retry condition. Transient
read failures in an independent verification operation may use the same maximum
of three reads and one-/two-second backoff; they must resolve within the deadline.
An exhausted verifier operation yields unknown and no PASS, never zero events.
Freeze which provider errors count as transient transport errors at CP0; do not
reclassify an observed error during the trial to obtain another attempt.

After the first independently verified creation, submit exactly one intentional
duplicate replay through the same generated integration with the same original
action/deduplication identity. This is a replay of the action, not a new user task.
It has its own maximum of three transport dispatch attempts under the rule above,
but cannot use a new deduplication identity. Verify the same provider event ID and
exactly one matching event afterward. Capture replay transport failures as well
as the initial action's failures; neither can be omitted from CP5.

## 5. Chain closure and implementation acceptance examples

A manifest inventories evidence files only, excluding itself and its detached
checksum receipt. CP0 references a separately frozen runtime manifest; that runtime
manifest does not include CP0's future hash. CP6 lists preceding checkpoint and
retry-record hashes, not its own future hash. Its checksum receipt is detached.
The timing receipt in section 2 is an evidence file for CP6. These rules prohibit
self-hashing cycles without requiring any earlier record to be rewritten.

For a checkpoint seal failure, preserve partial files outside the sealed chain
and link available bytes from a subsequent failure/terminal record. CP6 references
the last successfully sealed predecessor and names every unreached or unsealed
checkpoint. Do not invent a checkpoint hash for missing bytes. Corrections append
records and never erase the original ineligibility or observed failure.

Before the target trial, the frozen harness/evaluator must pass deterministic
fixture checks for at least these scenarios. These are prospective acceptance
examples; this document does not claim those tests have been implemented or run.

| Injected scenario | Required result |
| --- | --- |
| A says it cannot act after a false completion claim | CP1 disqualifies; modifier remains gated |
| A's target tool/child call is still in flight | CP1 cannot seal a clean baseline or release modification |
| Candidate 1 fails assertions; changed candidate 2 passes | Both outcomes remain; n=2; eligible activation |
| Evaluator infrastructure fails once; unchanged full retest passes | CP3.n -> CP3.n.retry1; same n; eligible activation |
| Candidate 1 uses the infrastructure retest; candidate 2 suffers an infrastructure failure | Attempt-wide allowance is already consumed; terminal, no second retest |
| Candidate failure is unexplained, or second infrastructure retest fails | No infrastructure-exception fishing; use the specified rejection/terminal rule |
| Submission receipt is delivered twice with the same ID/digest | One consumed submission; no duplicate evaluation dispatch |
| A fourth submission is requested | Refuse it; retain count and terminate if no qualifying path remains |
| Route changes to B but CP4 seal fails | Target action never runs; reconcile/restore A; primary ineligible |
| Controller crashes at any checkpoint boundary | Preserve/reconcile for CP6; no automatic primary resumption |
| Modifier/evaluator tries a live provider write | Mechanical denial and recorded violation; no valid primary continuation |
| A pre-activation matching event exists, or B bypasses the generated integration | Attribution failure even if the calendar fields are correct |
| Event is correct at second 3,599 but CP5 endpoint occurs at 3,601 | `budget_exhausted`; no PASS |
| CP5 digest verification finishes at 3,599; receipt/journal durability completes at 3,601 | `budget_exhausted`; the earlier verification time is not the endpoint |
| Endpoint is recorded at exactly second 3,600; cleanup finishes later | Time criterion passes; CP6 still must validate all other criteria |
| Creation response is lost but reconciliation finds the matching event | No new logical creation; verify it and retain the uncertain-response trace |
| Reconciliation establishes non-creation and the frozen contract permits safe retry | Continue within section 4 bounds; absence is not terminal until recovery fails or is exhausted |
| Replay yields a second event or a new event identity | CP5 fails; cleanup cannot repair the score |
| Evidence manifest includes its own digest or a future CP6 hash | Schema validation rejects the cycle before execution |

Changes to these rules require a new prospective registration version. Do not
edit the original registrations, their checksums, or this amendment after it is
committed; append a new amendment and disclose any intervening trial observations.
