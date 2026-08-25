"""The instrumentation must reproduce the library, not merely resemble it.

This is the project's load-bearing test. Everything the harness shows a
user - CC80 scores, spread-step arithmetic, drop reasons, the ASPECT
decision record - is produced by the shadow reconstruction, while the text
the model actually receives comes from the library. If those two ever
describe different computations, the product is a confident-looking
fiction. So the suite sweeps store sizes and budgets, including the
awkward ones, with ASPECT on and off, and asserts byte equality every
time.

The deployed read path under test is CC-007: additive recent continuity
(the last recency_window_n episodes, rendered outside the budget) plus a
long-term block that one character budget governs - CC80 (dense/BM25
min-max fused) in rank order, with the protected static ASPECT spread
carving out half of the allowance when it is enabled.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from episodic import EpisodicConfig
from episodic._embedding import EMBEDDING_DIMENSION, embed_solo
from episodic._packing import EMPTY_PAYLOAD_CHARS
from episodic._render import render_stm_payload

from recollect.engine._internals import read_episodes
from recollect.engine.shadow import TraceDivergenceError, retrieve_with_trace

from .conftest import FakeEmbedder, build_store, make_episodes

# Budgets chosen to hit the interesting regimes: below the empty-tag floor,
# exactly at it, too small for one episode in either half, mid-range where
# packing must skip, and generous enough that nothing is dropped.
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
ASPECTS = [False, True]

QUERY = "venice question 3: what did we decide?"


class PinnedVectorEmbedder(FakeEmbedder):
    """Every text embeds to the same non-zero vector.

    Makes the dense component constant (all cosines tied at 1.0): the
    case min-max normalization must handle by contributing zeros, and the
    case where the tie-breaker alone decides the whole ranking.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pinned = np.full(EMBEDDING_DIMENSION, 0.25, dtype=np.float32)

    def __call__(self, text: str) -> np.ndarray:
        self.calls += 1
        return self._pinned.copy()


def _retrieve(store, config, query, budget, *, query_embedding=None):
    episodes = read_episodes(store)
    if query_embedding is None:
        query_embedding = embed_solo(store._embedder, query)
    return retrieve_with_trace(
        episodes=episodes,
        query_text=query,
        query_embedding=query_embedding,
        budget=budget,
        config=config,
        strict=True,
    )


@pytest.mark.parametrize("size", STORE_SIZES)
@pytest.mark.parametrize("budget", BUDGETS)
@pytest.mark.parametrize("aspect", ASPECTS)
def test_shadow_reproduces_library(tmp_path, size, budget, aspect):
    """Across every store size, budget, and ASPECT state, the accounts agree."""
    config = replace(EpisodicConfig(), aspect_enabled=aspect)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"s{size}a{int(aspect)}.sqlite",
        embedder,
        config,
        make_episodes(size),
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
    """The allowance governs only the long-term block; recency is additive."""
    result = _retrieve(store, config, "budget question 7", budget)
    assert result.report.retrieval_chars_delivered <= max(budget, 0)
    assert result.report.retrieval_budget_chars == max(budget, 0)
    assert result.report.chars_delivered == len(result.context_block.payload)
    assert result.context_block.chars == result.report.chars_delivered
    # Total delivered MAY exceed the allowance (additive recent block),
    # but the governed part may not, and neither number is negative.
    assert result.report.chars_delivered >= result.report.retrieval_chars_delivered
    assert result.report.chars_available >= 0


def _assert_candidate_verdicts(result, episodes):
    assert len(result.candidates) == len(episodes)
    assert {c.id for c in result.candidates} == {str(e["id"]) for e in episodes}

    delivered = [c for c in result.candidates if c.delivered]
    assert len(delivered) == result.report.episodes_delivered

    # Every candidate carries a verdict: delivered ones have no drop reason
    # and a claiming path; undelivered ones always explain themselves.
    for candidate in result.candidates:
        if candidate.delivered:
            assert candidate.drop_reason is None
            assert candidate.delivered_via is not None
        else:
            assert candidate.drop_reason


@pytest.mark.parametrize("aspect", ASPECTS)
def test_every_episode_appears_as_a_candidate(tmp_path, aspect):
    """Visibility means the undelivered are visible too."""
    config = replace(EpisodicConfig(), aspect_enabled=aspect)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"verdicts{int(aspect)}.sqlite",
        embedder,
        config,
        make_episodes(40),
    )
    try:
        episodes = read_episodes(store)
        result = _retrieve(store, config, "painting question 12", 4_000)
    finally:
        store.close()

    _assert_candidate_verdicts(result, episodes)


@pytest.mark.parametrize("aspect", ASPECTS)
def test_tier_attribution_matches_the_report(tmp_path, aspect):
    """Per-tier counts must reconcile with the library's own attribution."""
    config = replace(EpisodicConfig(), aspect_enabled=aspect)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"attr{int(aspect)}.sqlite", embedder, config, make_episodes(40)
    )
    try:
        result = _retrieve(store, config, "rainfall question 4", 4_000)
    finally:
        store.close()

    by_tier = {tier.name: tier for tier in result.tiers}
    assert len(by_tier["recency"].delivered_ids) == result.report.recency_count
    assert len(by_tier["semantic"].delivered_ids) == result.report.semantic_count
    assert len(by_tier["aspect"].delivered_ids) == result.report.aspect_count

    total = sum(len(tier.delivered_ids) for tier in result.tiers)
    assert total == result.report.episodes_delivered


@pytest.mark.parametrize("aspect", ASPECTS)
def test_tier_outcomes_partition_its_proposals(tmp_path, aspect):
    """Every proposal lands in exactly one of delivered/overlapped/skipped."""
    config = replace(EpisodicConfig(), aspect_enabled=aspect)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"partition{int(aspect)}.sqlite",
        embedder,
        config,
        make_episodes(40),
    )
    try:
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
    finally:
        store.close()


def test_starvation_is_distinguished_from_overlap(tmp_path):
    """A path that duplicates an earlier one is not reported as starved.

    This is the distinction that makes the packing-order fault findable: a
    bare zero cannot tell you whether the budget ran out or whether the
    path merely proposed what an earlier path already had.
    """
    config = replace(EpisodicConfig(), aspect_enabled=True)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / "starved.sqlite", embedder, config, make_episodes(40)
    )
    try:
        # A budget large enough for almost everything: paths that re-propose
        # what was already claimed deliver nothing new but are overlapped,
        # not starved.
        generous = _retrieve(store, config, QUERY, 32_000)
        for tier in generous.tiers:
            if tier.proposed_ids and not tier.delivered_ids:
                if tier.skipped_ids:
                    assert tier.starved
                else:
                    assert tier.fully_overlapped

        # A budget too small to hold everything: later phases propose
        # episodes that never land, which is starvation.
        tight = _retrieve(store, config, QUERY, 900)
        for tier in tight.tiers:
            if tier.starved:
                assert tier.skipped_ids
                assert not tier.delivered_ids
    finally:
        store.close()


@pytest.mark.parametrize("aspect", ASPECTS)
def test_cc80_scores_are_recorded_for_every_episode(tmp_path, aspect):
    """The fused score that decides admission is recorded for all of them."""
    config = replace(EpisodicConfig(), aspect_enabled=aspect)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"cc80{int(aspect)}.sqlite", embedder, config, make_episodes(40)
    )
    try:
        result = _retrieve(store, config, "contracts question 9", 4_000)
    finally:
        store.close()

    for candidate in result.candidates:
        assert isinstance(candidate.dense_cosine, float)
        assert isinstance(candidate.bm25_score, float)
        assert isinstance(candidate.cc80_score, float)

    ranks = sorted(c.cc80_rank for c in result.candidates)
    assert ranks == list(range(1, len(result.candidates) + 1))

    ordered = sorted(result.candidates, key=lambda c: c.cc80_rank)
    scores = [c.cc80_score for c in ordered]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("aspect", ASPECTS)
def test_cc80_detail_reconciles_with_the_candidates(tmp_path, aspect):
    """The fusion constants and per-component bounds match the store config."""
    config = replace(EpisodicConfig(), aspect_enabled=aspect)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"detail{int(aspect)}.sqlite", embedder, config, make_episodes(40)
    )
    try:
        result = _retrieve(store, config, "venice question 1", 4_000)
    finally:
        store.close()

    detail = result.cc80_detail
    assert detail.dense_weight == config.semantic_dense_weight
    assert detail.bm25_k1 == config.bm25_k1
    assert detail.bm25_b == config.bm25_b

    dense = [c.dense_cosine for c in result.candidates]
    bm25 = [c.bm25_score for c in result.candidates]
    if dense:
        assert detail.dense_min == pytest.approx(min(dense))
        assert detail.dense_max == pytest.approx(max(dense))
        assert detail.dense_constant == (min(dense) == max(dense))
    else:
        assert detail.dense_constant
    assert detail.bm25_constant == (len(bm25) < 2 or min(bm25) == max(bm25))

    for candidate in result.candidates:
        assert 0.0 <= candidate.cc80_score <= 1.0
        assert candidate.cc80_score == pytest.approx(
            config.semantic_dense_weight * candidate.dense_normalized
            + (1 - config.semantic_dense_weight) * candidate.bm25_normalized
        )


@pytest.mark.parametrize("aspect", ASPECTS)
def test_packing_decisions_are_ordered_and_complete(tmp_path, aspect):
    """Every long-term proposal produces a decision, in the order made."""
    config = replace(EpisodicConfig(), aspect_enabled=aspect)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"packing{int(aspect)}.sqlite",
        embedder,
        config,
        make_episodes(40),
    )
    try:
        result = _retrieve(store, config, "budget question 2", 1_500)
    finally:
        store.close()

    decisions = result.packing.decisions
    assert [d.order for d in decisions] == list(range(1, len(decisions) + 1))

    admitted = {d.candidate_id for d in decisions if d.admitted}
    delivered_via_recency = {
        c.id for c in result.candidates if c.delivered and c.delivered_via == "recency"
    }
    delivered = {c.id for c in result.candidates if c.delivered}
    assert admitted == delivered - delivered_via_recency

    by_id = {c.id: c for c in result.candidates}
    half = result.packing.half_chars
    for decision in decisions:
        assert decision.reason
        # The admission charge is the rendered element plus one separator.
        assert decision.cost_chars == by_id[decision.candidate_id].render_chars + 1
        if decision.admitted:
            # Each phase is governed by its own allowance: the protected
            # halves by the half, the full and slack walks by the whole.
            if decision.phase in ("initial", "spread"):
                allowance = half
            else:
                allowance = result.packing.budget_chars
            assert decision.payload_chars_after <= max(allowance, 0)


def test_aspect_spread_steps_expose_the_arithmetic(tmp_path):
    """Each greedy choice carries the gain and cost that justified it.

    The half is tight (2,000 of a 4,000 allowance on a 60-episode store),
    so the initial CC80 walk admits only part of the pool and the facet
    saturation has candidates left to spread over.
    """
    config = replace(EpisodicConfig(), aspect_enabled=True)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / "spread.sqlite", embedder, config, make_episodes(60)
    )
    try:
        result = _retrieve(store, config, "painting question 6", 4_000)
    finally:
        store.close()

    detail = result.aspect_detail
    assert detail.mode == "protected"

    steps = detail.steps
    assert steps, "fixture produces no spread admissions"
    assert [s.step for s in steps] == list(range(1, len(steps) + 1))
    assert [s.candidate_id for s in steps] == detail.spread_ids

    by_id = {c.id: c for c in result.candidates}
    for step in steps:
        candidate = by_id[step.candidate_id]
        assert step.source_turn == candidate.turn_number
        assert step.score == pytest.approx(candidate.cc80_score)
        assert step.additive_chars > 0
        assert step.marginal >= 0.0
        assert step.ratio == pytest.approx(step.marginal / step.additive_chars)

    cumulative = [s.cumulative_chars for s in steps]
    assert cumulative == sorted(cumulative)
    covered = [s.covered_total for s in steps]
    assert covered == sorted(covered)

    assert detail.stopping_reason in (
        "no_complete_candidate_fits",
        "no_positive_marginal",
    )
    # The spread's own running accounting ends where its last step ended.
    assert detail.solo_chars == steps[-1].cumulative_chars


@pytest.mark.parametrize(
    "budget,expected",
    [(35, "fallback"), (200, "fallback"), (1_000, "protected")],
)
def test_fallback_when_the_initial_half_admits_nothing(tmp_path, budget, expected):
    """An empty initial pack spends the whole budget on one CC80 walk.

    Budgets 35 and 200 give halves of 17 and 100 characters - less than one
    rendered episode - so the initial half admits nothing and the protected
    path must hand the full allowance to a single CC80 walk.
    """
    config = replace(EpisodicConfig(), aspect_enabled=True)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"fb{budget}.sqlite", embedder, config, make_episodes(40)
    )
    try:
        result = _retrieve(store, config, QUERY, budget)
    finally:
        store.close()

    detail = result.aspect_detail
    assert detail.mode == expected
    if expected == "fallback":
        assert all(p == "full" for p in result.packing.phases)
        assert detail.spread_ids == []
        assert detail.returned_ids == []
        assert detail.steps == []
        assert detail.solo_chars is None
        assert detail.stopping_reason is None
        # the half was still computed; the fallback just did not use it
        assert result.packing.half_chars == int(budget * config.aspect_share)
    else:
        assert result.packing.phases[0] == "initial"
        assert detail.solo_chars is not None


@pytest.mark.parametrize("size", [0, 1, 5, 32])
def test_empty_eligible_set_falls_back(tmp_path, size):
    """A store that fits in the recency window has no long-term business."""
    config = replace(EpisodicConfig(), aspect_enabled=True)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"elig{size}.sqlite", embedder, config, make_episodes(size)
    )
    try:
        result = _retrieve(store, config, QUERY, 32_000)
    finally:
        store.close()

    assert result.aspect_detail.mode == "fallback"
    assert result.packing.decisions == []
    assert result.packing.half_chars == 0
    assert result.report.recency_count == size
    assert result.report.semantic_count == 0
    assert result.report.aspect_count == 0
    assert result.report.episodes_delivered == size
    assert result.verification.trustworthy


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
        query_text="venice question 3",
        query_embedding=query_embedding,
        budget=4_000,
        config=config,
        strict=False,
    )
    assert not result.verification.trustworthy
    assert not result.verification.payload_identical


@pytest.mark.parametrize("aspect", ASPECTS)
def test_constant_vectors_flag_the_inert_component(tmp_path, aspect):
    """All-equal vectors normalise to zeros, flagged rather than diluted."""
    config = replace(EpisodicConfig(), aspect_enabled=aspect)
    embedder = PinnedVectorEmbedder()
    store = build_store(
        tmp_path / f"pinned{int(aspect)}.sqlite",
        embedder,
        config,
        make_episodes(10),
    )
    try:
        result = _retrieve(store, config, "venice question 3", 4_000)
    finally:
        store.close()

    detail = result.cc80_detail
    assert detail.dense_constant
    assert detail.dense_min == pytest.approx(1.0)
    assert detail.dense_max == pytest.approx(1.0)
    assert all(c.dense_normalized == 0.0 for c in result.candidates)
    assert all(c.dense_cosine == pytest.approx(1.0) for c in result.candidates)
    assert result.verification.trustworthy
    # With the dense term inert the ranking rests on BM25 alone, and the
    # rank still totals exactly.
    ranks = sorted(c.cc80_rank for c in result.candidates)
    assert ranks == list(range(1, 11))


def test_exact_score_ties_break_by_turn_then_id(tmp_path):
    """Twin episodes (same vector, same text) order by turn, not by chance."""
    twin_q = "twin question one: what did we decide about twins?"
    twin_a = "twin answer one: we decided twins are identical records."
    pairs = [
        (twin_q, twin_a),
        (twin_q, twin_a),
        *make_episodes(20),
    ]
    config = EpisodicConfig()
    embedder = FakeEmbedder()
    store = build_store(tmp_path / "twins.sqlite", embedder, config, pairs)
    try:
        result = _retrieve(store, config, "twins", 4_000)
    finally:
        store.close()

    twins = [c for c in result.candidates if c.preview.startswith("twin question")]
    assert len(twins) == 2
    first, second = sorted(twins, key=lambda c: c.turn_number)
    # Identical inputs, identical fused score - the tie-breaker decides.
    assert first.cc80_score == second.cc80_score
    assert first.cc80_rank < second.cc80_rank


def test_matches_the_store_context_directly(tmp_path):
    """The shadow's payload equals the store's own context() byte-for-byte."""
    config = replace(EpisodicConfig(), aspect_enabled=True)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / "direct.sqlite", embedder, config, make_episodes(40)
    )
    try:
        episodes = read_episodes(store)
        query = "painting and rainfall, item 7"
        payload, report = store.context(query, budget=6_000)
        vec = embed_solo(store._embedder, query)
        result = retrieve_with_trace(
            episodes=episodes,
            query_text=query,
            query_embedding=vec,
            budget=6_000,
            config=config,
            strict=True,
        )
        assert result.payload == payload
        assert result.context_block.payload == payload
        assert result.report.chars_delivered == report.chars_delivered
        ids = {e["id"] for e in episodes}
        for decision in result.packing.decisions:
            assert decision.candidate_id in ids
            assert decision.phase in ("full", "initial", "spread", "slack")
    finally:
        store.close()


def test_query_vector_is_used_not_recomputed(store, config):
    """The shadow and the authority must score against the same vector."""
    episodes = read_episodes(store)
    query_embedding = embed_solo(store._embedder, "venice question 3")
    before = store._embedder.calls

    retrieve_with_trace(
        episodes=episodes,
        query_text="venice question 3",
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
        *result.tiers,
        result.cc80_detail,
        result.aspect_detail,
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
    config = replace(EpisodicConfig(), recency_window_n=window, aspect_enabled=True)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / f"w{window}.sqlite", embedder, config, make_episodes(40)
    )
    try:
        result = _retrieve(store, config, "venice question 3", 6_000)
    finally:
        store.close()
    assert result.verification.trustworthy
    assert result.report.recency_count == min(window, 40)


def test_vectors_are_float32_and_pinned_width(store):
    """Guards the assumption every cosine in the trace rests on."""
    vector = embed_solo(store._embedder, "shape check")
    assert vector.dtype == np.float32
    assert vector.shape == (1024,)


def test_empty_store_is_handled(tmp_path):
    """A first turn has nothing to retrieve and must still trace cleanly."""
    config = replace(EpisodicConfig(), aspect_enabled=True)
    embedder = FakeEmbedder()
    store = build_store(tmp_path / "empty.sqlite", embedder, config, [])
    try:
        result = _retrieve(store, config, "anything at all", 32_000)
    finally:
        store.close()

    assert result.candidates == []
    assert all(tier.proposed_ids == [] for tier in result.tiers)
    assert result.report.episodes_delivered == 0
    assert result.aspect_detail.mode == "fallback"
    assert result.verification.trustworthy


def test_recent_block_is_additive(tmp_path):
    """The recent block costs chars without eating the long-term budget."""
    config = replace(EpisodicConfig(), aspect_enabled=True)
    embedder = FakeEmbedder()
    store = build_store(
        tmp_path / "additive.sqlite", embedder, config, make_episodes(40)
    )
    try:
        episodes = read_episodes(store)
        query = "venice"
        vec = embed_solo(store._embedder, query)
        recent = episodes[-32:]
        for budget in (32_000, 4_000, 34, 0):
            result = _retrieve(store, config, query, budget, query_embedding=vec)
            r = result.report
            assert r.chars_delivered == len(result.payload)
            # recency is additive: it can only add chars, never displace
            # long-term chars
            assert r.chars_delivered >= r.retrieval_chars_delivered
            if budget >= EMPTY_PAYLOAD_CHARS:
                # the delta is exactly the rendered recent block
                delta = len(render_stm_payload(recent, [])) - len(
                    render_stm_payload([], [])
                )
                assert r.chars_delivered - r.retrieval_chars_delivered == delta
            else:
                # below the empty-tag cost the long-term block is empty
                assert r.semantic_count == 0
                assert r.aspect_count == 0
                assert r.chars_delivered == len(render_stm_payload(recent, []))
    finally:
        store.close()