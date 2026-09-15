# Host-local checkpoint/development integration

Implementation contract, not preregistration, a live-trial result, or evidence of
authenticated remote agents. The immutable registrations and planning capture
are unchanged.

`Controller.open_development` now joins the offline CP0–CP6 controller,
`Development` gates and owned `FixtureExecutor`. It requires a verified passing
CP1 and consumes the modification dispatch before returning a development cycle.
The original request, policy, baseline and attempt clock remain fixed. The active
timing policy is unbounded under [amendment 02](SELF_MODIFICATION_AMENDMENT_02.md).
Runtime settings cannot change when a rejected candidate starts a subsequent
cycle. Update/review counts accumulate only for auditing; the subsequent explicit
no-quota decision removed their admission ceilings. Candidate and infrastructure-retest limits
remain the controller's existing attempt-wide limits.

## Local authority

The trusted host calls `authorize(action)` to mint one opaque, single-use grant
for the assigned author, forward reviewer, checker, code reviewer or submitter.
Each grant binds controller instance, cycle, action, stage, gate generation and
exact current development binding. An initial plan grant has no plan binding.
Consumption compares retained object identity, not a caller-supplied ID or a
reconstructed dataclass. Journaled grant IDs are audit records, never credentials.
Only one handoff can be outstanding. A lost handoff is not silently retried.

All these objects, minting methods and controller references stay in the trusted
host. They must not be given to workers, exposed as tool methods, deserialized
from worker reports, or treated as an authentication system for network clients.
The subsequent [isolated role runners](SELF_MODIFICATION_ROLES.md) obtain reports
from separately launched containers and associate their owned native attachment,
verified executor archive and frozen request with the host-retained grant. This
authenticates process origin and evidence binding, not a reviewer's judgment.
The original fake-runtime tests below establish only local authorization.

## Due process and execution

The path is plan → forward review → isolated fixture execution → exact frozen
development checks → independent code review → CP2 submission. Report evidence
bytes must match their declared snapshot digest and are archived before the gate
changes. Full review reports retain approval decisions and unresolved findings.
Failed checks and rejected reviews remain revisable outcomes. Identity, scope,
inventory, archive or clock-integrity failures permanently close the primary path.

`execute` is async and owns executor creation and receipt import. It constructs
the fixture spec from the current binding and frozen host inputs; there is no
public receipt-injection or raw-implementation method. Its busy lease fences
concurrent handoffs, CP6 accounting and controller close until cleanup and final
async delivery settle. Resource closure alone does not release the lease.
No controller lock is held across an `await`. Immediately before physical worker
release, the host rechecks controller ownership, eligibility, checkpoint bytes
and clock continuity under the lock. Working deadlines are explicitly `None`;
elapsed or quiet time cannot prevent release. Cleanup is separately bounded only
after an observed terminal event.

Executor handoff requires a completed primary run, exact run/spec/binding, current
archive anchor, independently read-back snapshot bytes and intact clock continuity.
Diagnostic refresh is disabled for owned primary executors. Refreshed,
failed, foreign, stale and cancelled receipts cannot become candidates.
Import records the pre-capture executor binding and the new implementation
revision. Any subsequent edit invalidates previous test/review approval, even
when it recreates identical bytes.

Cancellation waits for worker cleanup and host evidence finalization, including
cancellation during import. A successful executor result alone is provisional.
Failure to persist failure accounting raises an explicit accounting-unconfirmed
error; it never creates a passing checkpoint. Blocking mint/report/submission
methods must also run off the serving event loop. Real OS qualification is
documented separately; Python locks and fake runtimes are not containment proof.

## Evidence and checkpoints

Successful executor records and their files are copied into the controller's
independently verified archive before development advances. The executor closes
after import; CP2 uses this host-owned copy, not a later reread of a worker path.
CP2 contains the cycle's grant history, original plan, reviews/checks, execution
records, source snapshot and transition history. Once integration is enabled,
raw `Controller.submit` is fenced before even its idempotent receipt branch.
The returned facade is not sufficient submission authority: a separate internal
one-shot authorization binds the admitted artifact, evidence and submission ID.

CP6 copies development reports and imported executor bytes as well as its
controller journal. Failed execution imports available records and explicitly
reports capture completeness, termination confirmation and archive errors. A
poisoned controller journal may prevent CP6 itself; preserve its archive and
use the existing accounting-only recovery path. Neither a failed export nor an
uncertain stop is represented as complete evidence. Existing archive size/path
limits apply to aggregate checkpoint bundles; oversize evidence fails closed.

## Remaining scope

The host facade still produces **simulation** checkpoint outcomes. The new
`run_role` path adds one-shot host-brokered model requests and separate networkless
plan/reviewer/modifier/check containers, not native OpenCode or an interactive
agent tool loop. It does not schedule three inference lanes, route A/B deployment,
broker credentials, evaluate Google Calendar, or establish an independent public
timestamp. Runtime identities and production budgets still require prospective
freezing before CP0. Docker qualification and startup diagnosis were deferred at
this slice's original implementation. Subsequent
[production-path qualification](SELF_MODIFICATION_DOCKER_RUNTIME.md) now exercises
real adapter/executor success and cancellation through this facade and verifies
CP2/CP6 evidence bytes. Those fixture reviews/checks are synthetic. The separate
role qualification exercises actual frozen check scripts and model responses;
baseline, activation and experiment outcome still remain simulated. Target
orchestration is pending. No service or target trial is started by these tests.

## Original host-local validation

On 2026-09-12, all 59 new fake-runtime integration tests passed, including
checkpoint evidence byte comparison, foreign/replayed grants, returned-facade
submission bypass, stale/diagnostic/refreshed/late receipts, interrupted import,
unconfirmed accounting and cross-event-loop lease ownership. Independent forward
and code reviews identified submission and finalization gaps; fixes and focused
regressions were re-reviewed with no remaining blockers in this host-local scope.

Final checks: Ruff reported `All checks passed!`; full pytest reported
`1598 passed, 17 skipped, 2 warnings in 161.67s (0:02:41)`. The existing opt-in
Docker tests remain unrun. No UI changed. All three registration hashes and both
planning-capture hashes were rechecked unchanged. Ignored test evidence is
intentionally retained under `.agent/integration-*`; no external scratch files,
live services, provider calls, commits or pushes were created for this slice.
