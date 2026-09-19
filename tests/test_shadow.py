"""The instrumentation must reproduce the library, not merely resemble it.

This is the project's load-bearing test. Everything the harness shows a
user - each episode's cosine, its distance from the threshold, whether
relevance or continuity put it in the block - comes from the shadow
reconstruction, while the text the model actually receives comes from the
library. If those two ever describe different computations, the product is
a confident-looking fiction. So the suite sweeps store sizes, continuity
windows and thresholds, including the degenerate ones, and asserts byte
equality every time.

The deployed read path under test is the timeline: the union of every
episode at or above ``timeline_threshold`` with the last
``recency_window_n`` exchanges, rendered in source order. No budget, no
ranking, no drops.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from episodic import EpisodicConfig
from episodic._embedding import EMBEDDING_DIMENSION, embed_solo

from recollect.engine._internals import read_episodes
from recollect.engine.shadow import TraceDivergenceError, retrieve_with_trace

from .conftest import FakeEmbedder, build_store, make_episodes

STORE_SIZES = [0, 1, 5, 33, 60]
#: 0 disables continuity entirely, 1 is the minimal slice, 200 exceeds
#: every store built here, and 32 is the deployed value.
WINDOWS = [0, 1, 8, 32, 200]
#: 0.0 is the loosest the config permits, 1.0 admits only an exact
#: direction match, and 0.48 is the deployed threshold.
THRESHOLDS = [0.0, 0.48, 1.0]

QUERY = "venice question 3: what did we decide?"


class PinnedVectorEmbedder(FakeEmbedder):
    """Every text embeds to the same non-zero vector.

    Ties every cosine at 1.0, so nothing distinguishes the episodes and the
    source ordering alone decides the block. This is the case a
    reconstruction that sorted differently would still get *membership*
    right on while producing different bytes.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pinned = np.full(EMBEDDING_DIMENSION, 0.25, dtype=np.float32)

    def __call__(self, text: str) -> np.ndarray:
        self.calls += 1
        return self._pinned.copy()


def _retrieve(store, config, query, *, query_embedding=None, **kwargs):
    episodes = read_episodes(store)
    if query_embedding is None:
        query_embedding = embed_solo(store._embedder, query)
    return retrieve_with_trace(
        episodes=episodes,
        query_embedding=query_embedding,
        config=config,
        strict=True,
        **kwargs,
    )


def _timeline(**overrides) -> EpisodicConfig:
    return replace(EpisodicConfig(), **overrides)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", STORE_SIZES)
@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("threshold", THRESHOLDS)
def test_shadow_reproduces_library(tmp_path, size, window, threshold):
    """Across every store size, window and threshold, the accounts agree."""
    config = _timeline(recency_window_n=window, timeline_threshold=threshold)
    store = build_store(
        tmp_path / f"s{size}w{window}t{threshold}.sqlite",
        FakeEmbedder(),
        config,
        make_episodes(size),
    )
    try:
        result = _retrieve(store, config, QUERY)
    finally:
        store.close()

    assert result.verification.payload_identical, (
        result.verification.mismatched_fields
    )
    assert result.verification.report_fields_identical, (
        result.verification.mismatched_fields
    )
    assert result.verification.trustworthy
    assert (
        result.verification.authority_payload_sha256
        == result.verification.shadow_payload_sha256
    )


def test_matches_the_store_context_directly(tmp_path):
    """The harness must agree with the library's own public entry point."""
    config = _timeline()
    store = build_store(
        tmp_path / "direct.sqlite", FakeEmbedder(), config, make_episodes(40)
    )
    try:
        episodes = read_episodes(store)
        payload, report = store.context(QUERY)
        result = retrieve_with_trace(
            episodes=episodes,
            query_embedding=embed_solo(store._embedder, QUERY),
            config=config,
            strict=True,
        )
    finally:
        store.close()

    assert result.payload == payload
    assert result.report.episodes_delivered == report.episodes_delivered
    assert list(result.report.selected_ids) == list(report.selected_ids)
    assert result.report.read_policy == "timeline"


# ---------------------------------------------------------------------------
# What the selection is
# ---------------------------------------------------------------------------


def test_selection_is_the_union_of_relevance_and_continuity(store, config):
    result = _retrieve(store, config, QUERY)
    detail = result.timeline
    assert set(detail.selected_ids) == set(detail.relevant_ids) | set(
        detail.recent_ids
    )


def test_an_episode_qualifying_twice_is_delivered_once(store, config):
    """The timeline is a union, not a concatenation."""
    result = _retrieve(store, config, QUERY)
    detail = result.timeline
    assert len(detail.selected_ids) == len(set(detail.selected_ids))
    assert set(detail.overlap_ids) == set(detail.relevant_ids) & set(
        detail.recent_ids
    )
    # The count that says what retrieval actually added over continuity.
    assert detail.relevance_only_count == len(
        set(detail.relevant_ids) - set(detail.recent_ids)
    )


def test_the_block_is_chronological(store, config):
    """Delivered order is source order, not relevance order."""
    result = _retrieve(store, config, QUERY)
    order = {
        candidate.id: candidate.turn_number for candidate in result.candidates
    }
    turns = [order[identifier] for identifier in result.timeline.selected_ids]
    assert turns == sorted(turns)


def test_nothing_is_dropped_however_large_the_union(store, config):
    """No capacity means no drop can occur - asserted, not assumed."""
    result = _retrieve(store, config, QUERY)
    delivered = {c.id for c in result.candidates if c.delivered}
    assert delivered == set(result.timeline.selected_ids)
    for candidate in result.candidates:
        if candidate.relevant or candidate.in_recency_window:
            assert candidate.delivered, candidate.id


# ---------------------------------------------------------------------------
# Per-candidate accounting
# ---------------------------------------------------------------------------


def test_every_episode_appears_as_a_candidate(store, config):
    result = _retrieve(store, config, QUERY)
    assert len(result.candidates) == len(read_episodes(store))
    assert len({c.id for c in result.candidates}) == len(result.candidates)


def test_margin_is_the_distance_from_the_threshold(store, config):
    """The renderer must never have to recompute this and get a second answer."""
    result = _retrieve(store, config, QUERY)
    threshold = result.timeline.relevance_threshold
    for candidate in result.candidates:
        assert candidate.margin == pytest.approx(candidate.cosine - threshold)
        assert candidate.relevant == (candidate.cosine >= threshold)


def test_attribution_names_the_condition_that_admitted_it(store, config):
    result = _retrieve(store, config, QUERY)
    for candidate in result.candidates:
        if not candidate.delivered:
            assert candidate.delivered_via is None
        elif candidate.relevant and candidate.in_recency_window:
            assert candidate.delivered_via == "both"
        elif candidate.relevant:
            assert candidate.delivered_via == "relevance"
        else:
            assert candidate.delivered_via == "continuity"


def test_timeline_detail_reconciles_with_the_candidates(store, config):
    result = _retrieve(store, config, QUERY)
    detail = result.timeline
    assert {c.id for c in result.candidates if c.relevant} == set(
        detail.relevant_ids
    )
    assert {c.id for c in result.candidates if c.in_recency_window} == set(
        detail.recent_ids
    )
    assert detail.eligible_count == sum(
        1 for c in result.candidates if c.eligible
    )
    assert result.report.stm_count == len(detail.recent_ids)
    assert result.report.k_count == detail.relevance_only_count


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def test_a_threshold_of_one_leaves_only_continuity(tmp_path):
    config = _timeline(timeline_threshold=1.0)
    store = build_store(
        tmp_path / "strict.sqlite", FakeEmbedder(), config, make_episodes(40)
    )
    try:
        result = _retrieve(store, config, QUERY)
    finally:
        store.close()
    assert result.verification.trustworthy
    assert set(result.timeline.selected_ids) == set(result.timeline.recent_ids)
    assert result.timeline.relevance_only_count == 0


def test_a_zero_window_leaves_only_relevance(tmp_path):
    config = _timeline(recency_window_n=0)
    store = build_store(
        tmp_path / "nowindow.sqlite", FakeEmbedder(), config, make_episodes(40)
    )
    try:
        result = _retrieve(store, config, QUERY)
    finally:
        store.close()
    assert result.verification.trustworthy
    assert result.timeline.recent_ids == []
    assert set(result.timeline.selected_ids) == set(result.timeline.relevant_ids)


@pytest.mark.parametrize("window", WINDOWS)
def test_alternate_recency_windows_still_verify(tmp_path, window):
    """The verification holds when the mechanism is reconfigured."""
    config = _timeline(recency_window_n=window)
    store = build_store(
        tmp_path / f"w{window}.sqlite", FakeEmbedder(), config, make_episodes(40)
    )
    try:
        result = _retrieve(store, config, QUERY)
    finally:
        store.close()
    assert result.verification.trustworthy
    # A window past the end of the store clamps rather than overreaching.
    assert result.report.recency_count == min(window, 40)


def test_empty_store_is_handled(tmp_path):
    """A first turn has nothing to retrieve and must still trace cleanly."""
    config = _timeline()
    store = build_store(tmp_path / "empty.sqlite", FakeEmbedder(), config, [])
    try:
        result = _retrieve(store, config, "anything at all")
    finally:
        store.close()

    assert result.candidates == []
    assert result.timeline.selected_ids == []
    assert result.timeline.eligible_count == 0
    assert result.report.episodes_delivered == 0
    assert result.verification.trustworthy


def test_tied_cosines_are_ordered_by_turn_then_id(tmp_path):
    """With nothing to separate them, source order alone must decide."""
    config = _timeline()
    embedder = PinnedVectorEmbedder()
    store = build_store(
        tmp_path / "tied.sqlite", embedder, config, make_episodes(40)
    )
    try:
        result = _retrieve(store, config, QUERY)
    finally:
        store.close()

    assert result.verification.trustworthy
    cosines = {round(c.cosine, 9) for c in result.candidates}
    assert cosines == {1.0}
    order = {c.id: c.turn_number for c in result.candidates}
    turns = [order[i] for i in result.timeline.selected_ids]
    assert turns == sorted(turns)


# ---------------------------------------------------------------------------
# The verification itself
# ---------------------------------------------------------------------------


def test_divergence_is_raised_not_swallowed(store, config, monkeypatch):
    """A broken reconstruction must fail loudly, not serve a plausible lie."""
    import recollect.engine.shadow as shadow_module

    original = shadow_module.render_stm_payload

    def corrupted(recent, stm):
        return original(recent, stm) + "  <!-- drift -->"

    monkeypatch.setattr(shadow_module, "render_stm_payload", corrupted)

    with pytest.raises(TraceDivergenceError) as excinfo:
        _retrieve(store, config, QUERY)
    assert "did not reproduce" in str(excinfo.value)


def test_non_strict_mode_records_divergence_instead(store, config, monkeypatch):
    """Offline analysis can opt out of raising, but not out of knowing."""
    import recollect.engine.shadow as shadow_module

    original = shadow_module.render_stm_payload
    monkeypatch.setattr(
        shadow_module,
        "render_stm_payload",
        lambda recent, stm: original(recent, stm) + " drift",
    )

    result = retrieve_with_trace(
        episodes=read_episodes(store),
        query_embedding=embed_solo(store._embedder, QUERY),
        config=config,
        strict=False,
    )
    assert not result.verification.trustworthy
    assert not result.verification.payload_identical


def test_a_selection_that_agrees_on_membership_but_not_order_diverges(
    store, config, monkeypatch
):
    """Byte equality is the claim, not set equality.

    A reconstruction that picked the right episodes and rendered them in
    the wrong order would pass every count in the report. Only the payload
    comparison catches it, so that comparison is worth its own test.
    """
    import recollect.engine.shadow as shadow_module

    original = shadow_module.render_stm_payload
    monkeypatch.setattr(
        shadow_module,
        "render_stm_payload",
        lambda recent, stm: original(recent, list(reversed(list(stm)))),
    )

    result = retrieve_with_trace(
        episodes=read_episodes(store),
        query_embedding=embed_solo(store._embedder, QUERY),
        config=config,
        strict=False,
    )
    assert not result.verification.payload_identical
    # The counts alone would have said everything was fine.
    assert result.verification.report_fields_identical


def test_query_vector_is_used_not_recomputed(store, config):
    """The shadow and the authority must score against the same vector."""
    query_embedding = embed_solo(store._embedder, QUERY)
    before = store._embedder.calls

    retrieve_with_trace(
        episodes=read_episodes(store),
        query_embedding=query_embedding,
        config=config,
    )
    # Retrieval itself embeds nothing: it is a pure function of the store
    # and the vector it was handed.
    assert store._embedder.calls == before


def test_trace_fragments_serialize(store, config):
    """Everything shown in the UI must survive a JSON round trip."""
    result = _retrieve(store, config, QUERY)
    for model in (
        *result.candidates,
        result.timeline,
        result.context_block,
        result.report,
        result.verification,
    ):
        payload = model.model_dump_json()
        assert type(model).model_validate_json(payload) == model


def test_vectors_are_float32_and_pinned_width(store):
    """Guards the assumption every cosine in the trace rests on."""
    vector = embed_solo(store._embedder, "shape check")
    assert vector.dtype == np.float32
    assert vector.shape == (1024,)


# ---------------------------------------------------------------------------
# The deployment ceiling - a deviation from the library, taken for hardware
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ceiling", [None, 0])
def test_no_ceiling_leaves_the_mechanism_untouched(store, config, ceiling):
    unbounded = _retrieve(store, config, QUERY)
    capped = _retrieve(store, config, QUERY, ceiling_chars=ceiling)
    assert capped.payload == unbounded.payload
    assert capped.ceiling.engaged is False
    assert capped.ceiling.withheld_ids == []
    assert capped.ceiling.store_episodes == capped.ceiling.considered_episodes


def test_a_generous_ceiling_withholds_nothing(store, config):
    result = _retrieve(store, config, QUERY, ceiling_chars=10_000_000)
    assert result.ceiling.engaged is False
    assert result.ceiling.ceiling_chars == 10_000_000
    assert result.verification.trustworthy


def test_the_ceiling_holds_and_the_verification_still_does(store, config):
    """The whole reason the ceiling is a pre-filter and not a trim.

    Trimming the payload after the library produced it would make the two
    computations disagree byte-for-byte and refuse every turn. Deciding
    what the library is *shown* keeps the proof intact, and that is the
    property worth pinning.
    """
    # Between the protected continuity floor and the uncapped size, so the
    # ceiling has something to shed AND can actually reach its target.
    floor = len(_retrieve(store, config, QUERY, ceiling_chars=1).payload)
    uncapped = len(_retrieve(store, config, QUERY).payload)
    assert floor < uncapped, "this store cannot exercise the ceiling"
    ceiling = (floor + uncapped) // 2

    result = _retrieve(store, config, QUERY, ceiling_chars=ceiling)
    assert result.ceiling.engaged
    assert result.ceiling.withheld_ids
    assert len(result.payload) <= ceiling
    assert result.verification.payload_identical, (
        result.verification.mismatched_fields
    )
    assert result.verification.trustworthy


def test_the_ceiling_sheds_the_weakest_first(store, config):
    result = _retrieve(store, config, QUERY, ceiling_chars=4_000)
    by_id = {candidate.id: candidate for candidate in result.candidates}
    withheld = [by_id[identifier] for identifier in result.ceiling.withheld_ids]
    kept = [
        candidate
        for candidate in result.candidates
        if candidate.relevant and not candidate.withheld
        and not candidate.in_recency_window
    ]
    assert withheld, "expected the ceiling to shed something"
    if kept:
        assert max(c.cosine for c in withheld) <= min(c.cosine for c in kept)


def test_continuity_is_never_withheld(tmp_path):
    """Losing what was just said would be worse than exceeding the ceiling."""
    config = _timeline()
    store = build_store(
        tmp_path / "tight.sqlite", FakeEmbedder(), config, make_episodes(40)
    )
    try:
        # Far too small for 32 episodes of continuity, let alone anything else.
        result = _retrieve(store, config, QUERY, ceiling_chars=200)
    finally:
        store.close()

    assert result.verification.trustworthy
    assert result.timeline.recent_ids, "the continuity window was emptied"
    assert len(result.timeline.recent_ids) == 32
    # The ceiling is deliberately exceeded rather than dropping recency.
    assert len(result.payload) > 200
    for identifier in result.ceiling.withheld_ids:
        assert identifier not in result.timeline.recent_ids


def test_a_withheld_episode_is_not_reported_as_a_near_miss(store, config):
    """They mean opposite things and must never be conflated."""
    result = _retrieve(store, config, QUERY, ceiling_chars=4_000)
    withheld = [c for c in result.candidates if c.withheld]
    assert withheld
    for candidate in withheld:
        assert candidate.relevant, "withheld means it DID clear the threshold"
        assert candidate.margin >= 0
        assert candidate.delivered is False
        assert candidate.delivered_via is None
        assert candidate.in_recency_window is False


def test_the_ceiling_records_the_true_store_size(store, config):
    """report.pool_size counts only what the library saw; this counts all."""
    result = _retrieve(store, config, QUERY, ceiling_chars=4_000)
    detail = result.ceiling
    assert detail.store_episodes == len(read_episodes(store))
    assert detail.considered_episodes == result.report.pool_size
    assert detail.store_episodes - detail.considered_episodes == len(
        detail.withheld_ids
    )
    assert detail.withheld_chars > 0


def test_every_stored_episode_still_has_a_candidate_row_under_a_ceiling(
    store, config,
):
    """A withheld episode must not vanish from the account of the turn."""
    result = _retrieve(store, config, QUERY, ceiling_chars=4_000)
    assert len(result.candidates) == len(read_episodes(store))
