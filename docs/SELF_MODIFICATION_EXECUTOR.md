# Offline host executor and diagnostic refresh

Implementation status, not preregistration or live-trial evidence.

`selfmod/executor.py` supplies a host-side fixture coordinator over an injected
trusted runtime interface. The tests use fake runtime methods and the real local
SQLite journal. A [local Docker adapter](SELF_MODIFICATION_DOCKER_RUNTIME.md) is
implemented with fake-CLI and benign-child tests; real Docker qualification is
still pending. No host-process fallback, model execution, credential access or
Calendar integration is included.

## Lifecycle

The executor freezes the fixture inputs, supervisor bytes, original attempt clock
identity/timing policy and a finite diagnostic refresh allowance at construction.
One exclusive operation owns the executor; a second call cannot dispatch the
same run. A consumed run cannot be reopened after success or failure. No recovery
API reconstructs execution authority from a journal.

1. Persist and read back run consumption before container preparation.
2. Attest the created/stopped container, then start only the trusted supervisor.
3. Verify READY, re-attest the waiting container and compare host input bytes.
   Refuse input paths overlapping the evidence archive.
4. Archive release intent, bind explicit null timing (or finite fixture remainder), send
   once, and archive the exact transmitted release when sending returns.
5. Collect bounded output through EOF, archive the raw report, verify independent
   container and attachment exit state, and confirm namespace termination.
6. Validate the captured source, archive and read back its bytes, and check the
   clock continuity and any finite fixture deadline before returning a receipt.

Active integrated setup, release, execution and readback use `None` for both
fixture and inherited deadlines. [Amendment 02](SELF_MODIFICATION_AMENDMENT_02.md)
makes timing observational. Low-level finite fault fixtures still reject expiry,
including expiry during readback; they are not the active agent profile. Cleanup
keeps a separate three-second adapter timeout after a terminal observation, even
after clock failure. Root cleanup/capture/report timing starts on a trusted
settling signal or observed supervisor exit, never on work age or silence.

Exceptions (including cancellation raised inside the synchronous operation)
permanently set `primary_failed`, fence dispatch and
attempt namespace reconciliation, including a preparation whose response was
lost. Available bounded late reports remain failure evidence. A snapshot archive
record alone is not a successful receipt: clock/evidence validation or a finite
fault-fixture deadline can produce a terminal record. Journal damage or unconfirmed termination
blocks refresh. Raw worker output and reported success never override host checks.
Observed clock identity/order loss is irreversible even if a later clock reading
looks valid. An already claimed journal cannot initialize another executor and
reset run consumption or refresh counts. Failed exit observations are retained
before interpretation; oversized observations retain a marked bounded prefix.

## Refresh is diagnostic only

There is no automatic retry. The trusted host must explicitly call
`authorize_refresh`, then consume that exact grant with `refresh`. Admission
requires confirmed termination and a durable fixture failure-accounting record.
That record is **not CP6** and does not authorize a new target experiment.

The replacement gets fresh run, attempt and modifier-instance identities. It
starts from the original frozen baseline and policy, not the failed worker's
files. Image, entrypoint, task contract and supervisor identity remain fixed;
artifact approval is cleared. The original overall deadline and lineage-wide
refresh count remain in force. Every replacement result is permanently marked
diagnostic-only. These APIs do not carry forward review approvals or consume/reset
the experiment controller's candidate or infrastructure-retest allowances.

## Trust boundary and remaining work

The runtime interface is trusted host infrastructure. It must independently
observe cancellation, stream-limit output, verify exact resource ownership,
and reconcile all namespace descendants even after ambiguous creation. Python
interface declarations and fake tests do **not** stop a hung adapter, enforce OS
permissions, or authenticate a real termination report. The container watchdog
and real adapter qualification remain mandatory. Input preparation must create a
new host-owned directory; it cannot mount the repository or accept agent flags.
Use the executor's `run_async` entry point for async callers. It sends sticky
cancellation to the runtime, waits for cleanup/accounting even after repeated
cancellation, and discards an undelivered receipt if cancellation wins the handoff.
Unconfirmed cleanup/accounting raises an explicit integrity failure rather than
reporting clean cancellation. This relies on a cooperating runtime; an
uncooperative operation remains unconfirmed and holds its lease rather than
authorizing more work. Finite post-stop cleanup is not a total cancellation bound;
wrapping `run` directly in `asyncio.to_thread` does not supply this cancellation
protocol and is unsupported for production callers.

The [host-local integration](SELF_MODIFICATION_INTEGRATION.md) now supplies verified
CP1 release, local one-shot role grants, owned executor admission, linked CP2/CP6
evidence and primary-only receipt handoff. `verified_receipt` checks the owned
executor's live state, current archive anchor and exact snapshot at import; a
serialized receipt is not authority. Do not expose this fixture coordinator
directly to agents or treat its snapshots as submission, activation or
continuation authority. Actual authenticated review runners, production runtime
qualification and A/B routing remain future slices. Docker startup diagnosis is
deferred by the user; no running service was needed for these tests.

## Validation

On 2026-09-12, 92 new executor tests were included in the final full run:
Ruff reported `All checks passed!`; pytest reported `1491 passed, 17 skipped,
2 warnings in 133.02s (0:02:13)`. Independent review identified clock-continuity,
journal reconstruction and missing exit-evidence issues; fixes and regression
coverage were re-reviewed with no remaining blockers in this offline scope.
The 13 containment Docker tests remain unrun. Registration and planning-capture
hashes are unchanged. Ignored test evidence is retained under `.agent/executor-*`.
