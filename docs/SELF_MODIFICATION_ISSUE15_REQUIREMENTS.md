# Issue #15 requirements captured for the modifier's frozen input

Captured verbatim on 2026-09-15 from
[issue #15](https://github.com/IdrisAppliedAIResearch/recollect/issues/15)
(last updated 2026-09-09T18:26:39Z) with a read-only `gh issue view`. The
preregistration gives the modifier "issue #15 requirements as frozen in the
manifest"; the runtime manifest must pin this file's SHA-256. The original
protocol still limits the experiment to one calendar event, not the whole issue.

---

## Enable subagent calendar coordination, conflict resolution, and invitations

## User outcome

Delegate meeting coordination to the subagent while continuing to talk with the main chat. The assistant finds suitable times across multiple people, resolves conflicts within the user's instructions, and sends invitations when authorized. It reports the actual booking outcome with an event link.

## Outcome stories

- "Find 30 minutes with Alex and Sam next week, after 10 a.m." The assistant checks the connected calendars it can access, applies working hours and time zones, and offers suitable options. Missing availability is identified instead of treated as free time.
- "Book the first time that works and invite everyone." The assistant uses that authorization, verifies availability again, creates the event on the intended calendar, and confirms the attendees, local times, and event link.
- "I now have a conflict; move that meeting to Thursday." The assistant continues the same coordination task, proposes or makes the authorized change, and reports which attendees have accepted, declined, or not responded.
- "What is happening with that meeting?" Main chat answers from saved task state and remains available for unrelated conversation while coordination continues.

## Edge case outcomes

- Ambiguous contacts, calendars, dates, or time zones produce a focused clarification before an affected invitation is sent. Daylight-saving transitions and recurring-event exceptions preserve the intended local time and scope.
- If calendars change between proposal and booking, the assistant detects the conflict and finds another option or asks for the necessary decision. It does not silently move unrelated events.
- If an attendee's calendar cannot be read, the assistant can coordinate availability through an authorized communication channel; it distinguishes an invitation sent from a meeting accepted by everyone.
- A network timeout after creating or updating an event triggers reconciliation with the provider before retrying, avoiding duplicate invitations. Revoked access or provider outages leave a recoverable task with accurate status.
- Waiting for responses does not hold a model slot or busy worker. Restarts preserve the pending coordination and its external event references.

## Acceptance criteria

- [ ] Coordinate, create, reschedule, and cancel meetings through a selected calendar provider, with explicit account/calendar scope and verifiable event identifiers.
- [ ] Respect scheduling constraints, attendee identities, time zones, conflicts, and single-occurrence versus recurring-series changes.
- [ ] Honor authorization already given; ask only when a required decision or permission is missing. Provide a reviewable proposal when sending or modifying events has not been authorized.
- [ ] Support progress questions, steering, and cancellation through the existing subagent/main-chat workflow.
- [ ] Keep provider data, tool activity, and coordination updates in task/integration state. Only appropriate substantive conversation and user preferences enter conversational memory.
- [ ] Cover stale availability, missing access, partial attendee responses, duplicate retries, restart recovery, and time-zone transitions with deterministic provider fixtures.

## Implementation planning

Build on the continuous-task foundation in #10 and PR #14. Choose the first calendar provider, authentication/permission scopes, and change-notification mechanism before implementation; do not assume other people's private calendars are accessible. External calendar or message content must not override user instructions or grant permissions.

This is a future capability request; PR #14 does not implement it.
