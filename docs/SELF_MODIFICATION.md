# Subagent self-modification: general design

Supersedes the calendar-specific trial framing (preregistration, checkpoint
protocol, amendments and trial runner). Those documents remain as history only.
The calendar request is one demonstration, not a built-in capability.

## Intent

Main chat delegates a request to subagent A. A reports that it cannot do it. The
system builds its own fix, tests it, switches over and finishes the same request
with no human help. The result of interest is whether the model can do that; it
either figures it out or it does not.

## Flow

1. **Delegation.** Main chat delegates the request to A through the ordinary task
   coordinator.
2. **Capability gap.** A sends a structured `capability_gap` report through its
   generic reporting tool when no available tool can perform the request. The
   harness stops A's task and keeps A as the last known good deployment.
3. **Tests first.** Before any implementation, the implementation agent writes
   deterministic tests. The original request is the source of truth and anchors
   them; general and edge cases accompany it. External services are exercised
   through fixtures, never live credentials. An independent reviewer checks that
   the tests follow the original request; approved tests are frozen by digest and
   cannot be changed by the rest of the cycle.
4. **Due process.** Plan, independent plan review, implementation, deterministic
   checks (the frozen tests plus a fixed regression suite, in a networkless
   container), independent code review.
5. **Switch.** The accepted candidate becomes immutable image B. New work routes to
   B and the original request resumes on B.
6. **Done.** The resumed request completes. When the capability needs a service
   the user must authorize, B reports that the feature is ready and the exact
   authentication steps; the solution itself must already work and pass its tests.

## Bomb shelter

A is never modified. Candidates are built and tested in sandboxes without
network access or credentials, and B is an immutable image. If checks fail, B
fails to start, B crashes, or the resumed request fails, B is discarded, routing
returns to A, and the cycle restarts automatically from A with the recorded
failure as feedback. Retries continue until a candidate passes or the user stops
the run. Every step is kept in an append-only audit journal.

## Implementation

| Step | Code |
|---|---|
| Gap trigger | `TaskCoordinator(on_gap=...)` calls the hook with `gap_trigger.parse_gap_report` output for A's durable `blocked` report |
| Tests first | `tests_first.py`: requirements anchored on `original_request`, stdlib check scripts, independent review, frozen digest, fixed `regression.py` check |
| Loop | `loop.py` `SelfModificationLoop`: freeze tests once, then attempt → reset → feedback until finished or `stop()` |
| Due process | `loop.RoundDeveloper`: a fresh `ModificationRound` per attempt, `IntegratedDevelopment.run_until_ready` with the frozen checks, submit; earlier failures reach roles as `prior_attempts` |
| Switch | `loop.DeploymentSwitch`: build and verify B, stage, activate, register B's sandbox, hold the continuation, commit, release, await the resumed task |
| Bomb shelter | `DeploymentRouter.rollback`; a retry stages a fresh B and work bound to a voided B never serves |

| Wiring | `service.py` `install`: at startup (`RECOLLECT_SELFMOD_ENABLED=1`) builds and registers A, hands the coordinator its gap hook, cancels A's task on a gap and runs one loop at a time |

Not yet qualified live: a real run needs the model server and Docker, and the
authoring and role calls pin the modifier lane only on a three-slot server.

## Timing

No elapsed-time limits on agent work. Tool calls stay alive through the tool
host's progress keepalive, which the pinned OpenCode binary honors.
