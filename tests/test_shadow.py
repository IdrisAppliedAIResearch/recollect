"""The instrumentation must reproduce the library, not merely resemble it.

This is the project's load-bearing test. Everything the harness shows a
user - scores, cluster maps, selector arithmetic, drop reasons - is
produced by the shadow reconstruction, while the text the model actually
receives comes from the library. If those two ever describe different
computations, the product is a confident-looking fiction. So the suite
sweeps store sizes and budgets, including the awkward ones, and asserts
byte equality every time.
"""

from __future__ import annotations

import numpy as np
import pytest
from episodic import EpisodicConfig
from episodic._embedding import embed_solo
from episodic._packing import EMPTY_PAYLOAD_CHARS

from recollect.engine._internals import read_episodes
from recollect.engine.shadow import TraceDivergenceError, retrieve_with_trace

from .conftest import FakeEmbedder, build_store, make_episodes

# Budgets chosen to hit the interesting regimes: below the empty-tag floor,
# exactly at it, too small for one episode, mid-range where packing must
# skip, and generous enough that nothing is dropped.
BUDGETS = [
    0,
    1,
    EMPTY_PAYLOAD_CHARS - 1,
    EMPTY_PAYLOAD_CHARS,
    200,
    1_000,
    4_000,
    32_000,
]
STORE_SIZES = [0, 1, 5, 33, 60]

QUERY = "venice question 3: what did we decide?"


def _retrieve(store, config, query, budget):
    episodes = read_episodes(store)
    query_embedding = embed_solo(store._embedder, query)
    return retrieve_with_trace(
        episodes=episodes,
        query_embedding=query_embedding,
        budget=budget,
        config=config,
        strict=True,
    )


@pytest.mark.parametrize("size", STORE_SIZES)
@pytest.mark.parametrize("budget", BUDGETS)
def test_shadow_reproduces_library(tmp_path, size, budget):
    """Across every store size and budget, the two accounts agree."""
    config = EpisodicConfig()
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"s{size}.sqlite", embedder, config, make_episodes(size)
    )
    try:
        result = _retrieve(store, config, QUERY, budget)
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


@pytest.mark.parametrize("budget", BUDGETS)
def test_budget_ceiling_is_never_exceeded(store, config, budget):
    """The library's hard ceiling, re-asserted from the harness side."""
    result = _retrieve(store, config, "budget question 7", budget)
    assert result.report.chars_delivered <= max(budget, 0)
    assert len(result.context_block.payload) <= max(budget, 0)
    assert result.report.chars_available >= 0


def test_every_episode_appears_as_a_candidate(store, config):
    """Visibility means the undelivered are visible too."""
    episodes = read_episodes(store)
    result = _retrieve(store, config, "painting question 12", 4_000)

    assert len(result.candidates) == len(episodes)
    assert {c.id for c in result.candidates} == {str(e["id"]) for e in episodes}

    delivered = [c for c in result.candidates if c.delivered]
    assert len(delivered) == result.report.episodes_delivered

    # Every candidate carries a verdict: delivered ones have no drop reason,
    # undelivered ones always explain themselves.
    for candidate in result.candidates:
        if candidate.delivered:
            assert candidate.drop_reason is None
            assert candidate.delivered_via is not None
        else:
            assert candidate.drop_reason


def test_tier_attribution_matches_the_report(store, config):
    """Per-tier counts must reconcile with the library's own attribution."""
    result = _retrieve(store, config, "rainfall question 4", 4_000)

    by_tier = {tier.name: tier for tier in result.tiers}
    assert len(by_tier["recency"].delivered_ids) == result.report.stm_count
    assert len(by_tier["similarity"].delivered_ids) == result.report.k_count
    assert len(by_tier["coverage"].delivered_ids) == result.report.coverage_count

    total = sum(len(tier.delivered_ids) for tier in result.tiers)
    assert total == result.report.episodes_delivered


def test_tier_outcomes_partition_its_proposals(store, config):
    """Every proposal lands in exactly one of delivered/overlapped/skipped."""
    for budget in (600, 1_500, 32_000):
        result = _retrieve(store, config, QUERY, budget)
        for tier in result.tiers:
            buckets = (
                set(tier.delivered_ids)
                | set(tier.overlapped_ids)
                | set(tier.skipped_ids)
            )
            assert buckets == set(tier.proposed_ids)
            assert (
                len(tier.delivered_ids)
                + len(tier.overlapped_ids)
                + len(tier.skipped_ids)
                == len(tier.proposed_ids)
            )
            # The two zero-delivery cases are mutually exclusive.
            assert not (tier.starved and tier.fully_overlapped)


def test_starvation_is_distinguished_from_overlap(store, config):
    """A tier that duplicates an earlier one is not reported as starved.

    This is the distinction that makes the packing-order fault findable: a
    bare zero cannot tell you whether the budget ran out or whether the
    path merely proposed what recency already had.
    """
    # A budget large enough for everything: coverage re-proposes episodes
    # recency already claimed, so it delivers nothing but is not starved.
    generous = _retrieve(store, config, QUERY, 32_000)
    coverage = generous.tier("coverage")
    if coverage.proposed_ids and not coverage.delivered_ids:
        assert coverage.fully_overlapped
        assert not coverage.starved
        assert not coverage.skipped_ids

    # A budget too small to hold everything recency wants: later tiers get
    # nothing at all, which is starvation.
    tight = _retrieve(store, config, QUERY, 900)
    for tier in tight.tiers:
        if tier.starved:
            assert tier.skipped_ids
            assert not tier.delivered_ids


def test_relevance_is_scored_for_every_episode(store, config):
    """The cosine that decides everything is recorded for all of them."""
    result = _retrieve(store, config, "contracts question 9", 4_000)

    assert all(isinstance(c.relevance, float) for c in result.candidates)
    ranks = sorted(c.relevance_rank for c in result.candidates)
    assert ranks == list(range(1, len(result.candidates) + 1))

    ordered = sorted(result.candidates, key=lambda c: c.relevance_rank)
    scores = [c.relevance for c in ordered]
    assert scores == sorted(scores, reverse=True)


def test_similarity_tier_reports_inertness_not_emptiness(store, config):
    """Zero hits is reported with the margin that explains it."""
    result = _retrieve(store, config, "venice question 1", 4_000)
    detail = result.similarity_detail

    assert detail.threshold == config.k_threshold
    assert detail.inert == (detail.hit_count == 0)
    assert detail.margin_to_threshold == pytest.approx(
        detail.threshold - detail.max_relevance_observed
    )
    if detail.inert:
        # The point of the field: it distinguishes "nothing matched" from
        # "nothing could have matched".
        assert detail.margin_to_threshold > 0


def test_packing_decisions_are_ordered_and_complete(store, config):
    """Every proposal produces a decision, in the order it was considered."""
    result = _retrieve(store, config, "budget question 2", 1_500)
    decisions = result.packing.decisions

    assert [d.order for d in decisions] == list(range(1, len(decisions) + 1))
    admitted = {d.candidate_id for d in decisions if d.admitted}
    delivered = {c.id for c in result.candidates if c.delivered}
    assert admitted == delivered

    for decision in decisions:
        assert decision.reason
        assert decision.payload_chars_after <= max(result.packing.budget_chars, 0)


def test_selector_steps_expose_the_arithmetic(store, config):
    """Each greedy choice carries the gain that justified it."""
    result = _retrieve(store, config, "painting question 6", 8_000)
    steps = result.selector_steps
    if not steps:
        pytest.skip("no coverage selection at this budget")

    assert [s.step for s in steps] == list(range(1, len(steps) + 1))
    assert all(s.cumulative_chars > 0 for s in steps)
    assert all(s.additive_chars > 0 for s in steps)
    # Cumulative cost is monotonic.
    cumulative = [s.cumulative_chars for s in steps]
    assert cumulative == sorted(cumulative)


def test_clusters_partition_the_pool(store, config):
    """Cluster membership covers the pool exactly once."""
    result = _retrieve(store, config, "venice question 8", 8_000)
    if not result.clusters:
        pytest.skip("no clustering at this budget")

    members = [i for cluster in result.clusters for i in cluster.member_ids]
    assert len(members) == len(set(members))
    assert len(members) == result.report.pool_size

    for cluster in result.clusters:
        assert cluster.size == len(cluster.member_ids)
        assert set(cluster.selected_ids) <= set(cluster.member_ids)
        assert set(cluster.delivered_ids) <= set(cluster.member_ids)


def test_divergence_is_raised_not_swallowed(store, config, monkeypatch):
    """A broken reconstruction must fail loudly, not serve a plausible lie."""
    import recollect.engine.shadow as shadow_module

    original = shadow_module.render_stm_payload

    def corrupted(recent, stm):
        return original(recent, stm) + "  <!-- drift -->"

    monkeypatch.setattr(shadow_module, "render_stm_payload", corrupted)

    with pytest.raises(TraceDivergenceError) as excinfo:
        _retrieve(store, config, "venice question 3", 4_000)
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

    episodes = read_episodes(store)
    query_embedding = embed_solo(store._embedder, "venice question 3")
    result = retrieve_with_trace(
        episodes=episodes,
        query_embedding=query_embedding,
        budget=4_000,
        config=config,
        strict=False,
    )
    assert not result.verification.trustworthy
    assert not result.verification.payload_identical


def test_empty_store_is_handled(tmp_path, config):
    """A first turn has nothing to retrieve and must still trace cleanly."""
    embedder = FakeEmbedder()
    store = build_store(tmp_path / "empty.sqlite", embedder, config, [])
    try:
        result = _retrieve(store, config, "anything at all", 32_000)
    finally:
        store.close()

    assert result.candidates == []
    assert result.clusters == []
    assert result.report.episodes_delivered == 0
    assert result.verification.trustworthy
    assert result.similarity_detail.inert


def test_query_vector_is_used_not_recomputed(store, config):
    """The shadow and the authority must score against the same vector."""
    episodes = read_episodes(store)
    query_embedding = embed_solo(store._embedder, "venice question 3")
    before = store._embedder.calls

    retrieve_with_trace(
        episodes=episodes,
        query_embedding=query_embedding,
        budget=4_000,
        config=config,
    )
    # Retrieval itself embeds nothing: it is a pure function of the store
    # and the vector it was handed.
    assert store._embedder.calls == before


def test_trace_fragments_serialize(store, config):
    """Everything shown in the UI must survive a JSON round trip."""
    result = _retrieve(store, config, "budget question 5", 4_000)
    for model in (
        *result.candidates,
        *result.clusters,
        *result.tiers,
        *result.selector_steps,
        result.similarity_detail,
        result.packing,
        result.context_block,
        result.report,
        result.verification,
    ):
        payload = model.model_dump_json()
        assert type(model).model_validate_json(payload) == model


@pytest.mark.parametrize("window", [0, 1, 8, 32, 200])
def test_alternate_recency_windows_still_verify(tmp_path, window):
    """The verification holds when the mechanism is reconfigured."""
    config = EpisodicConfig(recency_window_n=window)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"w{window}.sqlite", embedder, config, make_episodes(40)
    )
    try:
        result = _retrieve(store, config, "venice question 3", 6_000)
    finally:
        store.close()
    assert result.verification.trustworthy


@pytest.mark.parametrize("threshold", [0.0, 0.2, 0.48, 0.9])
def test_alternate_thresholds_still_verify(tmp_path, threshold):
    """Including a threshold low enough to make the similarity path fire."""
    config = EpisodicConfig(k_threshold=threshold)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"k{threshold}.sqlite", embedder, config, make_episodes(40)
    )
    try:
        result = _retrieve(store, config, "venice question 3", 6_000)
    finally:
        store.close()
    assert result.verification.trustworthy
    if threshold == 0.0:
        assert not result.similarity_detail.inert


def test_vectors_are_float32_and_pinned_width(store):
    """Guards the assumption every cosine in the trace rests on."""
    vector = embed_solo(store._embedder, "shape check")
    assert vector.dtype == np.float32
    assert vector.shape == (1024,)
