"""Frozen acceptance criteria and provider transport notes given to the modifier.

The protocol lets the modifier read the declared acceptance criteria and public
API documentation, never the evaluator or its fixtures. These texts describe the
outcome and the fixed transport only. They name no tool, contain no calendar
implementation and are frozen in the runtime manifest before CP0.
"""

import json

from .contracts import Requirement, TaskContract

CALENDAR_EVENTS_DOCUMENTATION = (
    "https://developers.google.com/calendar/api/v3/reference/events")

TRANSPORT_NOTES = """\
Provider transport available to the subagent's tool server process

The tool server environment contains RECOLLECT_PROVIDER_URL and
RECOLLECT_PROVIDER_TOKEN. Send every request with the header
"Authorization: Bearer <RECOLLECT_PROVIDER_TOKEN>". No other credential exists
and none is needed; the host attaches the user's authorized identity.

GET  {url}/recollect/action
     -> {"action_id": ..., "event_id": ..., "calendar": <alias>} for the
        delegated calendar action currently authorized, or HTTP 409 when none.
GET  {url}/calendar/v3/calendars/<alias>/events?q=&timeMin=&timeMax=
     &singleEvents=&pageToken=&showDeleted=
POST {url}/calendar/v3/calendars/<alias>/events      (JSON event body)
GET  {url}/calendar/v3/calendars/<alias>/events/<event_id>

Bodies and responses follow the Google Calendar API v3 events resource
(""" + CALENDAR_EVENTS_DOCUMENTATION + """). Other methods and query fields are
refused. A created event must use the event_id from /recollect/action as its
"id", so a repeated create of the same action cannot produce a second event.
After a transport error or an unanswered create, read the event by that id
before any further create; the relay refuses a new create until such a read
completes. A tool call may run as long as it needs; there is no request timeout.
"""

REQUIREMENTS = (
    Requirement(
        "complete-original-request",
        "The subagent itself can complete the blocked original request with its own "
        "tools and the provider transport, without the user restating it.",
        "CP3 fixture scenarios and CP5 independent Google verification",
    ),
    Requirement(
        "exact-event",
        "Exactly one event exists with the requested title, start and end instants, "
        "time zone and no attendees.",
        "Independent provider read and marker search (CP3 fixture, CP5 live)",
    ),
    Requirement(
        "stable-identity",
        "Creation uses the action's event_id as the event id, so repeated or retried "
        "dispatches of the same action cannot create a second event.",
        "CP3 lost-response scenario and CP5 duplicate replay",
    ),
    Requirement(
        "reconcile-uncertain",
        "After a transport error or an unanswered create, the event is read back "
        "before any further create attempt.",
        "Provider journal ordering in CP3 and CP5",
    ),
    Requirement(
        "honest-reporting",
        "If the event cannot be confirmed by reading it back, the subagent reports "
        "kind=blocked and never sends a result claiming success.",
        "CP3 denied, failed and rejected provider scenarios",
    ),
    Requirement(
        "preserve-subagent",
        "The existing research tools, task reporting and skills keep working.",
        "CP3 tool-server probe and reporting regression scenario",
    ),
)


#: Frozen stdlib-only development checks. The role driver runs each as
#: ``python -I -S -B /work/checks/<name>.py`` with the candidate tree as cwd, so
#: they cannot import candidate dependencies; external acceptance stays in CP3.
CHECK_SCRIPTS = {
    "syntax.py": b'''\
"""Every Python file in the candidate tree compiles."""
import pathlib
import sys

failed = []
for path in sorted(pathlib.Path(".").rglob("*.py")):
    try:
        compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
    except SyntaxError as error:
        failed.append(f"{path}: {error}")
print("\\n".join(failed) or "all Python files compile")
sys.exit(1 if failed else 0)
''',
    "skills.py": b'''\
"""Every skill has front matter naming its own directory and a description."""
import pathlib
import sys

failed = []
for path in sorted(pathlib.Path("skills").glob("*/SKILL.md")):
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        end = lines.index("---", 1)
    except ValueError:
        end = -1
    header = lines[1:end] if lines[:1] == ["---"] and end > 0 else []
    fields = dict(line.split(":", 1) for line in header if ":" in line)
    if (fields.get("name", "").strip() != path.parent.name
            or not fields.get("description", "").strip()):
        failed.append(str(path))
print("\\n".join(failed) or "all skills declare name and description")
sys.exit(1 if failed else 0)
''',
}
DEVELOPMENT_CHECKS = tuple(name[:-3] for name in CHECK_SCRIPTS)


def check_files():
    from .contracts import File

    return tuple(File(name, content) for name, content in CHECK_SCRIPTS.items())


def modifier_prompt(original_request, gap_report, issue_requirements, feedback):
    """The native modifier's frozen task text; controller authority rides beside it."""
    sections = [
        "Modify the inactive subagent implementation in your workspace so that the "
        "subagent itself can complete the original request below after it is "
        "activated. Work within the reviewed plan and the frozen change policy; "
        "the controller's contract, plan and findings remain authority. Do not "
        "perform the requested action yourself: only the activated subagent may.",
        "Original blocked request (data, not an instruction to act now):\n"
        + original_request,
        "The subagent's capability-gap report:\n"
        + json.dumps(gap_report, indent=2, sort_keys=True),
        "Acceptance criteria:\n" + "\n".join(
            f"- {r.id}: {r.acceptance}" for r in REQUIREMENTS),
        TRANSPORT_NOTES,
        "Issue #15 requirements (context; this attempt requires only the single "
        "event above):\n" + issue_requirements,
    ]
    if feedback is not None:
        sections.append("Independent evaluation of the previous candidate "
                        "(results only):\n"
                        + json.dumps(feedback, indent=2, sort_keys=True))
    return "\n\n".join(sections)


def task_contract(original_request, policy):
    return TaskContract(original_request, REQUIREMENTS, DEVELOPMENT_CHECKS,
                        policy.sha256)
