"""The verified shadow trace.

One turn of retrieval, computed twice.

The **authority** is the library: ``build_timeline_context`` runs untouched
and returns the payload the model will actually see, plus its
``ContextReport``. That result is what ships. Nothing in this module can
change it.

The **shadow** is an independent reconstruction of the same selection. It
takes one primitive from the library - the cosine of every stored episode
against the query - and from there re-derives the eligible horizon, the
continuity slice, the threshold test and the chronological union on its
own. It records what the report throws away: each episode's cosine, its
distance from the threshold, and which condition (relevance, continuity,
both, or neither) decided it.

Reconstructing the *ordering* matters as much as the selection. The
continuity slice is a tail of the source ordering, so accepting the
store's order on faith would make the shadow agree with the library by
construction and prove nothing. It is re-derived from ``(turn_number, id)``
here.

Then the two are compared. The shadow's payload must equal the authority's
payload character for character, and every field it derives must equal the
authority's report - including the fields the timeline holds constant,
because "nothing was dropped" is a claim worth checking rather than
assuming, and an unchecked constant is exactly how a mechanism change slips
past. If they disagree the trace is marked untrustworthy and - by default -
the turn raises, because a trace that quietly describes a computation that
did not happen is worse than having no trace at all.

The cost of computing twice is one extra pass over the store with no
embedding calls, since the query vector is computed once and passed to
both.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence

import numpy as np

from ..trace import (
    CandidateTrace,
    CeilingTrace,
    ContextBlockTrace,
    ReportTrace,
    SelectionPath,
    TimelineDetail,
    VerificationTrace,
)
from ._internals import (
    LIBRARY_VERSION,
    EpisodicConfig,
    build_timeline_context,
    cosine_scores,
    render_episode_element,
    render_stm_payload,
)

PREVIEW_CHARS = 240


class TraceDivergenceError(RuntimeError):
    """The instrumented reconstruction did not reproduce the library.

    Raised rather than logged. A divergence means the harness's account of
    what happened is wrong, and every number derived from it - cosines,
    margins, attributions - is suspect. The correct response is to stop,
    not to serve a confident-looking trace of a different computation.
    """


class RetrievalResult:
    """The authoritative context block plus its verified trace fragments."""

    def __init__(
        self,
        *,
        payload: str,
        report: ReportTrace,
        candidates: list[CandidateTrace],
        timeline: TimelineDetail,
        ceiling: CeilingTrace,
        context_block: ContextBlockTrace,
        verification: VerificationTrace,
    ) -> None:
        self.payload = payload
        self.report = report
        self.candidates = candidates
        self.timeline = timeline
        self.ceiling = ceiling
        self.context_block = context_block
        self.verification = verification


def _selection(
    episodes: Sequence[dict],
    scores,
    config: EpisodicConfig,
    through_turn: int | None,
    anchor_turn: int | None,
) -> tuple[list[int], list[int], list[int], tuple[int, ...], int | None]:
    """The timeline's selection, re-derived from primitives.

    Returns ``(eligible, recent, relevant, selected, anchor_index)`` as
    indices into ``episodes``. The source ordering is re-derived from
    ``(turn_number, id)`` rather than taken from the store: the continuity
    slice is a tail of that ordering, so accepting it on faith would make
    the shadow agree with the library by construction.
    """
    ids = [str(episode["id"]) for episode in episodes]
    turns = [int(episode["turn_number"]) for episode in episodes]
    eligible = sorted(
        (
            index
            for index in range(len(episodes))
            if through_turn is None or turns[index] <= through_turn
        ),
        key=lambda index: (turns[index], ids[index]),
    )
    window = config.recency_window_n
    recent = eligible[-window:] if window else []
    relevant = [
        index for index in eligible if scores[index] >= config.timeline_threshold
    ]
    chosen = set(recent) | set(relevant)
    anchor_index = None
    if anchor_turn is not None:
        anchor_index = turns.index(anchor_turn)
        chosen.add(anchor_index)
    selected = tuple(index for index in eligible if index in chosen)
    return eligible, recent, relevant, selected, anchor_index


def _ceiling_withholds(
    episodes: Sequence[dict],
    scores,
    config: EpisodicConfig,
    ceiling_chars: int | None,
    through_turn: int | None,
    anchor_turn: int | None,
) -> set[int]:
    """Which episodes the deployment ceiling keeps away from the library.

    A deviation from the mechanism, taken for hardware and recorded as
    such - see ``trace.CeilingTrace``. It runs *before* the authority, so
    the library and the shadow are handed the identical set and the
    byte-for-byte check still means what it says.

    Continuity and an explicit anchor are never withheld: losing what was
    just said, to make room for something older that merely scored well,
    would be a worse failure than exceeding the ceiling. So a recency
    window that alone exceeds the ceiling is delivered anyway, and the
    trace reports it.

    Relevance-qualified episodes are shed lowest-cosine first, one at a
    time against an exact re-render, because the serialized cost of a set
    is not the sum of its parts.
    """
    if not ceiling_chars or ceiling_chars <= 0:
        return set()

    _, recent, _, selected, anchor_index = _selection(
        episodes, scores, config, through_turn, anchor_turn
    )
    if len(render_stm_payload([], [episodes[i] for i in selected])) <= ceiling_chars:
        return set()

    protected = set(recent)
    if anchor_index is not None:
        protected.add(anchor_index)
    # Weakest first, ties broken by index so the choice is deterministic.
    sheddable = sorted(
        (index for index in selected if index not in protected),
        key=lambda index: (scores[index], index),
    )

    withheld: set[int] = set()
    surviving = list(selected)
    for index in sheddable:
        rendered = len(render_stm_payload([], [episodes[i] for i in surviving]))
        if rendered <= ceiling_chars:
            break
        withheld.add(index)
        surviving = [i for i in surviving if i != index]
    return withheld


def retrieve_with_trace(
    *,
    episodes: Sequence[dict],
    query_embedding: np.ndarray,
    config: EpisodicConfig,
    ceiling_chars: int | None = None,
    through_turn: int | None = None,
    anchor_turn: int | None = None,
    strict: bool = True,
) -> RetrievalResult:
    """Build the context block and a proven-faithful account of how.

    ``ceiling_chars`` is the deployment's hardware ceiling, applied before
    the authority runs so both computations are handed the same episodes.
    ``None`` or ``0`` takes the library's uncapped behaviour unmodified.

    ``strict=False`` records a divergence in the trace instead of raising.
    It exists for offline analysis of a known-broken pairing; the server
    runs strict.
    """
    store = list(episodes)
    store_ids = [str(episode["id"]) for episode in store]
    store_scores = cosine_scores(store, query_embedding)
    threshold = config.timeline_threshold

    # -- 0. the deployment ceiling picks what the library is shown ------
    withheld = _ceiling_withholds(
        store, store_scores, config, ceiling_chars, through_turn, anchor_turn
    )
    keep = [index for index in range(len(store)) if index not in withheld]
    admitted = [store[index] for index in keep]
    admitted_scores = store_scores[keep]

    # The full-store classification, for the candidate rows: a withheld
    # episode still has to report that it cleared the threshold. Shedding
    # never touches the continuity tail, so the store-level and
    # library-level windows are the same slice.
    (
        store_eligible,
        store_recent,
        store_relevant,
        _store_selected,
        store_anchor,
    ) = _selection(store, store_scores, config, through_turn, anchor_turn)

    # -- 1. the authority, untouched, on the admitted set ---------------
    authority_payload, authority_report = build_timeline_context(
        episodes=list(admitted),
        query_embedding=query_embedding,
        config=config,
        through_turn=through_turn,
        anchor_turn=anchor_turn,
    )

    # -- 2. the shadow, instrumented ------------------------------------
    shadow_started = time.perf_counter()
    ids = [str(episode["id"]) for episode in admitted]
    eligible, recent, relevant, selected, _anchor = _selection(
        admitted, admitted_scores, config, through_turn, anchor_turn
    )

    recent_set = set(recent)
    relevant_set = set(relevant)
    selected_records = [admitted[index] for index in selected]
    shadow_payload = render_stm_payload([], selected_records)

    recent_ids = tuple(ids[index] for index in recent)
    relevant_ids = tuple(ids[index] for index in relevant)
    selected_ids = tuple(ids[index] for index in selected)
    overlap_ids = tuple(ids[index] for index in eligible if index in
                        (relevant_set & recent_set))
    semantic_count = len(relevant_set - recent_set)

    shadow_latency_ms = (time.perf_counter() - shadow_started) * 1_000.0

    # -- 3. verification ------------------------------------------------
    verification = _verify(
        authority_payload=authority_payload,
        shadow_payload=shadow_payload,
        authority_report=authority_report,
        recent_ids=recent_ids,
        selected_ids=selected_ids,
        semantic_count=semantic_count,
        eligible_count=len(eligible),
        pool_size=len(admitted),
        threshold=threshold,
        through_turn=through_turn,
        anchor_turn=anchor_turn,
        shadow_latency_ms=shadow_latency_ms,
    )
    if strict and not verification.trustworthy:
        raise TraceDivergenceError(
            "The instrumented reconstruction did not reproduce the library's "
            "output for this turn. The trace cannot be trusted and was not "
            "served.\n"
            f"  payload identical: {verification.payload_identical}\n"
            f"  authority sha256:  {verification.authority_payload_sha256}\n"
            f"  shadow sha256:     {verification.shadow_payload_sha256}\n"
            f"  mismatched fields: {verification.mismatched_fields or 'none'}\n"
            f"  episodic version:  {verification.library_version}\n"
            "This usually means the library changed under a pinned "
            "instrumentation. Re-read recollect/engine/_internals.py."
        )

    # -- 4. assemble the trace fragments --------------------------------
    return RetrievalResult(
        payload=authority_payload,
        report=_trace_report(authority_report),
        candidates=_trace_candidates(
            episodes=store,
            scores=store_scores,
            threshold=threshold,
            eligible=set(store_eligible),
            recent=set(store_recent),
            relevant=set(store_relevant),
            delivered=set(selected_ids),
            withheld=withheld,
            anchor_index=store_anchor,
        ),
        timeline=TimelineDetail(
            read_policy=authority_report.read_policy,
            relevance_threshold=threshold,
            recency_window_n=config.recency_window_n,
            eligible_count=len(eligible),
            relevant_ids=list(relevant_ids),
            recent_ids=list(recent_ids),
            selected_ids=list(selected_ids),
            overlap_ids=list(overlap_ids),
            relevance_only_count=semantic_count,
            through_turn=through_turn,
            anchor_turn=anchor_turn,
        ),
        ceiling=CeilingTrace(
            ceiling_chars=ceiling_chars or None,
            engaged=bool(withheld),
            store_episodes=len(store),
            considered_episodes=len(admitted),
            withheld_ids=[store_ids[index] for index in sorted(withheld)],
            withheld_chars=sum(
                len(render_episode_element(store[index])) for index in withheld
            ),
        ),
        context_block=ContextBlockTrace(
            payload=authority_payload,
            chars=len(authority_payload),
            sha256=_sha256(authority_payload),
            recent_episode_count=len(recent_ids),
            retrieved_episode_count=len(selected_ids),
        ),
        verification=verification,
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _verify(
    *,
    authority_payload: str,
    shadow_payload: str,
    authority_report,
    recent_ids: tuple[str, ...],
    selected_ids: tuple[str, ...],
    semantic_count: int,
    eligible_count: int,
    pool_size: int,
    threshold: float,
    through_turn: int | None,
    anchor_turn: int | None,
    shadow_latency_ms: float,
) -> VerificationTrace:
    """Compare the reconstruction against the library, field by field.

    Every field is the shadow's independently re-derived value against the
    authority's report. Latency is the one field excluded, for the same
    reason it always was: wall time is not byte-reproducible.

    The block at the end asserts the fields the timeline holds constant.
    They are not carried in ``ReportTrace`` precisely because they never
    vary - which is also why they are worth asserting. If a later library
    starts dropping episodes on this path, this is what says so.
    """
    mismatches: list[str] = []

    def check(name: str, shadow_value, authority_value) -> None:
        if shadow_value != authority_value:
            mismatches.append(
                f"{name}: shadow={shadow_value!r} authority={authority_value!r}"
            )

    check("chars_delivered", len(shadow_payload), authority_report.chars_delivered)
    check("chars_wanted", len(shadow_payload), authority_report.chars_wanted)
    check(
        "episodes_delivered",
        len(selected_ids),
        authority_report.episodes_delivered,
    )
    check("stm_count", len(recent_ids), authority_report.stm_count)
    check("k_count", semantic_count, authority_report.k_count)
    check("recency_count", len(recent_ids), authority_report.recency_count)
    check("semantic_count", semantic_count, authority_report.semantic_count)
    check("pool_size", pool_size, authority_report.pool_size)
    check("recent_ids", recent_ids, tuple(authority_report.recent_ids))
    check("selected_ids", selected_ids, tuple(authority_report.selected_ids))
    check("eligible_count", eligible_count, authority_report.eligible_count)
    check("recency_additive", True, authority_report.recency_additive)
    check("read_policy", "timeline", authority_report.read_policy)
    check(
        "relevance_threshold", threshold, authority_report.relevance_threshold
    )
    check("through_turn", through_turn, authority_report.through_turn)
    check("anchor_turn", anchor_turn, authority_report.anchor_turn)
    check(
        "retrieval_chars_delivered",
        len(shadow_payload),
        authority_report.retrieval_chars_delivered,
    )

    # Structurally constant on the timeline path - asserted, not stored.
    check("episodes_dropped", 0, authority_report.episodes_dropped)
    check("truncated", False, authority_report.truncated)
    check("dropped_ids", (), tuple(authority_report.dropped_ids))
    check("drop_policy", "none", authority_report.drop_policy)
    check("budget_chars", None, authority_report.budget_chars)
    check("retrieval_budget_chars", None, authority_report.retrieval_budget_chars)
    check("coverage_count", 0, authority_report.coverage_count)
    check("aspect_count", 0, authority_report.aspect_count)
    check(
        "returned_semantic_count",
        0,
        authority_report.returned_semantic_count,
    )
    check("aspect_enabled", False, authority_report.aspect_enabled)

    return VerificationTrace(
        payload_identical=shadow_payload == authority_payload,
        report_fields_identical=not mismatches,
        authority_payload_sha256=_sha256(authority_payload),
        shadow_payload_sha256=_sha256(shadow_payload),
        mismatched_fields=mismatches,
        library_version=LIBRARY_VERSION,
        shadow_latency_ms=shadow_latency_ms,
    )


# ---------------------------------------------------------------------------
# Trace fragment builders
# ---------------------------------------------------------------------------


def _trace_candidates(
    *,
    episodes: Sequence[dict],
    scores,
    threshold: float,
    eligible: set[int],
    recent: set[int],
    relevant: set[int],
    delivered: set[str],
    withheld: set[int],
    anchor_index: int | None,
) -> list[CandidateTrace]:
    """One row per stored episode, including the ones the ceiling withheld.

    Classification is against the whole store, not the set the library was
    shown: an episode the ceiling excluded still has to report that it
    cleared the threshold, or the trace would describe it as a near miss
    and say the opposite of what happened.
    """
    rows: list[CandidateTrace] = []
    for position, episode in enumerate(episodes):
        identifier = str(episode["id"])
        is_recent = position in recent
        is_relevant = position in relevant
        is_anchor = anchor_index is not None and position == anchor_index
        is_withheld = position in withheld
        was_delivered = identifier in delivered

        via: SelectionPath | None = None
        if was_delivered:
            if is_relevant and is_recent:
                via = "both"
            elif is_relevant:
                via = "relevance"
            elif is_recent:
                via = "continuity"
            else:
                # Neither condition admitted it, so the caller's anchor did.
                via = "anchor"

        cosine = float(scores[position])
        rows.append(
            CandidateTrace(
                id=identifier,
                turn_number=int(episode["turn_number"]),
                preview=_preview(episode.get("user_message", "")),
                assistant_preview=_preview(episode.get("assistant_message", "")),
                cosine=cosine,
                margin=cosine - threshold,
                render_chars=len(render_episode_element(episode)),
                eligible=position in eligible,
                relevant=is_relevant,
                in_recency_window=is_recent,
                is_anchor=is_anchor,
                withheld=is_withheld,
                delivered=was_delivered,
                delivered_via=via,
            )
        )
    return rows


def _trace_report(report) -> ReportTrace:
    return ReportTrace(
        chars_delivered=report.chars_delivered,
        chars_wanted=report.chars_wanted,
        episodes_delivered=report.episodes_delivered,
        stm_count=report.stm_count,
        k_count=report.k_count,
        latency_ms=report.latency_ms,
        pool_size=report.pool_size,
        read_policy=report.read_policy,
        relevance_threshold=report.relevance_threshold,
        eligible_count=report.eligible_count,
        selected_ids=list(report.selected_ids),
        retrieval_chars_delivered=report.retrieval_chars_delivered,
        recency_count=report.recency_count,
        semantic_count=report.semantic_count,
        recent_ids=list(report.recent_ids),
        recency_additive=report.recency_additive,
        through_turn=report.through_turn,
        anchor_turn=report.anchor_turn,
    )


def _preview(value: object) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:PREVIEW_CHARS]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
