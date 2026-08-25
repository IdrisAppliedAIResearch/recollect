"""The one place Recollect reaches into ``episodic``'s private modules.

Every private import in this project lives here, on purpose.

**Why reach in at all.** The library's ``context()`` returns a payload and a
``ContextReport`` of counts. Inside, it computes far more than it returns:
a cosine for every episode, a cluster assignment for every candidate, and
the coverage selector's full step-by-step arithmetic with the marginal gain
behind each choice. All of it is discarded at the return boundary. That
discarded detail is precisely what this harness exists to show, and the
research it deploys found its central results there - a similarity path
that never fires, a coverage selector starved by packing order. Counts
cannot show either.

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
from episodic._context import (
    _candidate_pool as candidate_pool,
)
from episodic._context import (
    _recency_window as recency_window,
)
from episodic._context import build_context
from episodic._packing import (
    DROP_POLICY,
    EMPTY_PAYLOAD_CHARS,
    pack_stm_payload,
)
from episodic._render import render_episode_element, render_stm_payload
from episodic._selection import (
    ClusterDiversitySelector,
    SelectionResult,
    additive_weight,
    deterministic_clusters,
    relevance_vector,
    select,
    vector,
)
from episodic._store import EpisodeStore

#: The library version this instrumentation was written against. Recorded in
#: every trace so a trace is interpretable years later, and compared on
#: startup so a silent upgrade is announced rather than discovered.
EXPECTED_LIBRARY_VERSION = "0.2.0"

LIBRARY_VERSION = episodic.__version__


def read_episodes(store: EpisodeStore) -> list[dict]:
    """Every stored episode in the order ``context()`` reads them.

    Ordering is load-bearing: the recency window is a tail slice of this
    list, and the selector's tie-breaks resolve on turn number and id. The
    library's own accessor is used rather than a reimplemented query so the
    two cannot drift apart silently.
    """
    return store._all_episodes()


def store_meta(store: EpisodeStore, key: str) -> str | None:
    """Read one row from the store's metadata table."""
    return store._meta_get(key)


__all__ = [
    "DROP_POLICY",
    "EMPTY_PAYLOAD_CHARS",
    "EXPECTED_LIBRARY_VERSION",
    "LIBRARY_VERSION",
    "ClusterDiversitySelector",
    "EpisodeStore",
    "EpisodicConfig",
    "SelectionResult",
    "additive_weight",
    "build_context",
    "candidate_pool",
    "deterministic_clusters",
    "pack_stm_payload",
    "read_episodes",
    "recency_window",
    "relevance_vector",
    "render_episode_element",
    "render_stm_payload",
    "select",
    "store_meta",
    "vector",
]
