"""The one place Recollect reaches into ``episodic``'s private modules.

Every private import in this project lives here, on purpose.

**Why reach in at all.** The library's ``build_timeline_context`` returns a
payload and a ``ContextReport`` of counts. Inside, it computes a cosine
against the query for every stored episode and decides, per episode,
whether it cleared the relevance threshold, fell inside the recency
window, both, or neither. The report keeps only the totals; the
per-episode account is discarded at the return boundary. That discarded
detail is precisely what this harness exists to show - which episodes the
threshold admitted, which arrived only as continuity, and how close the
rest came to clearing it. Counts cannot show any of it.

**Why not fork the library instead.** Because the library is certified
behavior-preserving against committed artifacts, and a fork with print
statements in it is no longer that thing. Every number the research
published would need re-earning. The library stays untouched and pinned.

**What keeps this honest.** Recollect never *substitutes* its
reconstruction for the library's answer. It calls the library for the
authoritative payload and report, rebuilds the same pipeline alongside it
to capture the internals, and then asserts the two agree - byte-for-byte on
the payload, field-for-field on the report. If a library change breaks an
assumption here, the verification fails loudly on the next turn rather than
producing a plausible trace of something that did not happen. That check is
the contract; these imports are just how it is implemented.
"""

from __future__ import annotations

import episodic
from episodic._config import EpisodicConfig
from episodic._render import render_episode_element, render_stm_payload
from episodic._store import EpisodeStore
from episodic._timeline import build_timeline_context, cosine_scores

#: The library version this instrumentation was written against. Recorded in
#: every trace so a trace is interpretable years later, and compared on
#: startup so a silent upgrade is announced rather than discovered.
EXPECTED_LIBRARY_VERSION = "0.3.0"

LIBRARY_VERSION = episodic.__version__


def library_version_mismatch() -> str | None:
    """The upgrade notice, or ``None`` while the pinned version is installed.

    This exists because it did not. ``episodic`` is an editable install, so
    0.2.0 became 0.3.0 - a different read mechanism - under a running
    deployment, and nothing said so; the constant above was declared and
    exported but never once compared. Per-turn shadow verification catches
    behaviour that drifts, but only on a turn that exercises it. This is the
    cheap check that speaks at startup instead.
    """
    if LIBRARY_VERSION == EXPECTED_LIBRARY_VERSION:
        return None
    return (
        f"episodic {LIBRARY_VERSION} is installed but this instrumentation "
        f"is written against {EXPECTED_LIBRARY_VERSION}. Re-read "
        "recollect/engine/_internals.py before trusting a trace."
    )


def read_episodes(store: EpisodeStore) -> list[dict]:
    """Every stored episode in the order ``build_timeline_context`` reads them.

    Ordering is load-bearing: the timeline sorts eligible episodes by
    ``(turn_number, id)`` and takes the recency window as a tail slice of
    that. The library's own accessor is used rather than a reimplemented
    query so the two cannot drift apart silently.
    """
    return store._all_episodes()


def store_meta(store: EpisodeStore, key: str) -> str | None:
    """Read one row from the store's metadata table."""
    return store._meta_get(key)


__all__ = [
    "EXPECTED_LIBRARY_VERSION",
    "LIBRARY_VERSION",
    "EpisodeStore",
    "EpisodicConfig",
    "build_timeline_context",
    "cosine_scores",
    "library_version_mismatch",
    "read_episodes",
    "render_episode_element",
    "render_stm_payload",
    "store_meta",
]
