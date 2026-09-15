"""Host-owned incremental archive of committed native events, never a receipt.

The trusted async reader takes a request dict and returns canonical JSON bytes
from native_history_reader. The journal lives outside all worker mounts. Capture
before each dispatch; call finalize only after the runtime has stopped writers.
Capture checks the incremental boundary, not immutability of earlier rows.
Finalize rereads the entire committed prefix against verified host identities.
Neither method establishes upstream stop or projection/event agreement. Native
pruning updates remain exact events, including the original tool output; SSE
streaming deltas are not durable EventV2 history and are not claimed here.
"""

import asyncio
import base64
import binascii

from .contracts import File, Snapshot
from .journal import EMPTY_SNAPSHOT, IntegrityError
from .native import _durable, _settle
from .native_history_reader import (
    MAX_PAGE_BYTES,
    MAX_ROW_BYTES,
    canonical,
    digest,
    strict_json,
    validate_request,
    validate_row,
)

COMPLETENESS = "committed_native_events"
PAGE_KEYS = {
    "session_id", "owner_id", "head", "watermark", "after", "complete",
    "prior_row_sha256", "last_event_sha256", "rows", "fragment",
}


def iter_event_rows(records, *, session_id):
    """Yield exact rows from verified Journal records, one bounded row at a time.

    Only identities committed by native_history_event are yielded. Failure
    prefixes and an unfinished fragment remain evidence, never accepted events.
    Call journal.verify() before supplying its records; this is not a standalone
    journal verifier. Projection/continuation validation belongs to the caller.
    """
    pending = {}
    partial = bytearray()
    sequence = -1
    for record in records:
        value = record.value
        data = value["data"]
        if data.get("native_session_id") != session_id:
            continue
        if value["kind"] == "native_history_page":
            page = strict_json(record.files.files[0].content)
            if type(page) is not dict or set(page) != PAGE_KEYS:
                continue
            for row in page["rows"]:
                pending[row["seq"]] = row
            fragment = page["fragment"]
            if fragment is not None:
                if fragment["offset"] != len(partial):
                    raise IntegrityError("Archived native fragment gap")
                partial.extend(base64.b64decode(fragment["data"], validate=True))
                if len(partial) > MAX_ROW_BYTES:
                    raise IntegrityError("Archived native row exceeds byte bound")
                if len(partial) == fragment["total_bytes"]:
                    row = strict_json(bytes(partial))
                    pending[row["seq"]] = row
                    partial.clear()
        elif value["kind"] == "native_history_event":
            sequence += 1
            row = pending.pop(sequence, None)
            encoded = validate_row(row, session_id, sequence)
            if (data["seq"] != sequence or data["event_id"] != row["id"]
                    or data["event_sha256"] != digest(encoded)):
                raise IntegrityError("Archived native event identity mismatch")
            yield row


class NativeHistory:
    """One session, one trusted reader, one host journal; failure poisons reuse.

    There is no page-count, total-history, elapsed-time or work quota. Journal
    records contain bounded pages and per-sequence identities, never one growing
    history snapshot. Cancellation retains ownership until durable writes settle.
    """

    def __init__(self, session_id, journal, *, reader):
        validate_request(session_id, -1, None, None)
        if not callable(reader):
            raise ValueError("An explicit trusted async native reader is required")
        self.session_id, self.journal, self.reader = session_id, journal, reader
        self._sequence = -1
        self._digest = self._owner = self._head = self._watermark = None
        self._owner_seen = False
        self._busy = self._failed = self._final = False
        self._complete = False

    @property
    def durable_sequence(self):
        return self._sequence

    @property
    def durable_head(self):
        """Last locally archived native identity, distinct from the observed head."""
        return {"seq": self._sequence, "sha256": self._digest}

    @property
    def poisoned(self):
        return self._failed

    @property
    def final_metadata(self):
        """Coverage and validation scope, not a continuing immutability claim."""
        return {
            "session_id": self.session_id, "owner_id": self._owner,
            "durable_sequence": self._sequence,
            "last_event_sha256": self._digest, "observed_head": self._head,
            "watermark": self._watermark,
            "complete": self._complete and not self._failed,
            "final": self._final and not self._failed,
            "history_completeness": COMPLETENESS,
            "verification_scope": (
                "full_prefix" if self._final and not self._failed
                else "incremental_boundary"),
            "prefix_revalidated_through": (
                self._watermark if self._final and not self._failed else None),
            "execution_receipt": False,
        }

    async def _record(self, kind, data, raw=None):
        files = (EMPTY_SNAPSHOT if raw is None
                 else Snapshot((File("page.json", raw),)))
        return await _durable(self.journal.append, kind,
                              {"native_session_id": self.session_id, **data}, files)

    def _page(self, raw, request, watermark):
        page = strict_json(raw)
        if type(page) is not dict or set(page) != PAGE_KEYS or canonical(page) != raw:
            raise IntegrityError("Malformed or noncanonical native history page")
        if (page["session_id"] != self.session_id
                or page["owner_id"] is not None and type(page["owner_id"]) is not str
                or self._owner_seen and page["owner_id"] != self._owner):
            raise IntegrityError("Native history ownership mismatch")
        for name in ("head", "watermark", "after"):
            minimum = -1 if name == "after" else 0
            if (type(page[name]) is not int
                    or not minimum <= page[name] <= 2**63 - 1):
                raise IntegrityError("Invalid native history sequence")
        if (page["head"] < page["watermark"]
                or self._head is not None and page["head"] < self._head
                or watermark is not None and page["watermark"] != watermark
                or watermark is None and page["watermark"] != page["head"]
                or not request["after"] <= page["after"] <= page["watermark"]):
            raise IntegrityError("Native head rollback or watermark mismatch")
        if (page["prior_row_sha256"] != request["last_event_sha256"]
                or type(page["complete"]) is not bool
                or page["complete"] != (page["after"] == page["watermark"])
                or type(page["rows"]) is not list):
            raise IntegrityError("Native boundary identity or completeness mismatch")
        return page

    async def _event(self, row, encoded, pages):
        await self._record("native_history_event", {
            "seq": row["seq"], "event_id": row["id"],
            "event_sha256": digest(encoded), "pages": pages,
        })
        # Both the raw bytes and sequence identity have passed journal readback.
        self._sequence, self._digest = row["seq"], digest(encoded)

    def _verify_event(self, row, encoded, expected):
        archived = next(expected, None)
        if (archived is None or type(archived["seq"]) is not int
                or archived["seq"] != row["seq"]
                or archived["event_id"] != row["id"]
                or archived["event_sha256"] != digest(encoded)):
            raise IntegrityError(
                f"Native archived prefix identity mismatch at sequence {row['seq']}")

    async def capture(self, *, final=False):
        """Archive through the first snapshot's head, then recheck its boundary.

        Return that integer watermark. Ordinary capture permits later appends
        and does not establish that older captured rows are still unchanged.
        With final=True, first capture new events, then reread 0..watermark in
        bounded pages against verified journal identities. Every observed head
        must equal the watermark; caller-owned writer stop is required.
        """
        if self._busy or self._failed or self._final:
            raise IntegrityError("Native history is busy, failed or finalized")
        self._busy, self._complete = True, False
        watermark = None
        partial = bytearray()
        partial_digest, partial_total = None, None
        partial_pages = []
        raw = b""
        cursor, last_digest = self._sequence, self._digest
        verifying, expected = False, None
        try:
            while True:
                request = {"session_id": self.session_id, "after": cursor,
                           "last_event_sha256": last_digest}
                if watermark is not None:
                    request["through"] = watermark
                if partial:
                    request.update(offset=len(partial), row_sha256=partial_digest)
                raw = b""
                raw = await self.reader(request)
                if type(raw) is not bytes:
                    raise IntegrityError("Trusted native reader must return bytes")
                if len(raw) > MAX_PAGE_BYTES:
                    raise IntegrityError("Native reader page exceeds 1 MiB")
                kind = ("native_history_verification_page" if verifying
                        else "native_history_page")
                receipt = await self._record(kind, {
                    "request": request,
                }, raw)
                page_ref = {"sequence": receipt.anchor.sequence,
                            "sha256": receipt.anchor.sha256}
                page = self._page(raw, request, watermark)
                watermark = page["watermark"]
                self._watermark, self._owner = watermark, page["owner_id"]
                self._owner_seen = True
                self._head = page["head"]
                if final and page["head"] != watermark:
                    raise IntegrityError("Native writers advanced during final capture")
                fragment = page["fragment"]
                if fragment is not None:
                    if (page["rows"] or page["after"] != cursor
                            or page["last_event_sha256"] != last_digest
                            or page["complete"] or type(fragment) is not dict
                            or set(fragment) != {"seq", "offset", "total_bytes",
                                                  "row_sha256", "data"}):
                        raise IntegrityError("Malformed native row fragment")
                    if (type(fragment["seq"]) is not int
                            or fragment["seq"] != cursor + 1
                            or type(fragment["offset"]) is not int
                            or fragment["offset"] != len(partial)
                            or type(fragment["total_bytes"]) is not int
                            or not 0 < fragment["total_bytes"] <= MAX_ROW_BYTES
                            or type(fragment["data"]) is not str):
                        raise IntegrityError("Invalid native fragment bounds")
                    if partial and (fragment["row_sha256"] != partial_digest
                                    or fragment["total_bytes"] != partial_total):
                        raise IntegrityError("Native fragment identity changed")
                    partial_digest = fragment["row_sha256"]
                    partial_total = fragment["total_bytes"]
                    try:
                        chunk = base64.b64decode(fragment["data"], validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise IntegrityError("Invalid fragment encoding") from exc
                    if not chunk or len(partial) + len(chunk) > partial_total:
                        raise IntegrityError("Native fragment overflow or no progress")
                    partial.extend(chunk)
                    partial_pages.append(page_ref)
                    if len(partial) == partial_total:
                        encoded = bytes(partial)
                        row = strict_json(encoded)
                        if (validate_row(row, self.session_id, cursor + 1)
                                != encoded or digest(encoded) != partial_digest):
                            raise IntegrityError("Reassembled native row mismatch")
                        if verifying:
                            self._verify_event(row, encoded, expected)
                        else:
                            await self._event(row, encoded, partial_pages)
                        cursor, last_digest = row["seq"], digest(encoded)
                        partial.clear()
                        partial_pages = []
                else:
                    if partial:
                        raise IntegrityError("Native reader abandoned partial row")
                    # Validate the entire page before accepting any row identity.
                    encoded_rows = [validate_row(row, self.session_id,
                                                 cursor + i + 1)
                                    for i, row in enumerate(page["rows"])]
                    last = digest(encoded_rows[-1]) if encoded_rows else last_digest
                    if (page["after"] != cursor + len(encoded_rows)
                            or page["last_event_sha256"] != last):
                        raise IntegrityError("Native page sequence/digest mismatch")
                    if not encoded_rows:
                        if not page["complete"]:
                            raise IntegrityError("Native reader made no progress")
                        # This separate read verifies the last durably held row.
                        if final and not verifying:
                            records = await _durable(self.journal.verify)
                            # Iterate trusted per-sequence metadata without
                            # flattening archived native pages into a snapshot.
                            expected = (
                                value["data"] for record in records
                                if (value := record.value)["kind"]
                                == "native_history_event"
                                and value["data"].get("native_session_id")
                                == self.session_id
                            )
                            verifying = True
                            cursor, last_digest = -1, None
                            continue
                        if verifying and (next(expected, None) is not None
                                          or cursor != self._sequence
                                          or last_digest != self._digest):
                            raise IntegrityError(
                                "Native archived prefix length mismatch")
                        break
                    for row, encoded in zip(page["rows"], encoded_rows, strict=True):
                        if verifying:
                            self._verify_event(row, encoded, expected)
                        else:
                            await self._event(row, encoded, [page_ref])
                        cursor, last_digest = row["seq"], digest(encoded)
            metadata = {
                **self.final_metadata, "complete": True, "final": final,
                "verification_scope": (
                    "full_prefix" if final else "incremental_boundary"),
                "prefix_revalidated_through": watermark if final else None,
            }
            await self._record("native_history_final" if final else
                               "native_history_captured", metadata)
            self._complete, self._final = True, final
            return watermark
        except BaseException as exc:
            self._failed = True
            prefix = getattr(exc, "raw_prefix", b"") or raw
            if type(prefix) is not bytes:
                prefix = b""
            await _settle(asyncio.create_task(self._record(
                "native_history_failure", {
                    **self.final_metadata, "failure": type(exc).__name__,
                    "verification_phase": verifying,
                    "raw_prefix_truncated": len(prefix) > MAX_PAGE_BYTES,
                }, prefix[:MAX_PAGE_BYTES],
            )))
            raise
        finally:
            self._busy = False

    async def finalize(self):
        """Capture and revalidate every event after caller-owned writer shutdown.

        Return the final watermark only after every canonical row identity and
        the fixed head agree. Verification pages never replace accepted events.
        The caller must keep writers frozen throughout this pass.
        """
        return await self.capture(final=True)
