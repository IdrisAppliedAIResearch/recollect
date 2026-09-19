"""Bring stored config pins forward to the timeline read policy.

Run with Recollect STOPPED, from the repository root:
uv run --no-sync python scripts/migrate_store_pins.py --apply

Every store carries the ``EpisodicConfig`` it was created under, and
``SessionStore._offer_config`` refuses to open one whose pin does not
round-trip against the installed library. Adding ``read_policy`` and
``timeline_threshold`` in episodic 0.3.0 broke that round-trip for every
store written before it, so each one needs its pin rewritten once.

Only the read policy changes. The mechanism constants a store was created
under are left exactly as they are: they describe the episodes already in
it, and rewriting them would make its history describe a computation that
never ran. The episodes and embeddings are never touched.

Each store is copied to ``<name>.pre-timeline.bak`` before it is written,
so a migration can be undone by restoring that file.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

from episodic import EpisodicConfig, EpisodicError

TARGET_POLICY = "timeline"
BACKUP_SUFFIX = ".pre-timeline.bak"


def _stored_pin(path: Path) -> str | None:
    connection = sqlite3.connect(str(path))
    try:
        row = connection.execute(
            "SELECT value FROM episodic_meta WHERE key = 'config'"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return None if row is None else str(row[0])


def _write_pin(path: Path, pin: str) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "UPDATE episodic_meta SET value = ? WHERE key = 'config'", (pin,)
        )
        connection.commit()
    finally:
        connection.close()


def _plan(path: Path) -> tuple[str, str | None]:
    """Return ``(verdict, new_pin)`` for one store without writing anything."""
    stored = _stored_pin(path)
    if stored is None:
        return "no config pin; skipped", None
    try:
        current = EpisodicConfig.from_json(stored)
    except EpisodicError as error:
        return f"UNPARSEABLE ({error}); left alone", None
    if current.read_policy == TARGET_POLICY and current.to_json() == stored:
        return "already on timeline", None
    return (
        f"{current.read_policy} -> {TARGET_POLICY}",
        replace(current, read_policy=TARGET_POLICY).to_json(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions",
        type=Path,
        default=Path("var/sessions"),
        help="Session directory root (default: var/sessions)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes. Without it this is a dry run.",
    )
    arguments = parser.parse_args()

    stores = sorted(arguments.sessions.glob("*/episodes.sqlite"))
    if not stores:
        print(f"No stores under {arguments.sessions}.")
        return 1

    changed = 0
    for store in stores:
        verdict, new_pin = _plan(store)
        print(f"{store.parent.name}  {verdict}")
        if new_pin is None:
            continue
        changed += 1
        if not arguments.apply:
            continue
        backup = store.with_name(store.name + BACKUP_SUFFIX)
        shutil.copy2(store, backup)
        _write_pin(store, new_pin)
        # Read it back through the same parser the app will use, so a
        # store that would fail to open is reported here and not on the
        # user's next turn.
        written = _stored_pin(store)
        reopened = EpisodicConfig.from_json(str(written))
        if reopened.to_json() != written or reopened.read_policy != TARGET_POLICY:
            shutil.copy2(backup, store)
            print(f"  FAILED verification; restored from {backup.name}")
            return 1
        print(f"  written; backup at {backup.name}")

    if not arguments.apply:
        print(f"\nDry run: {changed} store(s) would change. Re-run with --apply.")
    else:
        print(f"\n{changed} store(s) migrated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
