# Self-modification harness implementation status

This is an implementation work log, not a preregistration amendment or a claim
that the live experiment is ready. The original protocol, checkpoint protocol,
and amendment 01 remain authoritative and unchanged.

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

## Remaining runtime work

| Planning requirement | Remaining implementation / verification |
| --- | --- |
| Deterministic controller and checkpoints | Durable journal, CP0-CP6 seals, independent chain verification, deadline endpoint, recovery accounting |
| Containment and refresh | OS/container resource limits, read-only mounts, network policy, independent watchdog, confirmed termination, separately authorized replacement attempts |
| Controlled candidate construction | Quiescent filesystem snapshot capture, link/mode/entry validation, immutable artifacts and dependency locks |
| Review due process | Fresh reviewer launch, read-only inputs, findings/resolution history, authenticated reports, bounded review rounds |
| Three concurrent streams | Admission/ingress changes, independent sandboxes, request-linked server generation evidence, GPU preflight |
| A/B deployment | Pinned task routing, accepted-digest launch, CP4 continuation gate, A draining and rollback |
| Original task defines done | Frozen independent evaluator, provider fixtures, credential broker, generated-code attribution, real Google verification and replay |

Review severities and production budgets are not silently selected here; freeze
them in the runtime manifest before CP0. No prebuilt calendar adapter or target
rehearsal is included. No live services, account connection, calendar mutation,
publication, or experiment execution is authorized by this implementation log.
