"""The ntfy channel: one POST, one notification (ntfy.sh first).

Config, in the user-dropped credential file:
``{"url": "https://ntfy.sh/<topic>"}`` — the topic URL is both the send
target and a read credential: anyone who knows it reads every reminder
sent. That cost is stated in the seam plan; sensitive text stays
on-device (the Notice path) instead.

ntfy's API is a bare POST: the body is the message, an optional title
header decorates it. Nothing else about this channel is clever.
"""

from __future__ import annotations

import httpx

from .error import ChannelError

_TIMEOUT = 20.0


async def send(config: dict, text: str, *, transport=None) -> None:
    url = config.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ChannelError(
            "the ntfy config needs a url starting with https:// or http://")
    async with httpx.AsyncClient(timeout=_TIMEOUT, trust_env=False,
                                 transport=transport) as client:
        try:
            response = await client.post(url, content=text.encode("utf-8"))
        except httpx.HTTPError as error:
            raise ChannelError(
                f"ntfy could not be reached ({type(error).__name__})") from None
    if response.status_code >= 300:
        # Status only: a provider body has no business in a job record.
        raise ChannelError(f"ntfy refused the message (HTTP "
                           f"{response.status_code})")
