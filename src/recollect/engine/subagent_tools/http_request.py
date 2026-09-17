"""Arbitrary HTTP requests for the research model.

``http_request`` lets the model issue a full HTTP request (any common
method, custom headers, and a JSON or string body) and read back the
response. Every failure mode is returned as a JSON document rather than
raised, because a dead research turn is strictly worse than one that
learned the request failed. The client is created through the module-level
``httpx.AsyncClient`` so callers (and tests) can substitute a transport.
"""

from __future__ import annotations

import json

import httpx

from ..webtools import PublicWebTransport

#: Methods the tool will issue. Anything else is rejected up front so the
#: model cannot silently send a malformed request.
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}

_UA = "recollect-research/1.0 (local research agent)"


def _error(message: str) -> str:
    return json.dumps(
        {"tool": "http_request", "error": message}, ensure_ascii=False
    )


def _parse_body(raw: bytes):
    """Parse the response body as JSON when possible, else return its text."""
    if not raw:
        return ""
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


async def http_request(
    method: str,
    url: str,
    body: dict | list | str | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
) -> str:
    """Issue one HTTP request and return the response.

    ``body`` accepts a dict or list (serialized to JSON), a pre-serialized
    JSON string (sent verbatim), or None for a bodyless request. Returns a
    JSON document with ``status``, ``headers`` and ``body`` (the response
    parsed as JSON when possible, otherwise raw text). Non-2xx statuses and
    connection failures are returned as documents, not raised.
    """
    normalized_method = (method or "").strip().upper()
    if normalized_method not in _METHODS:
        return _error(
            f"unsupported method {method!r}; use one of "
            + ", ".join(sorted(_METHODS))
        )

    try:
        parsed_url = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return _error(f"invalid URL: {url!r}")
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
        return _error(
            f"only http/https URLs are allowed; got {url!r}"
        )

    if isinstance(body, (dict, list)):
        request_body: bytes | None = json.dumps(body, ensure_ascii=False).encode(
            "utf-8"
        )
    elif isinstance(body, str):
        request_body = body.encode("utf-8")
    elif body is None:
        request_body = None
    else:  # pragma: no cover - guarded by the type signature
        return _error(
            "body must be a dict, list, JSON string, or null"
        )

    request_headers = {
        "User-Agent": _UA,
        **(headers or {}),
    }

    try:
        async with httpx.AsyncClient(
            transport=PublicWebTransport(),
            trust_env=False,
            timeout=timeout,
            follow_redirects=False,
            headers=request_headers,
        ) as client:
            response = await client.request(
                normalized_method,
                url,
                content=request_body,
            )
    except httpx.HTTPError as error:
        return _error(
            f"{type(error).__name__}: {error}"
        )

    return json.dumps(
        {
            "tool": "http_request",
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": _parse_body(response.content),
        },
        ensure_ascii=False,
    )
