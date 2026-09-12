# Subagent self-modification: Google Calendar preregistration v1

Protocol ID: `recollect-selfmod-calendar-v1`

Registration date: 2026-09-12. Status: protocol registered locally before
implementation or live trials. No baseline or candidate trial has run.
Repository baseline: `c984043d8fd161e83741f27c04413d13c4cdf27d`.
Branch: `selfmodifying-experiment`.

This experiment addresses a narrow outcome from
[issue #15](https://github.com/IdrisAppliedAIResearch/recollect/issues/15)
under the self-modification proposal in
[issue #22](https://github.com/IdrisAppliedAIResearch/recollect/issues/22).
It does not claim completion of either issue.

## Outcome story

The user is talking to Recollect's main chat and asks:

> On my connected Google test calendar, create a 30-minute event called
> "Self-modification review {experiment_id}" on {event_date} at 3:00 p.m.
> America/Chicago. Do not invite anyone. If the subagent lacks this capability,
> have it request its own modification, test the candidate, switch to the
> working version, and finish this same request. Tell me when the event is
> verified and give me its link.

Before the trial, substitute the frozen experiment ID and an absolute event
date: the next calendar day in America/Chicago after the recorded trial start
date. Resolve and record the expected start/end instants before submission.
Do not reinterpret the date if execution crosses midnight.

Main chat delegates to subagent A. A reports that it cannot create Google
Calendar events because it lacks the required integration. A's capability-gap
report starts a modification task through the controller. The modifier builds
candidate B, which passes independent checks. The controller switches new
subagent work to B and resumes the original task. B creates the event, and an
independent Google Calendar read confirms its fields and uniqueness. Main chat
returns the verified event link. The user does not reissue the request or
manually repair the candidate between these steps.

Throughout modification, main chat remains the control conversation and A can
continue unrelated research. Main chat, A, and the modifier must be capable of
simultaneous inference on the same GPU and model server. Research and modification
must not be serialized through a single background inference lane.

## Claim and scope

Primary hypothesis: starting from a subagent that cannot perform the requested
calendar action, the running system can generate and activate a modification of
that subagent and complete the original action without a human supplying the
missing implementation during the trial.

The demonstrated change must include executable calendar integration code
produced by the modifier during the recorded run. Enabling an already implemented
calendar tool, changing a prompt to claim success, creating an ICS file, or
linking the user to a manual booking page does not satisfy this hypothesis.

This is one prospective capability-acquisition demonstration. It cannot establish
a success rate, general self-improvement, better conversational quality, or
competence across the full calendar issue. Those require further experiments.

## Fixed separation of responsibilities

- Main chat supervises, relays progress, and remains outside the modification
  target. The controller owns test execution, routing, activation, and rollback.
- A and B are versioned subagent environments. The modifier has a separate
  conversation and build workspace. It edits the inactive subagent implementation,
  its tools, skills, and declared dependencies within a mechanically enforced
  boundary; it cannot edit the controller, evaluator, or their tests.
- The main application, `episodic`, shadow verification, embedding configuration,
  and production episode stores are outside the writable surface.
- Human-built scaffolding may provide isolated builds, general execution tools,
  Google authentication/transport, independent verification, and the A/B lifecycle.
  It must not provide a ready-made calendar action for B to merely enable.
- Google account authorization and test-calendar scope are established before
  baseline and remain constant across A and B. Credential values stay outside
  source, model transcripts, and published evidence. B inherits the same authorized
  identity through the fixed credential mechanism.
- The evaluator may contain calendar-specific checks and provider fixtures. Its
  implementation and credentials are unavailable to the modifier. The modifier
  may read the declared acceptance criteria and public API documentation.

## Registration and freeze sequence

1. Hash this file's exact UTF-8/LF bytes with SHA-256 and commit the protocol and
   checksum receipt. The checksum file is not part of its own hash. Record the
   resulting Git commit separately. Do not rewrite the registered commit.
2. Build only the scaffolding before the trial. Generic build, routing, fixture,
   and rollback checks may run during development; list them in the trial
   manifest. Do not rehearse the target self-modification task or pre-generate
   its calendar adapter and then call a later success the first attempt.
3. Before any target baseline or modifier inference, commit and hash a runtime
   manifest containing the fields below and the frozen evaluator. A protocol
   hash alone does not identify this later executable setup.
4. Run the primary attempt under those frozen artifacts. Preserve the original
   protocol and manifest. Any amendment gets a new version, SHA, reason, and
   timestamp; disclose whether relevant results were already observed.

A local SHA proves content identity, not independent publication time. A local
Git commit also does not supply independent timestamp evidence. Public
preregistration requires publishing the protocol and SHA to an external record
before the trial; public publication is not yet authorized or completed.
Do not describe this local registration as publicly timestamped.

The runtime manifest must identify:

- Protocol SHA-256; baseline, controller, evaluator, A, and modifier-harness Git
  commits and artifact hashes; clean-tree status or full captured diffs.
- Model weight identity/hash; model-server build and launch arguments; three-slot
  capacity, context allocation, generation settings, prompts, tool definitions,
  dependency locks, container image digests, and hardware/driver versions.
- Exact writable paths, mounted resources, network policy, credential mechanism,
  and controller-owned activation rules. No unresolved permission choices.
- Google test-calendar identifier held in the private record, a stable public
  alias, authorized operations, and evidence of the same access before and after.
- Experiment ID, UTC start window, event date and expected instants, exact rendered
  request, task identity scheme, evidence locations, and independent verifier.
- Deterministic provider fixtures and expected results; the fixed unrelated
  research workload and main-chat responsiveness probes used for concurrency.
- All prior relevant development probes, failures, and amendments. If an end-to-end
  target trial has already occurred, explicitly register this as a later attempt.

## Baseline and automatic trigger

Inspect and record A's actual tool inventory and artifact identity. An independent
read must find zero events matching the unique experiment marker before the
request. Submit the exact registered request through main chat once.

A must report a missing calendar capability through its normal task reporting
path. Wording need not match a fixed phrase, but the report must identify the
missing operation and request the modification path. The controller may use a
structured capability-gap message; it must not manufacture that report based
on the experiment ID or a calendar keyword.

Record that A has not created the event before B is activated. If A already
completes the action, the starting premise is falsified. If A fabricates success,
fails to identify the gap, or never triggers modification, record that outcome;
do not substitute a scripted refusal or manually launch the modifier and count
the result as primary success.

The modifier receives the original blocked request, baseline report, issue #15
requirements as frozen in the manifest, permitted source, and this protocol.
It receives no hidden solution patch or code from an earlier target attempt.

## Candidate checks and A/B switch

Candidate evaluation runs outside the modifier's writable environment. A remains
available while B is built and checked. Candidate evaluation uses the modification
lane or waits for it; it cannot take over main chat's or A's reserved lane.

Before activation, require all of these checks:

- B starts and registers its generated calendar tool; ordinary subagent research
  and report/steer/cancel behavior still passes the frozen regression checks.
- Fixture checks verify calendar selection, title, start/end/time zone, and absence
  of attendees; they also cover denied access, provider failure, and a timeout
  after creation followed by reconciliation without duplicate creation.
- A malformed request or provider rejection cannot produce a verified-success
  report. No modification of unrelated events is allowed.
- Artifact and write-boundary checks confirm only the declared subagent surface
  changed. Repository-required checks run against the candidate as applicable;
  test skipping, edited expectations, or weakened verification cannot qualify it.
- The switch and rollback mechanism passes fixture checks: failed B leaves A
  serving; successful activation selects the recorded B digest; switching back
  restores A's recorded digest. Existing A tasks retain A until they finish.

Activate the exact tested B artifact. Preserve the original task ID or an explicit
parent/continuation link. Automatically resume the original calendar request
without asking the user to restate it. Main chat and the controller must not
execute the missing calendar action on B's behalf.

An independent verifier must read the event from Google Calendar and list matching
events on the designated calendar. Pass requires exactly one matching event with
the frozen title, start, end, and time zone, no invited attendees, a provider event
ID, and a usable event link. A tool receipt or the model's own assertion is
insufficient. Replaying the same action identity must leave exactly one event.
Code rollback does not undo the event; external cleanup is a separate recorded
action on the verified test event only.

## Concurrency and responsiveness

Before the target attempt, run a declared generic concurrency check with requests
in all three lanes. Model-server evidence must show at least one second of
overlapping active generation across main chat, A, and the modifier on the same
server/GPU. Three open sockets or queued requests are not evidence of generation.
If this cannot be established, record an architecture feasibility failure and do
not silently change to serialized background work.

During the primary run, execute the frozen unrelated research workload on A while
the modifier works. Submit the main-chat probe "What is 17 plus 25?" at the first
observed modifier generation and at the first candidate evaluation. Each response
must finish within 30 seconds of submission and answer 42. Record queue time,
time to first token, completion time, model utilization, and peak GPU memory where
available. A reservation alone does not establish acceptable response latency.

## Attempts, stopping, and classification

Register one primary end-to-end attempt, with at most three submitted candidate
versions and 60 minutes from original request submission to verified completion.
Candidate rejection feedback may drive autonomous revisions within that attempt.
Count provider waits, builds, tests, switches, and retries against elapsed time.
Record all candidate patches and outcomes, including rejected candidates.

No human candidate edits, changed prompts, increased budgets, newly granted
permissions, or manual activation are allowed after submission for primary
success. An assisted continuation may be useful, but is reported separately.
Runtime crashes, provider outages, and authorization problems are recorded as
blocked or failed observations, never discarded from the attempt record.
Later reruns need new attempt IDs and must retain this attempt's outcome.

Primary PASS requires the baseline inability and automatic trigger, a modifier-
authored executable change, independent candidate gates, actual A/B activation,
verified Google event, duplicate prevention, concurrency/responsiveness criteria,
and completion within the registered limits. Failure of any requirement prevents
an end-to-end PASS even if other components work.

Report separately: baseline premise falsified; gap/trigger failure; modification
failure; evaluation failure; deployment failure; event verification failure;
concurrency/responsiveness failure; environment blocked; budget exhausted; human
assisted. Preserve overlapping failure labels where applicable.

## Evidence and reporting

Keep an append-only sequence of UTC and monotonic timestamps for the request,
gap report, modification task, candidate revisions, checks, activation, resumed
action, independent verification, probes, and cleanup. Include model input/output
and tool traces, source diffs, test output, A/B digests, routing receipts, provider
event evidence, and every failed attempt. Hash the final evidence manifest.

Store runtime schedules, tool data, and audit records outside episodic memory.
Retain intentional experiment evidence with documented paths; remove throwaway
scratch files. Publish only redacted records without tokens, account identifiers,
or unrelated calendar data; hash the released evidence separately from private
originals. Claim only the observed capability and report all deviations.

This document authorizes no service launch, Google account connection, live
calendar write, or public publication by itself. The current task is registration;
those actions belong to subsequent implementation and execution instructions.
