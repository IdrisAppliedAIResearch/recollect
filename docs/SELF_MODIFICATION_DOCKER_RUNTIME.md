# Local Docker fixture runtime

Implementation and qualification work log, not a preregistration amendment.

`docker_runtime.py` implements the fixture runtime interface using the native Docker
CLI. It requires an absolute executable, explicit local Unix-socket/named-pipe
endpoint and a host-owned shared root. It never launches Docker Desktop, pulls or
builds an image, changes Docker settings, or starts models. The fixture spec pins
the image by immutable local ID. The runtime is one-use; refresh needs a new runtime
instance as well as the executor's explicit diagnostic grant.

Each runtime creates a private CLI configuration and each run creates a fresh,
regular, unlinked input directory. Inherited Docker context, credentials, proxy,
TLS and other Docker environment overrides are excluded. Fixed CLI arguments
select the local endpoint/configuration. The input contains only the frozen spec
and supervisor bytes; there is no repository, credential or Docker-socket mount.
The executor separately attests the effective container profile before release.

## Transport and cancellation

`process.py` is a transport for trusted native CLI commands, not a host runner for
candidate code. Reader threads retain bounded byte buffers; an independent
watchdog observes cancellation and clock integrity while the caller may be recording
evidence. READY is framed separately from the report. The single bounded release
uses a monitored writer and keeps stdin open: the supervisor interprets EOF as
controller disappearance. Collection requires stdout/stderr EOF and process exit.
Active work has explicit `None` deadlines; finite low-level qualification fixtures
still enforce expiry. Output overflow, clock loss and uncertain pipe teardown
remain terminal errors. See [amendment 02](SELF_MODIFICATION_AMENDMENT_02.md).

The executor's `run_async` runs blocking work off the event loop. Cancellation
signals both executor and transport, then waits for the synchronous operation and
failure accounting. Further cancellation cannot interrupt that finalization. A
successfully collected but undelivered receipt is discarded if caller cancellation
wins the handoff. Primary failure remains permanent. Cleanup uses a separate
finite deadline and ignores the run's cancellation token. Unconfirmed cleanup or
accounting is reported as an integrity failure, not clean cancellation.
Cleanup uses its own clock even if the execution clock permanently fails. A
throwing cancellation hook is recorded but cannot bypass finalization. Async
delivery rechecks cancellation, clock continuity and any finite fault-fixture
deadline after the worker thread returns. Active work has no age-based cutoff.

## Daemon reconciliation

Killing a CLI process does not cancel a request already delivered to Docker. The
adapter reconciles both the unique run label and exact name, then validates full
ID, image and ownership labels before any mutation. An owned container with a bad
resource profile may still be stopped; it never qualifies for execution.

After confirmed stop the adapter removes only the verified full container ID,
without force, and verifies absence. Removal prevents a delayed start on that ID.
An ambiguous create followed by no visible container is **not** confirmed cleanup:
creation could still be pending, so refresh is blocked. Multiple identities,
foreign labels, lookup failures or uncertain removal also block confirmation.
Bounded CLI outputs and termination observations, including unconfirmed cleanup,
are retained in executor evidence before their confirmation is interpreted.
The runtime never removes input/config directories; their paths remain under the
supplied shared root for explicit inspection/cleanup after confirmed termination.

## Limits and remaining qualification

The trusted boundary includes the native CLI, Docker daemon, kernel and host
filesystem. Native process creation and hung host filesystem operations are not
made interruptible by Python timeouts. Pipe teardown fails closed if a descendant
holds inherited handles; it does not kill arbitrary host process trees. These
limits follow the [Python subprocess contract](https://docs.python.org/3/library/subprocess.html).
CLI configuration is explicitly frozen because environment/context settings can
change Docker behavior; see the [Docker CLI documentation](https://docs.docker.com/reference/cli/docker/).

Unit tests cover fake-daemon lifecycle faults, real benign child-process streams,
watchdog expiry, cancellation races and byte limits. The live qualification below
adds native Docker adapter/executor and host-local checkpoint integration evidence.
It does not establish arbitrary daemon-race behavior, daemon-restart recovery,
power-loss durability or a hard real-time host guarantee. Real agent launch and
transport authentication remain required. A fixture receipt alone is not CP2,
CP6, deployment approval, or permission to resume the primary Calendar task.

## Verification record

On 2026-09-12, the final full suite reported `1539 passed, 17 skipped,
2 warnings in 138.74s (0:02:18)` and Ruff reported `All checks passed!`.
This slice adds 48 tests. Independent review identified cleanup-clock,
receipt-delivery, failed-termination-evidence and cancellation-hook gaps; fixes
and regression tests were re-reviewed with no remaining focused blockers.
No real Docker command or target trial was run. The 13 existing containment
Docker tests remain skipped. The three preregistration and two planning-file
hashes were rechecked unchanged. Test evidence is retained under `.agent/adapter-*`.

## Production-path live qualification (2026-09-12)

The preceding verification record describes the earlier adapter implementation.
The containment suite subsequently passed 14 real Docker cases, including effective
cgroup-v2 OOM enforcement; see [containment qualification](SELF_MODIFICATION_CONTAINMENT.md).

`tests/test_selfmod_runtime_docker.py` adds 12 opt-in cases through the actual
`DockerFixtureRuntime`, native `PipeCommand`, `FixtureExecutor.run_async`, and,
for two cases, host-local development/controller integration. The post-review
focused run reported `20 passed in 24.43s`: 12 live cases and 8 offline regressions
for qualification helpers. No production source change was required.

| Case | Required observation |
| --- | --- |
| Exact candidate capture | Complete expected bytes, protected files unchanged, verified receipt and owned-container absence |
| Lost create/READY/release response (3) | Native operation completed before injected response loss; no primary retry or snapshot; verified full-ID removal and archived failure |
| Execution deadline | Release reached, real host deadline failure, confirmed cleanup, no snapshot |
| Repeated async cancellation | Execution stays pending while cleanup is held at a bounded test barrier; repeated cancellation cannot bypass cleanup/accounting |
| Lost lookup/removal response (2) | Unconfirmed termination, no receipt or refresh; successful removal with lost acknowledgement remains uncertain despite independent absence |
| Diagnostic refresh | Fresh runtime/container/run/attempt/instance, unchanged baseline/policy/deadline, grant replay rejected, resulting receipt remains diagnostic |
| Refresh limit | Two separately stopped failed runs exhaust the one-refresh lineage budget |
| CP2/CP6 success integration | Exact candidate and executor evidence bytes copied into both checkpoints; terminal result explicitly `simulation_complete` |
| CP6 cancellation integration | Confirmed termination and caller-cancellation bytes retained; `simulation_failed`, no CP2 |

Response-loss tests interpose **after** real native calls; they are deterministic
fault injections, not claims of observed spontaneous daemon faults. A release
write proves pipe delivery, not worker acceptance. The deadline observation
includes cleanup but is not a hard host latency guarantee. Teardown separately
reconciles only exact test-owned full IDs, name, image and both labels before any
mutation; rescue removal cannot count as passing production termination.
Independent code review caught teardown short-circuiting on pipe-close failure
and missing successful-response assertions in fault prerequisites. Teardown now
continues verified cleanup, accumulates errors, and retains directories on any
uncertainty. Fault assertions outside cleanup require successful native results,
actual injection, and valid READY binding. Offline helper regressions exercise
pipe-close/lookup/removal failure, foreign identity refusal and false fault claims.

Execution and independent observations used the existing local named-pipe endpoint
and immutable image ID
`sha256:a6f4bc8e8d17018436caeba85e4ac13e26605c504cc67c378ab81fe8a35e8bae`.
No daemon shutdown/restart, image build, model request, Calendar access, or service
configuration change was involved. Simulated baseline/review/evaluation/activation
reports remain explicit fixtures; this is not the target-capability rehearsal.
The test-owned containers and external input/config directories were removed.
Evidence is retained under `.agent/runtime-live-*`. Initial test-only expectation
errors (snapshot ordering and claimed/verified journal record count) were corrected;
their failed-run evidence is retained separately, not treated as qualification.

Final post-review full validation, with both Docker opt-ins enabled: Ruff reported
`All checks passed!`; pytest reported
`1647 passed, 1 skipped, 2 warnings in 233.52s (0:03:53)`. All 29 live Docker cases
passed (14 containment, 12 production-path, 3 existing sandbox). Focused independent
rereview found no remaining blockers in this qualification scope. The earlier
`1639 passed` full run predates the review fixes and is not the final validation.
The final report is `.agent/runtime-live-final-full.xml`; ignored test evidence
remains intentionally retained. Recollect and Qwen stayed healthy, no test-owned
containers or external input directories remained, and all three preregistration
and two planning-capture hashes were unchanged. Changes remain uncommitted.
