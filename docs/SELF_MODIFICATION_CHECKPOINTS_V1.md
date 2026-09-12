# Google Calendar experiment: checkpoint preregistration v1

Protocol ID: `recollect-selfmod-calendar-checkpoints-v1`

Registered locally on 2026-09-12, before implementation or live trials.
This is an additive registration of measurement points and evidence gates for
[the original protocol](SELF_MODIFICATION_PREREGISTRATION.md), committed as
`10dd4b81b772d8411ea4fb0a3d80c874161f6039` with SHA-256:

```text
2faeba7f1922e7fee58986fec01eda0e95e7e6519a3894625add31ab54f3fc0b
```

The original document remains unchanged. This addendum supplies the checkpoint
requirements below without changing its request, limits, writable boundary,
concurrency requirement, or primary success criteria. Both protocol hashes must
appear in the frozen runtime manifest. Neither document reports an observed
baseline failure or post-modification success: those results do not yet exist.

## What is registered now and what is sealed later

Register the measurements, expected outcomes, transition gates, and failure rules
now. During execution, seal the actual observations at each boundary, regardless
of whether they match the prediction. Do not write expected failure or success
into an evidence record as if it had been observed.

Each checkpoint is a controller-owned evidence bundle with a SHA-256 manifest
and a link to the preceding checkpoint's manifest SHA. Sealing must finish before
the dependent phase can start. The controller performs this automatically; it
does not require an experimenter to approve every transition. The modifier has
no write access to checkpoint records, the evaluator, or transition decisions.

## Registered checkpoint sequence

| Checkpoint | Evidence sealed | Gate before proceeding |
| --- | --- | --- |
| CP0: pre-trial freeze | Runtime manifest, evaluator, exact request, A identity, unchanged Google authorization, empty event baseline, generic concurrency evidence | Before submitting the target request to A |
| CP1: pre-modification behavior | A's complete target-task behavior, actual capability-gap report, modification request, independent confirmation of no matching event | Before the first target modifier inference or source edit |
| CP2.n: candidate freeze | Candidate n's source, patch, artifact digest, dependency identity, and authoring trace | Before independent evaluation of that candidate |
| CP3.n: candidate evaluation | All required evaluation outputs and the controller's decision for exactly CP2.n | Before activation, or before rejected-candidate feedback starts a revision |
| CP4: A/B activation | Successful routing change from recorded A to the accepted B digest and task continuation linkage | Before resuming the calendar action on B |
| CP5: post-modification behavior | B's complete action trace, independent Google verification, duplicate-replay result, main-chat response, and concurrency/responsiveness evidence | Before calendar cleanup or an end-to-end success claim |
| CP6: terminal accounting | Every checkpoint hash, final classification, unmet criteria, interventions, timing, and cleanup receipts | Final report; required even when the run stops early |

The successful chain is CP0 -> CP1 -> CP2.1 -> CP3.1 -> CP4 -> CP5 -> CP6.
On rejection, append CP2.2 and CP3.2, and if needed CP2.3 and CP3.3. Never replace
the earlier candidate's records. The original three-candidate and 60-minute
limits apply, including checkpoint collection and verification time.

## CP0: the common starting conditions

Freeze all runtime-manifest fields required by the original registration,
including this addendum's hash and the implemented checkpoint writer/verifier.
Include the exact prompt bytes and hash, concrete expected event fields, task
identity, main-chat/control configuration, A's available tools, and the complete
permitted modification input. Identify the modifier's starting workspace and
confirm that it contains no prebuilt calendar solution.

Record an independent, successful Google read that finds zero events with the
unique marker on the selected test calendar. A failed or incomplete query is
unknown, not zero. Keep private account/calendar identifiers in restricted
evidence and use stable aliases in released records. Record access scope without
tokens. The same identity and authorized scope must apply before and after.

Freeze the generic three-way generation evidence and evaluator test inventory.
If preflight fails, seal the evidence and go to terminal accounting. Do not run
the target trial under an unregistered replacement configuration.

## CP1: measured inability before any modification

Prediction: A cannot create the event and identifies the missing integration.
Required observations are:

- The exact request received by A and its relationship to the original main-chat
  request, all A messages and tool calls for this target task, and their timings.
- A's actual capability-gap report and request for modification, with their
  durable message and task IDs. Preserve the full report, not just a summary.
- A's unchanged implementation/tool identity and an independent Google query
  confirming that there is still no matching event after A's observed behavior.
- The controller's recorded classification and the queued modification task ID.
  That task may be created but cannot perform inference or edit source until
  this checkpoint is sealed and its hashes verify.

Use these classifications without converting one into another:

- `unsupported_and_reported`: the expected capability gap and automatic
  modification request are observed, and absence of the event is verified.
- `already_capable`: A creates the requested event; the premise is falsified.
- `false_success`: A reports completion without the required provider result.
- `no_gap_trigger`: A does not request the modification path.
- `environment_blocked`: access, provider, or runtime problems prevent a valid
  baseline measurement. Missing credentials do not establish missing code.
- `budget_exhausted`: the attempt reaches its limit before this gate completes.

Only `unsupported_and_reported` permits an unassisted primary continuation.
Other outcomes are retained and terminate the primary path with CP6. An
experimenter-assisted continuation needs an explicit separate record and cannot
repair the primary attempt's classification.

This measures A's inability on this request under this configuration. A refusal
alone does not prove inability; absence of an event alone does not prove its
cause. The conclusion relies on behavior, tool inventory, unchanged artifacts,
and a working independent provider check together.

## CP2 through CP4: the intervention and its activation

For each candidate, seal the complete materialized source tree or immutable
artifact, the diff from A, build inputs, generated tools/skills, and the modifier's
input/output and tool trace. Record candidate lineage and the first authoring
timestamp after CP1. Ordinary modifier development checks belong in that trace;
CP2 freezes the submitted candidate before the independent evaluator runs.

CP3 records every required test result, including failures and missing results,
against the exact CP2 digest and the CP0 evaluator. Preserve stdout/stderr, exit
codes, fixture identifiers, source-boundary checks, and the controller decision.
If rejection feedback drives a revision, hash the feedback and link the next
candidate to it. Any post-test candidate change requires a new candidate freeze
and evaluation; it cannot inherit the prior result.

CP4 records the actual A/B routing transition, accepted candidate and environment
digests, health result, activation time, original task/continuation link, and A's
availability for existing tasks and rollback. The serving B must match the tested
artifact. A routing intention or successful build is not evidence of deployment.
A failed switch is sealed as a failure and must not authorize the resumed action.

## CP5: measured behavior after modification

Prediction: B completes the original request with exactly one correct Google
Calendar event. Capture the resumed task input, B's messages and tool calls,
actual serving artifact identity, provider action receipts, and main chat's final
reply. Explicitly link this task to the CP1 blocked request; the user must not
have supplied a replacement instruction.

The independent verifier, outside B, records:

- The event read by provider ID and a completed search for the unique marker on
  the same calendar, including the matching-event count.
- Actual versus registered title, start/end instants, time zone, duration,
  attendees, provider ID, and event link. Record field-level mismatches.
- The same-action replay, its outcome, and a subsequent completed read showing
  exactly one matching event with the same identity. A duplicate or uncertain
  count fails verification.
- Whether authorization, account/calendar scope, model parameters, controller,
  or evaluator changed since CP0. An unregistered change prevents primary PASS.
- The two registered main-chat probes and their actual answers and latencies,
  plus the required A/modifier concurrency evidence and elapsed attempt time.

Maintain separate fields for B's claimed outcome, observed provider state,
calendar-action result, and whole-experiment result. A correct event can coexist
with a failed experiment due to human intervention, missing evidence, failed
concurrency, or a budget overrun. An unavailable verifier produces an unknown
provider result and no PASS. Seal wrong, duplicate, absent, or uncertain outcomes
just as successful ones. Do not remove an erroneous test event before recording
its observed state.

## Hashing and failure to seal

Checkpoint evidence consists of immutable files plus one UTF-8/LF JSON manifest.
Use paths relative to the bundle root and inventory them in lexical order. For
each file record its path, byte length, and SHA-256 over its exact stored bytes.
The manifest must contain:

- Protocol/addendum/runtime-manifest hashes, attempt ID, checkpoint ID, sequence
  number, candidate ID when applicable, and previous checkpoint-manifest SHA.
- Collection start/end and seal times in UTC, monotonic timestamps with process
  boot identity, and actor/process identity. Explicitly disclose clock resets.
- Source/runtime artifact identities, evidence inventory, actual observations,
  outcome classification, gate decision and reasons, deviations, and missing
  evidence. A schema version is mandatory; freeze the schema with CP0.

Hash the exact manifest bytes after writing it; keep its SHA receipt separately
to avoid self-reference. The first checkpoint has a null predecessor and both
registration hashes. Later checkpoints reference the immediately preceding
sealed manifest, including rejected candidates. Verify file digests, manifest
digest, and chain links before advancing. Close relevant log segments before
hashing; ongoing concurrent work writes new segments rather than appending to
sealed files. Preserve a sequence-based cutoff for each checkpoint.

A missing file, hash mismatch, write error, or interrupted seal blocks the
dependent transition. Preserve partial evidence and terminate or record recovery
explicitly; do not regenerate a passing history. Never overwrite a sealed bundle.
Corrections append a new linked record identifying the old record and reason.

Hashes provide content commitments, not proof that observations are truthful or
that a local clock is trustworthy. An independent published digest supplies an
external timing reference. This request creates a local checkpoint registration;
it does not publish it or manufacture checkpoint-result hashes before execution.
Private raw evidence and redacted releases have distinct manifests and hashes;
record the mapping without exposing credentials or unrelated calendar data.

## CP6: terminal comparison and cleanup

Report the registered prediction, observed pre-modification behavior, intervention
digest, observed post-modification behavior, and final classification side by
side. Every assertion must reference a checkpoint and evidence file. Mark unreached
checkpoints as not reached; never fabricate an empty success record for them.

Record all failures, autonomous revisions, manual interventions, deviations,
outages, and elapsed time. Retain both unsuccessful and successful candidates.
If interrupted, preserve the last sealed checkpoint and partial evidence and
append terminal accounting on recovery; loss of evidence is itself reportable.

After CP5 is sealed, clean up only verified experiment artifacts according to
the authorized cleanup scope and append receipts to CP6. For an early failure,
first seal available failure/provider-state evidence, then record any cleanup
in CP6. Rollback and calendar deletion are distinct actions. Finish with the
terminal manifest SHA, without changing CP1 or CP5 to match the final narrative.
