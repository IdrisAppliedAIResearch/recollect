"""Google Calendar as connector #0: consent on Google's tab, calendar at home.

The demo service of the pivot: a gap that names calendar, appointments or
scheduling offers to connect Google Calendar instead of building anything.
The OAuth client is a Google web-application client whose redirect allowlist
contains the loopback port below; its JSON (``client_id``/``client_secret``,
optionally a different ``redirect_port``) is written by whoever registers it.

The exchanged token addresses the user's own calendar; :meth:`connection`
resolves the primary calendar's id and time zone so worker tools never have
to guess them.
"""

from __future__ import annotations

from .base import Connector

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
#: The scope to add when the user's grant lacks it; events covers reading too.
EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class GoogleCalendar(Connector):
    id = "google_calendar"
    name = "Google Calendar"
    description = "Read and write events on the connected Google account."
    # Deliberately narrow: bare "event" matched "event logging" and offered a
    # calendar for a gap that wanted none.
    keywords = ("calendar", "appointment", "appointments", "schedule",
                "scheduling", "agenda", "meeting", "booking", "reserve a slot")
    tools = ("calendar_list_events", "calendar_create_event")
    scopes = (EVENTS_SCOPE,)
    auth_url = AUTH_URL
    token_url = TOKEN_URL
    # Google hands out a refresh token only for an offline consent request,
    # and prompt=consent forces one even when the account already granted it.
    auth_params = {"access_type": "offline", "prompt": "consent"}

    async def connection(self, access_token: str, client) -> dict:
        response = await client.get(
            f"{CALENDAR_LIST_URL}/primary",
            headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code != 200:
            raise RuntimeError(
                f"Could not read the primary calendar (HTTP "
                f"{response.status_code}).")
        calendar = response.json()
        return {"calendar_id": calendar.get("id", "primary"),
                "calendar_time_zone": calendar.get("timeZone", "UTC")}
