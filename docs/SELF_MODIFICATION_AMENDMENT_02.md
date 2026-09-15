# Google Calendar preregistration amendment 02: observational timing

Registration ID: `recollect-selfmod-calendar-amendment-02`

Date: 2026-09-12. Prospective user-directed policy change, before any target
baseline, Calendar trial, generated Calendar integration or activation. Local
registration only: the detached SHA-256 establishes byte identity, not an
independent public timestamp.

The user directed: "Let’s not have any time limits for anything related to the
agent working. For the experiment time lets track it but leave it unbounded too
or else we’re guessing and we might accidentally kill a healthy process".
This follows the separate instruction, "No budget/call caps."

## 1. Precedence and immutable history

This amendment supersedes elapsed-time eligibility and termination rules in:

- Original protocol: `2faeba7f1922e7fee58986fec01eda0e95e7e6519a3894625add31ab54f3fc0b`.
- Checkpoints v1: `0fce4f35c4f85bc6f8b1cd5568f9107564ebf009755ca1baeb5d192eecd3e1dd`.
- Amendment 01: `2ac86c7496aee12168cd62026ac17e843bf688398f7a5f441b9d5a5e8635bcb5`.

Those documents, checksum receipts and verbatim planning captures are not edited.
All other requirements remain in force. Every new active runtime and checkpoint
manifest must include this amendment's digest as `timing_amendment`, in addition
to the previous registration digests. Historical manifests without that key
remain inspectable, but cannot authorize an active amended attempt. Freeze the
actual runtime/evaluator/profile identities before CP0; this document does not
certify unfinished implementation or authorize a target run.

## 2. No work deadline and no experiment deadline

Remove the 60-minute experiment cutoff, 30-second responsiveness cutoff, role
timeouts, agent startup/release deadlines, inference/HTTP request timeouts and
development/execution/admission deadlines from the active experiment path.
Provider operations and independent verification likewise have no local
elapsed-time cutoff. Do not replace these with a large finite value, rolling
lease, idle timeout, heartbeat deadline, token quota, call quota or compaction
count limit. Elapsed time or silence alone never makes work failed, runaway,
refreshable, rejected or eligible for retry.

The wire representation explicitly declares unbounded timing (`null`), bound to
the frozen specification and release identity. Missing timing fields or mixed
finite/unbounded declarations fail validation. Finite laboratory fault fixtures
may remain for historical/cleanup qualification, but actual integrated agent
settings must not expose a finite work-time override.

Measure durations without using their magnitude for eligibility. Preserve the
original monotonic start, clock identity, UTC observations and checkpoint order.
Observe the CP5 endpoint only after the independently read-back receipt and
durable endpoint marker required by amendment 01. Record elapsed time through
that endpoint, then retain it in CP6 evidence. A slow readback must extend the
recorded duration, never cause backdating or an elapsed-time failure. CP6 must
account for every failure, retry and unreached checkpoint as before.

Main-chat probes must still execute concurrently, answer correctly and have their
request/response evidence and individual durations recorded. A slow but correct
probe does not fail a time criterion; a missing, incorrect or nonconcurrent probe
cannot count as successful. An unfinished operation remains unfinished, not PASS.
Examples at 3,599, 3,600, 3,601 seconds or later have identical eligibility when
all non-time criteria agree. Neither a restart nor a clock identity/order failure
may reconstruct or reset the attempt into eligibility.

## 3. Termination and recovery are event-driven

Explicit user/controller cancellation, actual process exit/crash, violated
permissions, output/resource overflow, evidence corruption and other registered
non-time failures retain their existing fail-closed handling. Cancellation is
terminal for the primary attempt; it does not earn another primary try.
Refresh still requires proven termination, durable failure accounting, a new
identity and the existing diagnostic lineage rules. Age alone supplies none of
that authority. The original task remains the external definition of done.

Cleanup after an observed terminal event is separate from working time. A trusted
supervisor may arm a short cleanup/capture/report deadline only after observing
completion, failure or cancellation. PID 1 may likewise bound final protocol
drain after observing supervisor exit. These timers cannot start because working
time elapsed or output stopped. Explicit attachment disconnection may terminate
the owned namespace independently of a stalled supervisor. There is no orphan
age limit while an attachment remains apparently open.

Independent host cleanup keeps its existing short timeout after a stop/failure
decision. Cleanup uncertainty cannot produce a usable receipt or refresh grant.
An uncooperative native/HTTP cancellation acknowledgment may remain pending;
retain the busy lease and report unconfirmed settlement rather than claiming a
bounded stop, releasing authority or dispatching more work. HTTP closure is not
proof that upstream GPU inference stopped. Operating system/provider failures can
still occur; a local timer must not manufacture one to trigger a retry.

The existing three candidate submissions, infrastructure-retest allowance and
provider mutation/read retry counts remain unchanged. Provider retry eligibility
requires an actual classified error and the existing deduplication/reconciliation
proof, never slow response alone. Existing retry backoff delays are spacing, not
deadlines; all attempts and uncertainty remain in the evidence.

## 4. Disclosure and prospective qualification

Two non-target one-shot model probes occurred before this amendment. One rejected
a Markdown-fenced plan before container execution; the other admitted a plan but
the forward reviewer requested an already implemented candidate and rejected it.
Neither was a Calendar trial, and neither demonstrated a complete modification
lifecycle. Their failure archives remain intact; no subsequent success can erase
them. Earlier finite-time fixture results are historical qualification, not
evidence for this new policy.

Before a target trial, qualify explicit-null release authentication; healthy work
beyond previous boundaries; long simulated experiment/transport durations;
cancellation and clock-integrity failures without receipt admission; independent
post-terminal cleanup; complete completion-protocol framing; registration binding
and unchanged original hashes. Run independent forward/code review and repository
checks. Scripted Docker/model fixtures are not a real-model capability result.

The current standalone harness uses one-shot structured roles. Native continuous
agent/tool use, compaction, three simultaneous inference lanes, A/B deployment,
provider transport and the original-task evaluator remain separate integration
work. Ordinary application chat/research transports currently retain other finite
timeouts; they must be adapted and frozen under this amendment before they can
participate in the experiment. No claim of application-wide unbounded execution
or experiment readiness is made here.
