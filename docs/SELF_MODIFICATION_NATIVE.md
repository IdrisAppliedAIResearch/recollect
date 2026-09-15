# Native OpenCode integration and qualification

Implementation/qualification notes, not an experiment result or registration.

`selfmod.native.NativeSession` speaks to the pinned OpenCode 1.18.18 session API
through an explicitly supplied transport. There is no default network transport,
process launcher, receipt-import endpoint, candidate
capture, approval or deployment authority. It must not be substituted for the
qualified fixture executor. The ordinary research runner is unchanged.

## What this slice establishes

- A retained native build-agent conversation can use native read/edit tools,
  undergo explicit or threshold-triggered automatic native compaction and
  continue in the same session.
- Configuration, original task context and raw HTTP requests/responses are
  recorded in a separate host-owned append-only journal. Each continuation
  includes the original immutable authority bytes. The same context is provided
  as a read-only instruction file inside the qualification container.
- The pinned configuration enables automatic compaction and pruning, omits
  agent steps, denies research/delegation/shell tools and limits edit permissions
  to the provisioned change list. Native edit/write permissions match paths
  relative to the project root, so this non-repository profile checks root `/`
  and uses `work/<allowed path>`. OS ownership remains the actual write boundary.
- Returned messages must belong to the assigned session, finish without a native
  error and occur unchanged in history. With a dedicated durable-event reader,
  automatic continuation additionally proves the original user task, compaction,
  completed summary, synthetic continuation and final message in event order.
  Old message updates cannot impersonate new turns. Cyclic cursors and mismatches
  poison reuse. Without that reader, strict original-parent matching remains.
- Interrupted and oversized responses retain a bounded prefix with explicit
  incomplete/failure metadata. Close retains one shielded cleanup task across
  repeated cancellation. Local HTTP closure never attests GPU/process quiescence.

The real compatibility test runs the existing image by immutable local image ID,
with no network, a read-only root and read-only input mount. A scripted provider
listens only on container loopback; it does not call the host model. OpenCode runs
as UID 65532 with no effective capabilities. Existing editable files remain
root-owned/group-writable, and their parent directories forbid replacement or
unlink. Runtime state is separate from source. The test asserts a real edit,
compaction history, continuation after an intentionally misleading summary,
unchanged authority bytes and rejected writes to protected paths.

The test-only relay sends headers and bounded chunks incrementally through an
owned Docker subprocess. It reaps the CLI on cancellation; removal of the owned
container separately reconciles any exec child. Stderr is continuously drained
with bounded retention, and premature Content-Length EOF cannot emit a successful
end frame. Log captures are independent,
capture failures are surfaced, and input deletion requires confirmed container
absence. Cleanup is shielded from repeated cancellation, including setup failure.
This is a diagnostic fixture, not a qualified production supervisor/broker.

## Joint broker, history and controller-boundary slice

`NativeModelBroker` strips `max_tokens`, `max_completion_tokens` and
`max_output_tokens` only from the top-level forwarded request. The original native
request and each forwarded response chunk are durably archived. Model capacity
metadata remains available to native compaction. The broker has no call, token or
working-time quota; resource byte/depth bounds remain explicit. It freezes the
loopback model endpoint, ignores environment proxies, forbids redirects and
retries, and checks host authority before dispatch and delivery. Request identity
can include the verified pre-dispatch native-history watermark. HTTP completion
or close is never labeled upstream quiescence.
The accepted message shapes are text and function tools only: media URLs cannot
turn the loopback provider into a network-fetch deputy. Plain text, code, tool
arguments and assistant-only string `reasoning_content` remain unchanged.

The pinned binary's EventV2 implementation commits message/part updates, event
sequence and projection changes in one transaction. Pruning marks tool parts as
compacted without erasing their original output from the durable event log.
`native_history_reader.py` reads consistent SQLite snapshots, including committed
WAL contents, through fixed highwaters. `NativeHistory` checks contiguous sequence
and boundary identities, archives bounded pages/fragments, and never treats a
summary as the evidence archive. Its completeness claim is specifically
`committed_native_events`, not every transient SSE delta. Finalization requires
the owner to stop writers first; the reader itself is not a stop attestation.
Ordinary capture verifies the incremental boundary. Finalization rereads every
event from sequence zero through the fixed watermark and compares its identity
and canonical-row hash against verified host evidence. An old row modified and
later hidden behind the cursor therefore fails finalization. Verification pages
remain separate from the originally accepted archive.

The live qualification now routes actual native requests through the host broker
to a scripted provider. Both manual and automatic compaction paths preserve native
read/edit tool use and original authority despite a hostile summary. Original
requests still show output capacity 4096; actual forwarded requests omit all three
token-cap fields and preserve all other fields. Background title generation is
disabled, and no title-model request appears. Before final history capture the
test freezes the native writer and verifies no other unprivileged process exists.
This is not a GPU-stop or production candidate receipt.
The diagnostic failure path freezes writers and archives bounded raw database,
WAL and shared-memory chunks before container removal, even if semantic history
capture is poisoned. Raw evidence is explicitly not labeled complete history.
The failure test reconstructs that database and verifies committed events beyond
the last accepted host watermark. If raw preservation or launch ownership is
uncertain, the container and inputs are retained. Launch settlement and exact
container reconciliation also cover cancellation before the launch reply.

After two permission failures and explicit user approval, the diagnostic trusted
root supervisor has `DAC_READ_SEARCH` to read OpenCode's private state. Its worker
remains UID 65532 with zero effective, permitted and ambient capabilities. The
fixture supervisor is unchanged. A production native profile must separately pin
and attest this access. The test-only mailbox buffers a bounded scripted reply;
it is deliberately not a production streaming IPC implementation.

`IntegratedDevelopment.admit_native()` / `NativeAdmission` now consume the real
opaque execute grant, freeze the actual contract/current plan/findings, and retain
the controller lease through release, revocation and settlement. Unknown native
or upstream stop retains ownership. This is an execute-admission foundation:
there is no candidate-import endpoint or native success receipt. Closing it
permanently fails the primary path rather than inventing a successful handoff.
Its records use the existing development evidence kinds and survive CP6 export.

## Terminal source-capture boundary

`native_capture.py` freezes a native run, settings, complete baseline and helper
bytes. The separate stdlib-only `native_capture_worker.py` is invoked from the
read-only authority mount, not from the editable tree. Its output is capture
evidence; it is not a fixture receipt or a native execution-success receipt.

After a terminal native response, the owning adapter must close dispatch and
respawn admission. The collector validates the expected native PID **and start
identity**, inspects all process threads, and uses PID-bound signals to terminate
unprivileged namespace workers. Only the pinned trusted PID 1 and collector may
remain executable. A paused worker does not qualify as stopped. The root capture
profile includes `KILL` for this terminal operation, alongside `CHOWN`,
`DAC_READ_SEARCH`, `SETGID` and `SETUID`. Worker effective/permitted/inheritable/
ambient capabilities remain zero; the bounding set is not an effective grant.
There is no worker-age, silence, token or call cutoff.

The host must then finalize its owned `NativeHistory` archive. Capture binds the
stop bytes, native session and exact finalized watermark/hash. The collector
checks that SQLite boundary before and after reading source. Descriptor-relative
no-follow opens, device/inode identity, modes, ownership, link counts, sizes,
timestamps and repeated directory inventories guard source reads. Missing files,
protected drift, unexpected files, links or any incomplete read fail capture.
The host independently reconstructs the immutable `Snapshot` and validates policy,
metadata, digests and exact binding types against verified journal finality.

Terminal SQLite reads use a separate private-copy helper after writer quiescence.
It streams the database and any WAL/rollback journal into root-private temporary
scratch under `/evidence`; SQLite rebuilds its transient SHM there. The original
database is never opened for writes, and live history reads remain unchanged.
This handles clean checkpointed databases whose missing sidecars would otherwise
make a read-only open fail. It does not use SQLite's `immutable` option or ignore
an existing WAL. File identity/metadata and sidecar inventory are rechecked around
copying and reading; ancestor identity, mode and ownership are also checked.
The caller still must maintain quiescence: this is not a live backup mechanism.

Temporary copies are removed on success or failure; cleanup failure prevents
successful capture. The diagnostic `/evidence` mount remains 32 MiB, so a large
history can exhaust its available scratch capacity and fail capture. A hot
rollback journal requiring writable recovery also fails rather than silently
repairing history. Neither case confers candidate authority. Windows fixture
descriptor comparisons account for that platform's differing path/handle ctime
semantics; Linux production retains the complete nanosecond ctime comparison.

This is one prerequisite for the production executor, not that executor itself.
The trusted owner still must attest the namespace, fence respawn and new commands,
durably record raw stop/capture failures, establish upstream model settlement,
and perform current-authority handoff. The collector cannot stop a privileged host
from starting a new process, and these parsing functions cannot confer authority
on worker-authored JSON. `NativeAdmission.close()` remains fail-closed without a
qualified candidate executor. Qualification results are recorded in the harness
implementation log after verification.

## Native supervisor startup and terminal collection

The production-oriented helpers are separate from the diagnostic fixture.
`NativeRuntimeSpec` freezes the capture inputs, native binary hash, helper bytes,
image identity/environment and strict namespace profile. Host inspection must
attest that profile before the bound release. PID 1 provisions the allowed source
tree, launches the unprivileged native server, watches its exit without reaping
its identity, and retains the namespace after terminal collection. Control-input
loss, unexpected native exit before terminal fencing, log failure and model-bridge
failure permanently fail the run. Collection runs independently of blocked
control output; its notification is not
required to preserve the namespace. None of these messages is a candidate receipt.

The model bridge uses explicit chunked-response completion and bounded streaming
credit. Closing an unfinished exchange latches failure. The local HTTP transport
pins the Docker executable, local endpoint, isolated configuration and fixed
relay; it incrementally drains response and error output and reaps its CLI.
Neither a closed bridge nor a reaped CLI proves provider-side model settlement.

The no-inference live checks exercise a bound fence and control EOF. They verify
the collector result and native PID/start-bound stop record while PID 1 remains
alive, then separately remove the verified test namespace. Startup distinguishes
`native_started` (process identity observed) from HTTP readiness. A bounded
unprivileged readiness probe precedes the single transport health request; no
native work deadline is imposed. Failed early-startup attempts remain evidence,
and the readiness-gated qualification does not establish their underlying cause.

During terminal process inventory, a disappeared task file can report ENOENT or
ESRCH. For those task-file read errors, only confirmed task absence restarts the
complete inventory. Changed PID/thread inventories also restart it. An existing
but unreadable task, malformed credentials, identity drift or other IO failure
still fails closed. This is not a relaxation of the trusted-root allowlist.

The live fixture also owns cancellation during create/attach, retains inputs
after unresolved creation, and settles attachment readers even when namespace
cleanup fails. Root evidence-reading commands are gated on the bound
collection-finished notification. Emergency evidence copying uses a paused,
independently verified namespace and is not labeled semantic history or successful
collection.

Remaining production boundaries include host ownership and dispatch settlement:
the current HTTP relay uses a root Docker exec, which cannot be allowed to outlive
or arrive after the collector fence. This must be resolved without expanding the
collector's trusted-root peers. These helper checks do not supply authenticated
candidate handoff, production failure-history recovery or upstream quiescence.

## Pinned timeout behavior

The installed binary SHA-256 is
`bb71f45b564f9234a97f54d6252a4a41d2f4388ae4b078918f691824cc3b3e54`.
Read-only inspection of its embedded implementation found an optional
`chunkTimeout` schema field and a fetch wrapper that creates the chunk abort
controller only when that field is a positive number. There is no default
assignment to this option in the inspected binary. Its schema rejects both
`false` and zero; both rejected live attempts remain archived. After user approval,
omitting the field was accepted and read back absent from the effective provider
options. Request/header timeouts are explicitly false; host HTTPX timeouts are
None. The five-second readiness diagnostic is retried without an overall deadline
while the test container remains alive; it does not cancel native work.

Do not replace omission with a very large timeout or infer behavior from newer
documentation. Requalify these semantics against any different binary/profile.

## Experiment-readiness status (2026-09-15)

The production executor, handoff, scalable evidence, pruning and projection
agreement are implemented and qualified; see the harness implementation log.

1. **Resolved:** PID 1 launch gate and IPC lanes replace per-request Docker exec;
   the owned `NativeRuntime` joins admission, broker and history, captures
   quiescent immutable source and hands it once to development gates, where real
   container checks and a fresh code review ran against the exact candidate.
2. **Resolved:** live old-tool pruning, whole-session projection/event agreement,
   production failure-time database/WAL preservation and collector-confirmed
   quiescence.
3. **Resolved:** linear journal append and exact-anchor segment sidecars through
   CP2/CP6 without flattening.
4. **Remaining:** real three-lane overlap on a three-slot model server (pinned
   lanes and slot settlement are fixture-qualified), model-server provider
   defaults, and the task-coordinator integration of A/B bundle launch.

No Google Calendar action, local-model probe, runtime activation or experiment
PASS occurred. Original registration/planning documents are not modified.

## Evidence

The 2026-09-14 supervisor/helper qualification finished with Ruff clean and
`2774 passed, 3 skipped, 2 warnings in 707.51s` for the full suite with both
Docker opt-ins enabled and real-model tests disabled. All 55 live Docker cases
passed, including 12 native cases (two exercise this supervisor). The separate
fresh fence/EOF run passed both cases in 17.24 seconds. Receipts are
`.agent/native-supervisor-qualified-full.xml` and
`.agent/native-supervisor-census-live.xml`. This qualifies the documented helper
scope only, not the production executor or experiment readiness.

Preserve `.agent/native-*` test directories and XML. They include the initial
streaming-mock defect, rejected timeout settings, native configuration
normalization, an incorrect scripted-provider substring check, the original
absolute-path permission mismatch, and subsequent passing compatibility runs.
Successful focused results and the final full-suite result are recorded in the
harness implementation log; failed attempts are not retroactively relabeled.

Fresh-context forward/code review cleared this diagnostic-only scope after the
cleanup, incremental relay, truncated-body and durable-append fixes. Inspection
does not replace live tests or qualify the explicitly deferred production work.
