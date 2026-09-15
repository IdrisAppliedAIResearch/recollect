# Isolated structured role runners

Implementation log, not an experiment result or preregistration amendment.

`IntegratedDevelopment.run_role` now owns one-shot, host-brokered model roles
with independent container execution. The shared model server performs inference;
the trusted host constructs and records the request. A fresh networkless container
then consumes the frozen response as data. This is not an OpenCode process, an
interactive shell/tool loop, or three-lane scheduling.

## Iterative lifecycle driver (2026-09-14)

`IntegratedDevelopment.run_until_ready` now drives one fresh development cycle
through the existing authenticated role runner. It reads controller-owned stage
and review events, not an assumed sequence of approvals. Forward-review rejection
returns to planning; a failed check returns to implementation; code-review rejection
returns to implementation and requires fresh checks/review. There are no iteration,
call, token or elapsed-time quotas. This method stops at verified development
readiness: it does not submit CP2, activate B, or claim the original task is done.

One opaque host owner holds the entire lifecycle, including between roles and
through cancellation/failure accounting. Factories are trusted constructors that
must not dispatch work themselves. Worker execution still belongs exclusively to
the qualified executor. Competing grants/driver claims fail closed and abort the
primary attempt; they cannot release its owner's claim. Controller accounting and
close remain blocked while the lifecycle is pending. Eligibility is checked
atomically with final ownership release, so an intervening abort cannot return
successful readiness. Exceptions and cancellations are never automatic retries.
Fresh-cycle one-use ownership is replay prevention, not a limit on agent revisions.

Before implementation, context contains an empty candidate and null candidate
digest, with baseline source separately labeled. The forward-review prompt
explicitly asks for prospective assessment, not evidence of already implemented
work. After implementation, candidate bytes and digest identify the actual source.
Original contract, current plan and complete existing plan/review/check history
are rebuilt from controller-owned state and retained evidence. No history is
summarized away or truncated to make progress; existing byte bounds still fail
closed. The new profile identity includes the revised prompt bytes.

This closes rejection/revision sequencing in the structured-role prerequisite.
It is **not** native OpenCode tool use, compaction, or a persistent autonomous
coding session. The automatic driver reimplements within the reviewed plan after
code-review rejection; agent-selected replanning/disputes and native context
management remain integration work. The prior failed real-model probes are
unchanged historical observations, not retroactively passing results.

Fresh-context forward/code review approved this scope after fixing and testing
a readiness-delivery revocation race. Qualification uses scripted model responses
and fake observations or real isolated containers; no Calendar or third local-model
probe is included. Final test results are recorded in the harness implementation log.

## Authority and lifecycle

The caller is trusted host code and retains the existing opaque role grant.
Workers never receive controller objects, minting methods or submission authority.
The host consumes the original grant and records its action count before
inference. Counts are audit data, not quotas. A busy lease remains held through
inference, process execution, receipt
readback, development transition, cleanup and final async delivery. There is no
worker-facing receipt-import endpoint and no second grant minted for admission.

Each invocation gets a fresh request ID and container, bound to actor, stage,
generation, controller instance, cycle and development revision. The native Docker
attachment belongs to the host-created, independently attested container. Its
supervisor report and owned executor journal authenticate the process result;
actor IDs printed by a model are not credentials. Reviewers have separate assigned
actors and fresh bounded input, not the author's chat transcript.

The complete profile is captured before the first invocation: immutable driver
bytes and prompt tuples, loopback endpoint/model, observational timing policy and
exact check-name/script mapping. Its digest is frozen across controller cycles.
The future experiment runtime manifest must pin this profile before CP0; this
implementation does not replace that prospective freeze.

## Roles

| Action | Model output | Isolated execution and gate |
| --- | --- | --- |
| Plan | Structured changes and requirement-linked verification | Read-only source; host validates every change against the original contract/policy |
| Forward review | Approval, rationale, structured findings | Read-only source; distinct reviewer; unresolved blocking findings prevent implementation |
| Implement | Full UTF-8 replacements for exactly the reviewed paths | Driver writes only provisioned source paths; host reconstructs against original baseline and reviewed plan |
| Checks | No model call | Frozen scripts run against read-only candidate source; driver reports actual child exit codes and bounded logs |
| Code review | Approval, rationale, structured findings | Read-only complete candidate, original baseline/plan and prior check diagnostics; separate reviewer |
| Submit | No model call | Existing deterministic submission gate seals CP2 |

Modifications always start from the original baseline, including revisions of files
created in an earlier candidate. The current candidate is supplied as context, not
trusted restoration bytes. Deletion is unsupported by this containment profile.
Dependencies are not installed or resolved by these runners. There is no shell,
network, repository mount, provider credential or Docker socket inside the role.

Findings have stable IDs, blocking/advisory severity, open/resolved status, a
requirement, path, original issue and separate resolution text. Existing IDs cannot
change targets or original issues; open blockers cannot disappear or be downgraded.
Historical paths remain valid for resolving findings about removed planned work.
The reviewer—not the author—supplies the disposition. This enforces an auditable
process, not the correctness of an LLM's judgment.

## Result separation and evidence

Driver/context/reply/check files are an execution envelope, not the candidate.
The envelope's fixture baseline digest necessarily differs from the original
source digest. The host keeps the original executor receipt unchanged, records
the envelope → original development binding → extracted source mapping, and runs
the existing exact candidate reconstruction before accepting implementation bytes.
Planning, reviewer and checker envelopes grant no source writes.

Check scripts execute in child processes, never inside the reporting interpreter.
Their stdout/stderr go to bounded temporary files, not its report channel. The
reporting interpreter sets and verifies Linux non-dumpable status, preventing
same-UID children from opening its `/proc` descriptors or modifying its memory.
There is no shared writable report file. The root supervisor separately captures
the driver's stdout and verifies exit, capture and descendant quiescence.

An ordinary nonzero check exit is reported as a failed development check and can
lead to revision. A driver crash, malformed result, overflow or uncertain
termination is terminal. Frozen check exit status is not proof of arbitrary
candidate semantics: trusted check authors must keep external acceptance checks
outside candidate-controlled execution. In particular, importing hostile code
into a unit-test interpreter can subvert that unit test. Independent evaluation
and the original external task remain required for experiment success.

`development_role` records contain frozen request/context, bounded model request
and response bytes, profile identity, envelope/source mapping and the owned
executor archive. CP2 and CP6 copy these attached bytes, including failure before
container creation. Subsequent roles receive the original task and source, reviewed
plan, current candidate and recorded review/check reports. They cannot redefine
the acceptance contract. Context growth fails closed rather than silently dropping
findings or diagnostics.

## Limits and cancellation

- The current one-shot transport makes one model request per
  plan/implementation/review invocation; no model calls for checks or submission.
  There is no harness token, model-call, development-update or review-count quota.
  Requests omit `max_tokens` and `max_completion_tokens`, leaving output length
  to the server defaults. Call/update/review counts and nonnegative reported usage
  remain audit data only; malformed usage and truncated replies still fail closed.
  Single-use grants/brokers prevent replay, not further authorized invocations.
  Interrupted requests are not automatically retried.
- Model HTTP is restricted to an explicit `http://127.0.0.1:<port>/v1` chat endpoint.
  Redirects, environment proxies and compressed responses are rejected. Raw bytes
  are read in bounded chunks; model request and response each cap at 128 KiB.
- Role context is capped at 128 KiB; original containment limits still apply to
  the entire envelope, including its driver, source and checks. Unsupported inputs
  and reserved response space are checked before inference.
- Agent work is unbounded from inference through startup, execution and admission.
  The frozen policy is `observational`; model HTTP has no local request timeout.
  Independent cleanup is bounded only after an observed terminal event, never
  because healthy work is old or quiet. Native process creation and host filesystem
  I/O have no hard real-time guarantee.
- Check logs have a combined 32 KiB raw limit, leaving space for base64/framing in
  the 64 KiB role-report channel. Overflow is explicit failure, not silent passing
  truncation.

Model inference runs in an owned task. Caller cancellation reaches it once;
subsequent cancellation cannot bypass local settlement or release the busy lease.
Evidence distinguishes local request settlement/closure from upstream generator
termination. An HTTP disconnect does not prove GPU quiescence; interrupted or
uncertain requests permanently fail the primary path and are never retried here.
Local closure is conservatively unconfirmed on interrupted/error teardown.
An uncooperative cancellation acknowledgment can remain pending indefinitely;
the busy lease stays held and no receipt/refresh/new dispatch is authorized.
This is not yet a model-server cancellation/reconciliation mechanism.

## No-quota decision

After the initial runner qualification, the user explicitly directed:
"No budget/call caps." This supersedes the earlier implementation choice of
token/model-call and development-update/review-count quotas, including proposals
in the immutable planning transcript. Removed settings are not accepted as silent
no-ops and counters no longer decide eligibility. The historical transcript,
registration files and failed probe archives are preserved unchanged.

At that stage the time cutoffs remained. The subsequent user-directed
[timing amendment](SELF_MODIFICATION_AMENDMENT_02.md) now removes agent-work and
experiment elapsed-time cutoffs. Three candidate submissions, infrastructure
retest and provider retry counts remain. Permissions, review, cancellation and
containment/byte protections are unchanged. Growing context/archive size still
bounds this fixture-oriented transport. It must not be represented as a
continuous default-agent implementation: native tool use,
compaction and durable stage-appropriate context remain future integration work.
The resulting runtime profile must be frozen prospectively before the target trial.

Independent forward/code review cleared this narrow change. A review-identified
test gap was fixed: cumulative action counts are checked immediately across cycle
reopening, not just after later operations. Final post-review validation reported
`All checks passed!` from Ruff and
`1707 passed, 2 skipped, 2 warnings in 298.38s (0:04:58)` from full pytest with both
Docker opt-ins. All 40 live Docker cases passed. The skips are the existing Windows
case and the separately gated real-model probe; no further model probe was run.
Evidence is retained under `.agent/no-call-caps-focused`, `no-call-caps-full`,
`no-call-caps-final` and their XML reports. Test-owned containers/external inputs
were cleaned; existing services stayed healthy and all five immutable hashes
remained unchanged. No service/configuration, target, commit or push changes.

## Remaining work

The host controller still records simulation outcomes. Real baseline collection,
automatic capability-gap triggering, three inference lanes, immutable A/B
deployment/rollback, Calendar credential transport, independent provider evaluation
and final runtime-manifest publication remain separate work. No target rehearsal
or generated Calendar integration is included. The immutable preregistration and
verbatim planning capture are unchanged.

## Qualification status

Independent forward/code review and focused rereview cleared the bounded runner
implementation after fixes for cancellation settlement, compressed-response
bounds, captured driver/prompt identity, Unicode reports, log framing and stable
finding reconciliation. A release-cancellation test now holds a barrier so the
worker cannot race past the observation before cancellation is delivered.

Two non-target local-model probes on 2026-09-12 did **not** complete the lifecycle:

1. The initial plan used Markdown fences. Strict JSON parsing rejected it before
   any container launch. The frozen prompts were clarified to require raw JSON;
   parsing was not relaxed and a fence-rejection regression was added.
2. The next attempt admitted a real model plan and ran a separate forward-review
   container. The reviewer rejected the plan because the unchanged baseline did
   not yet contain the implementation. The probe's fixed next action was then
   refused by the development-stage gate. No implementation or submission occurred.

The second observation exposes stage/context ambiguity in this one-shot profile
and a probe that assumes first-pass review approval. It is not a containment
failure or proof of successful real-model modification. No review was overridden,
no third model attempt was made, and no target Calendar task was rehearsed.
Stage-appropriate reviewer context and continuous revision orchestration need a
follow-up decision before further model qualification. Preserved evidence is in
`.agent/roles-model-probe.xml`, `.agent/roles-model-verified.xml` and their matching
archive directories; these failures remain part of the development record.

Final regression validation on 2026-09-12 used both Docker opt-ins, leaving the
separate real-model opt-in off after the two recorded failures. Ruff reported
`All checks passed!`; full pytest reported
`1705 passed, 2 skipped, 2 warnings in 295.78s (0:04:55)`.
All 47 new offline role tests and 11 new scripted-inference/live-container cases
passed, including the corrected cancellation barrier. All 40 live Docker cases
passed across the suite. The skips are the existing Windows case and the
separately gated real-model probe; this regression result does not erase that
probe's failures or establish real-model end-to-end qualification.

Ignored `.agent/roles-*` archives and XML reports are intentionally retained,
including the final `.agent/roles-final-full.xml`. Test-owned containers and
external fixture directories were cleaned. Existing Recollect/Qwen services were
left running without configuration changes. All three preregistration hashes and
both planning-capture hashes remained unchanged. No UI, Calendar, deployment,
commit or push was performed.
