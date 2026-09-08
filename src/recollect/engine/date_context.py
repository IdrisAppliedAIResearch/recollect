"""Calendar context from the turn timestamp, independent of model knowledge."""

from __future__ import annotations

import json
from datetime import date

_WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)


def current_date_context(day: date) -> str:
    return (
        f"Current date (UTC): {day.isoformat()}. "
        f"Day of week (UTC): {_WEEKDAYS[day.weekday()]}. "
        "Interpret current, latest, and today relative to this date. Do not "
        "assume the current year from training knowledge or earlier conversation. "
        "Honor historical periods explicitly requested by the user. Check and "
        "report the dates covered by sources; older data is not automatically "
        "current data. Carry this date into research delegations."
    )


def research_date_context(day: date, user_request: str) -> str:
    return (
        current_date_context(day)
        + "\n\nThe original user request controls the time period. If the "
        "proposed research task adds a year the user did not request, use "
        "the original request and the current date.\nOriginal user request: "
        + json.dumps(user_request, ensure_ascii=False)
    )
