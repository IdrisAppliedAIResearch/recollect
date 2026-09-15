# Unattended trial runner: design

Implementation design, not a preregistration amendment, runtime manifest or
trial result. The registered protocol, checkpoint protocol and amendments 01/02
are unchanged. Every choice below is harness lifecycle policy that must be frozen
in the runtime manifest before CP0; none supplies a Calendar implementation.

## Goal

After the user freezes the runtime manifest and starts one command, the attempt
runs CP0 through CP6 with no human action: request submission, capability-gap
trigger, modifier, independent candidate evaluation, A/B activation, original-task
continuation on B, independent Google verification, duplicate replay, main-chat
probes, terminal accounting and authorized cleanup. Amendment 01 counts any human
intervention as terminal ineligibility, so the runner must not need one.

## Decisions (user, 2026-09-15)

1. **The generated tool is not named.** Acceptance criteria describe the outcome
   and the provided transport only. CP3 therefore grades B end to end with model
   turns against provider fixtures; the resulting nondeterminism is accepted.
2. **CP1 quiescence by harness cancellation.** After A's durable `blocked`
   capability-gap report, the controller cancels A's target task and confirms its
   sandbox invocation ended before the independent read and CP1 seal.
3. **Dedicated trial command.** `recollect selfmod-trial` owns its own app server
   (experiment profile) and controller. Nothing else uses that app during a run.

## Process shape

```
recollect selfmod-trial --manifest <frozen runtime manifest> --attempt <dir>
  verify manifest + registrations          (no freeze, no publication)
  AppState(experiment profile)             3 lanes, continuous OpenCode, unbounded
  loopback app server                      UI viewable; probes use its chat path
  TrialOrchestrator(controller mode=primary)
    CP0 collectors -> receive_request -> main chat turn (exact bytes)
    watch A's task mailbox -> cancel target task -> quiescence -> verifier -> CP1
    open_development -> native modifier (slot 2) -> checks/reviews -> CP2.n
    candidate evaluator (fixtures, modifier lane) -> CP3.n   (<= 3 candidates)
    B image -> router/sandboxes -> gate closed, verifier empty -> CP4
    release continuation on B -> provider relay (gate open) -> result + reply
    verifier read/search -> replay same call -> attribution -> probes -> CP5
    endpoint marker -> verified-event cleanup -> CP6
```

Any failure follows amendment 01: abort, roll routing back to A when activation
is open, close provider gates, preserve evidence, and seal CP6.

## Components

| # | Component | Purpose |
| --- | --- | --- |
| T1 | Controller `primary` mode | Same gates/evidence as simulation; mode, result labels and deviations reflect a live attempt. Simulation stays the default for fixtures. |
| T2 | Experiment runtime profile | Amendment 02 adaptation of app transports: no per-inference token cap or request timeout in ModelIngress, generator, task announcements or native provider config; lane-parameterized ingress so evaluation can use the modifier lane. |
| T3 | Subagent bundle and deployment-aware tasks | A's implementation tree (tool server package, skills, dependency lock, launch metadata) as a verified bundle image; configgen launches the bundle's tool server; the task coordinator selects a sandbox manager through `DeploymentSandboxes`. |
| T4 | Provider relay | Host HTTP endpoint reachable from sandboxes with a per-task bearer capability. Calendar-API-shaped routes on a stable alias map to the private calendar through `ProviderBroker`; the frozen action/deduplication identity is exposed to the worker; each provider operation is correlated with the single in-flight native tool call observed by the host. Credentials never enter a sandbox. |
| T5 | Candidate evaluator | Frozen, outside the modifier's reach: boundary/lock checks, candidate image build, regression (tool server starts, reporting/research tools present), and model-driven provider-fixture scenarios (correct event, no attendees, denied access, provider failure, response loss with reconciliation, malformed request). Explicit infrastructure-error classification. Scenario specs are data so qualification can use a non-target capability. |
| T6 | Trial orchestrator | The CP0-CP6 state machine above, including collectors, gap watcher, cancellation/quiescence, development cycles, activation, continuation, replay, attribution, probes at first modifier generation and first candidate evaluation, the frozen unrelated workload on A, cleanup and failure accounting. |
| T7 | CLI and manifest verification | `selfmod-trial` plus manifest schema/identity verification. A separate user-run freeze command writes the manifest at preregistration; the agent never runs it for real. |
| T8 | Qualification | Unit tests per component and an end-to-end non-target fixture run (scripted three-slot model, fake provider, real Docker where applicable) through CP6, plus failure paths. No target rehearsal and no generated Calendar code. |

## Replay without a named tool

The host records B's generated tool call (tool name, arguments, native call ID)
from the continuation's authenticated history and from the relay correlation.
The replay re-invokes that same tool with identical arguments through B's own tool
server in the serving container, under the same action and deduplication identity
(`phase="replay"`). No model turn is involved.

## Known limitations to disclose before freezing

- Invocation attribution relies on host correlation between the relay request
  and the single in-flight native tool call, as slot linkage relies on lane
  exclusivity; it is not a server-side request identity.
- Bundle images are built by create/copy/commit without network installs, so a
  candidate can only use dependencies already present in the pinned base image.
- CP3 model-driven scenarios can reject a correct candidate by chance; each
  rejection consumes one of the three registered submissions.
