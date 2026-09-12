# Offline checkpoint/controller implementation contract

Status: offline implementation; no target experiment or live integration.
The three registered protocols and the verbatim planning capture remain unchanged.

## Definition of done for this slice

A simulated attempt produces an independently verifiable CP0-CP6 evidence chain.
Every injected persistence, receipt, identity, ordering, or integrity failure
blocks dependent progression. Success in these fixtures is not Google-task PASS.

## Trust boundary and storage design

The controller is ordinary synchronous host code, not a model or an agent-facing
RPC API. It consumes observations from trusted fixture adapters in this slice.
Actual provider, model, sandbox, and reviewer attestations remain future work.
Run blocking disk work off the serving event loop when integrating it later.

Use a single-attempt local SQLite journal with verified DELETE/EXTRA durability
settings, append-only records, and an independent fresh-connection reader. Exact
evidence bytes and canonical UTF-8/LF manifests are archived with their records.
Materialized checkpoint files are additionally verified before progression;
the journal keeps the submitted bytes if materialization is interrupted. Never
repair an evidence discrepancy and continue the same primary attempt.

SQLite EXTRA/DELETE selects the synchronization policy described in
[SQLite's durability documentation](https://www.sqlite.org/pragma.html#pragma_synchronous).
That policy is VFS-dependent: the [upstream Windows deletion implementation](https://github.com/sqlite/sqlite/blob/master/src/os_win.c) ignores
the `syncDir` argument, so the extra directory-sync explanation alone is not a
Windows power-loss guarantee. Freeze the installed SQLite build/VFS and qualify
the actual storage before live use. Durability still assumes a supported local
filesystem and an OS/storage stack that honors flushes; these tests do not
simulate physical power loss or certify hardware. Network shares are outside
the supported deployment contract.

Controller-owned storage must not be writable by workers. Hashes and SQL
immutability guards detect accidents and changes against a pinned journal head;
they are not protection against an administrator rewriting the entire archive
and all external anchors. Published timestamping remains separately authorized.

## Required ordering

- CP0 precedes durable receipt of the original request. Budget timing starts
  conservatively immediately before receipt persistence; the later completed
  readback is recorded separately. No persistence/readback interval becomes free
  time. Later stages do not reset or pause the original 60-minute budget.
- CP1 must seal and verify before modification is schedulable. A mixed false
  completion claim, unknown effect, or nonquiescent target cannot pass.
- Candidate acceptance durably binds submission ID, counter, and artifact digest.
  Same-ID/same-digest redelivery is idempotent; conflicting identity is terminal.
  CP2 precedes evaluation, and CP3 precedes feedback or activation.
- Permit at most three submissions and one evaluator infrastructure retest across
  the whole attempt. An accepted third candidate may finish. Preserve rejections;
  do not retry identical rejected candidate bytes to seek a pass.
  Positively identified host infrastructure failure can include partial passing
  assertions, but cannot conceal a failed assertion or lost evaluator capture.
- CP4 verifies the accepted artifact and actual gated route transition. No target
  continuation is schedulable until its seal/receipt verify.
- CP5 verification, receipt persistence/readback, and durable endpoint-marker
  completion precede the timed endpoint observation. CP6 records the observation
  without inserting its future timestamp into CP5's already sealed manifest.
- Failures permanently remove primary eligibility. Restart is recovery/accounting
  only, never a continuation of primary execution. CP6 names partial/unreached
  checkpoints and preserves confirmed counts and uncertain writes.

## Failure-only accounting

The primary verifier rejects any forensic accounting branch. A separate inspector
can identify the still-verifiable checkpoint prefix when materialized evidence is
missing or damaged. It does not repair that evidence. An explicit failed branch
may append only a failed CP6; recorded seals and currently verified seals are
reported separately. The archive retains the original submitted bytes, and damaged
disk files remain untouched. Earlier reasons, submission counts, retest use, and
original clock observations remain available across accounting-only reopen.

Bundle names include their archive sequence, so an interrupted preparation never
collides with a later accounting bundle. A sealed CP6 without its materialized,
verified receipt is incomplete. Final accounting adds a separate completion
marker after CP6 verification/readback; no self-referential hash is needed.
Any exception during that marker write/readback remains terminal, even if the
write later proves to have committed. `Controller.recover` always declares lost
continuity and permits only failed accounting; it does not interpret an old
completion marker as permission to restore success. This includes an explicit
recovery of a previously completed archive. To inspect a completed result without
changing it, use `read_archive` against the head retained after the successful
call, and independently verify its materialized checkpoints. Do not call recovery
as a status/inspection API. A local marker is not a public timestamp.
Storage failure can also prevent CP6: the controller stops, preserves whatever
was written, and requires accounting-only recovery when storage is usable.

The snapshot/development gates in `development.py` are not yet connected to this
controller's fixture submission API. Simulation scheduling decisions are consumed
once but do not execute agents. Real execution must bind grants to the current
controller instance, phase, and accepted digest at the privileged dispatch point.

## Verification and fault-injection matrix

- Happy chain; candidate rejection/revision; one full infrastructure retest;
  second infrastructure error on a later candidate; third/fourth submission.
- Missing, extra, corrupted, reordered, cross-attempt, or self-referential evidence;
  stale journal anchors; duplicate/conflicting submissions; skipped stages.
- Failure before and after prepare/commit/materialization/readback/seal/receipt;
  late receipt/endpoint writes; poisoned writer refuses subsequent progression.
- Process interruption at boundaries; second writer denied; reopening never
  dispatches; recovery records terminal accounting without repairing history.
- Interrupted CP6 prepare, materialize, seal, receipt, and pre-completion writes;
  later failed accounting preserves earlier bundles rather than overwriting them.
- Correct event-shaped fixture evidence before deadline but endpoint after it;
  inclusive endpoint exactly at the limit; CP6 after the limit; clock regression.

Runtime identity freeze, real containment, physical storage qualification,
dispatch-time permission enforcement, and provider-specific verification are not
claimed by this offline slice.
