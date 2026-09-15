"""Frozen standalone stdlib helper for the container-local native API only.

Run by fixed path with isolated Python. The host owns its read-only mount;
neither request data nor environment chooses a destination or executable.
"""

import base64
import http.client
import json
import re
import sys
from urllib.parse import parse_qsl, urlencode

CHUNK_BYTES = 8192
FRAME_BYTES = 65536
BODY_BYTES = 16 * 1024 * 1024
REQUEST_BYTES = ((BODY_BYTES + 2) // 3) * 4 + 2 * FRAME_BYTES


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate relay key")
        result[key] = value
    return result


def validate_target(method, path):
    """Admit only NativeSession routes and its history pagination parameters."""
    if (type(method) is not str or type(path) is not str
            or len(path) > FRAME_BYTES
            or not path.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in path)
            or "#" in path or "\\" in path):
        raise ValueError("Invalid native method/path")
    route, separator, query = path.partition("?")
    fixed = {
        ("GET", "/global/health"), ("GET", "/config"),
        ("GET", "/project/current"), ("POST", "/session"),
    }
    match = re.fullmatch(r"/session/ses_[A-Za-z0-9]+/(message|summarize)", route)
    if (method, route) not in fixed and not (
        match and (method == "POST" or (method == "GET"
                                        and match[1] == "message"))
    ):
        raise ValueError("Forbidden native method/path")
    if not separator:
        return
    if method != "GET" or not match or match[1] != "message":
        raise ValueError("Native query is forbidden on this route")
    items = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                      encoding="utf-8", errors="strict")
    params = pairs(items)
    if (set(params) not in ({"limit"}, {"limit", "before"})
            or params["limit"] != "100" or urlencode(items) != query
            or ("before" in params and (
                not params["before"] or not params["before"].isascii()
                or any(ord(c) < 33 or ord(c) > 126 for c in params["before"])
            ))):
        raise ValueError("Invalid native pagination")


def emit(output, value):
    frame = (json.dumps(value, separators=(",", ":")) + "\n").encode("ascii")
    if len(frame) > FRAME_BYTES:
        raise ValueError("Native relay frame exceeds bound")
    output.write(frame)
    output.flush()


def relay(source, output):
    # Docker exec -i may keep container stdin open after the host closes its
    # pipe. Each invocation owns one canonical line, so EOF is not a delimiter.
    raw = source.readline(REQUEST_BYTES + 1)
    if len(raw) > REQUEST_BYTES:
        raise ValueError("Native relay request exceeds bound")
    if not raw.endswith(b"\n"):
        raise ValueError("Unterminated native relay request")
    value = json.loads(raw, object_pairs_hook=pairs)
    if type(value) is not dict or set(value) != {"method", "path", "body"}:
        raise ValueError("Invalid native relay request")
    canonical = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if raw != canonical:
        raise ValueError("Noncanonical native relay request")
    validate_target(value["method"], value["path"])
    if type(value["body"]) is not str:
        raise ValueError("Invalid native request bytes")
    body = base64.b64decode(value["body"], validate=True)
    if len(body) > BODY_BYTES or (value["method"] == "GET" and body):
        raise ValueError("Invalid native request body")
    # HTTPConnection neither consults proxy variables nor follows redirects.
    connection = http.client.HTTPConnection("127.0.0.1", 4096, timeout=None)
    try:
        connection.request(value["method"], value["path"], body=body or None,
                           headers={"Content-Type": "application/json",
                                    "Accept-Encoding": "identity"})
        with connection.getresponse() as response:
            emit(output, {"status": response.status,
                          "headers": response.getheaders()})
            total = 0
            while chunk := response.read1(CHUNK_BYTES):
                room = BODY_BYTES - total
                if room:
                    emit(output, {"chunk": base64.b64encode(chunk[:room]).decode()})
                total += len(chunk)
                if total > BODY_BYTES:
                    raise ValueError("Native response exceeds bound")
            if response.length not in (None, 0):
                raise ValueError("Native response ended before Content-Length")
            emit(output, {"end": True})
    finally:
        connection.close()


if __name__ == "__main__":
    # Defense in depth only: a same-UID worker could race this call. Relay bytes
    # are the worker's own API data and are never parsed as supervisor control.
    import ctypes

    ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE=0
    relay(sys.stdin.buffer, sys.stdout.buffer)
