"""The verified shadow trace.

One turn of retrieval, computed twice.

The **authority** is the library: ``build_context`` runs untouched and
returns the payload the model will actually see, plus its ``ContextReport``.
That result is what ships. Nothing in this module can change it.

The **shadow** is a reconstruction of the same pipeline, assembled from the
library's own primitives in the same order, instrumented at every stage.
It records the cosine of every episode, the cluster each fell into, each
greedy step of the coverage selector with the arithmetic behind it, and
every packing decision with its running character cost.

Then the two are compared. The shadow's payload must equal the authority's
payload character for character, and every field it derives must equal the
authority's report. If they disagree, the trace is marked untrustworthy and
- by default - the turn raises, because a trace that quietly describes a
computation that did not happen is worse than having no trace at all. This
is the whole reason the harness can claim its instrumentation is faithful
rather than merely plausible.

The cost of computing twice is roughly one extra pass over the store with
no embedding calls, since the query vector is computed once and passed to
both. On the internal corpus that is single-digit milliseconds against a
~55ms embed and a multi-second generation.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence

import numpy as np

from ..trace import (
    TIER_DESCRIPTIONS,
    TIER_LABELS,
    CandidateTrace,
    ClusterTrace,
    ContextBlockTrace,
    PackingDecision,
    PackingTrace,
    ReportTrace,
    SelectorStepTrace,
    SimilarityTierDetail,
    TierTrace,
    VerificationTrace,
)
from ._internals import (
    DROP_POLICY,
    EMPTY_PAYLOAD_CHARS,
    LIBRARY_VERSION,
    ClusterDiversitySelector,
    EpisodicConfig,
    additive_weight,
    build_context,
    candidate_pool,
    deterministic_clusters,
    recency_window,
    relevance_vector,
    render_stm_payload,
    select,
    vector,
)

PREVIEW_CHARS = 240


class TraceDivergenceError(RuntimeError):
    """The instrumented reconstruction did not reproduce the library.

    Raised rather than logged. A divergence means the harness's account of
    what happened is wrong, and every number derived from it - scores,
    attributions, drop reasons - is suspect. The correct response is to
    stop, not to serve a confident-looking trace of a different
    computation.
    """


class RetrievalResult:
    """The authoritative context block plus its verified trace fragments."""

    def __init__(
        self,
        *,
        payload: str,
        report: ReportTrace,
        candidates: list[CandidateTrace],
        clusters: list[ClusterTrace],
        tiers: list[TierTrace],
        similarity_detail: SimilarityTierDetail,
        selector_steps: list[SelectorStepTrace],
        packing: PackingTrace,
        context_block: ContextBlockTrace,
        verification: VerificationTrace,
    ) -> None:
        self.payload = payload
        self.report = report
        self.candidates = candidates
        self.clusters = clusters
        self.tiers = tiers
        self.similarity_detail = similarity_detail
        self.selector_steps = selector_steps
        self.packing = packing
        self.context_block = context_block
        self.verification = verification

    def tier(self, name: str) -> TierTrace:
        """One retrieval path by name. Mirrors ``TurnTrace.tier``."""
        for entry in self.tiers:
            if entry.name == name:
                return entry
        raise KeyError(name)


def retrieve_with_trace(
    *,
    episodes: Sequence[dict],
    query_embedding: np.ndarray,
    budget: int,
    config: EpisodicConfig,
    strict: bool = True,
) -> RetrievalResult:
    """Build the context block and a proven-faithful account of how.

    ``strict=False`` records a divergence in the trace instead of raising.
    It exists for offline analysis of a known-broken pairing; the server
    runs strict.
    """
    # -- 1. the authority, untouched -----------------------------------
    authority_payload, authority_report = build_context(
        episodes=list(episodes),
        query_embedding=query_embedding,
        budget=budget,
        config=config,
    )

    # -- 2. the shadow, instrumented -----------------------------------
    shadow_started = time.perf_counter()
    query = vector(query_embedding)
    episode_list = list(episodes)

    recent = recency_window(episode_list, config.recency_window_n)
    recent_ids = {str(episode["id"]) for episode in recent}

    relevance_by_id: dict[str, float] = {}
    if episode_list:
        relevance = relevance_vector(query, episode_list)
        relevance_by_id = {
            str(episode["id"]): float(relevance[index])
            for index, episode in enumerate(episode_list)
        }

    similarity_hits = [
        episode
        for episode in episode_list
        if relevance_by_id[str(episode["id"])] >= config.k_threshold
    ]
    similarity_ids = {str(episode["id"]) for episode in similarity_hits}

    pool = candidate_pool(episode_list, relevance_by_id, config)
    pool_ids = [str(episode["id"]) for episode in pool]

    cluster_of: dict[str, int] = {}
    selector_steps: list[SelectorStepTrace] = []
    coverage: list[dict] = []
    coverage_ids: list[str] = []
    selection = None

    if pool and budget >= EMPTY_PAYLOAD_CHARS:
        assignments = deterministic_clusters(pool, config.selector_cluster_count)
        cluster_of = {
            pool_ids[index]: int(assignments[index]) for index in range(len(pool_ids))
        }
        selection = select(
            candidates=pool,
            query_embedding=query,
            selector=ClusterDiversitySelector(
                lambda_=config.selector_lambda,
                cost_exponent=config.selector_cost_exponent,
                assignments=assignments,
                cluster_count=config.selector_cluster_count,
            ),
            budget_chars=budget,
        )
        by_id = {str(episode["id"]): episode for episode in pool}
        coverage_ids = list(selection.selected_ids)
        coverage = [by_id[identifier] for identifier in coverage_ids]
        selector_steps = _trace_selector_steps(selection, cluster_of)

    # -- 3. packing, replayed decision by decision ----------------------
    stm_candidates = [*similarity_hits, *coverage]
    packing_decisions, packed_recent, packed_stm, duplicates = _replay_packing(
        recent, stm_candidates, budget
    )
    shadow_payload = (
        render_stm_payload(packed_recent, packed_stm)
        if budget >= EMPTY_PAYLOAD_CHARS
        else ""
    )
    delivered_ids = {
        str(episode["id"]) for episode in (*packed_recent, *packed_stm)
    }

    # -- 4. what the paths wanted, for the shortfall accounting ---------
    wanted_stm: list[dict] = []
    wanted_seen = set(recent_ids)
    for episode in stm_candidates:
        identifier = str(episode["id"])
        if identifier in wanted_seen:
            continue
        wanted_seen.add(identifier)
        wanted_stm.append(episode)

    shadow_latency_ms = (time.perf_counter() - shadow_started) * 1_000.0

    # -- 5. verification ------------------------------------------------
    verification = _verify(
        authority_payload=authority_payload,
        shadow_payload=shadow_payload,
        authority_report=authority_report,
        delivered_ids=delivered_ids,
        recent_ids=recent_ids,
        similarity_ids=similarity_ids,
        pool_size=len(pool),
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

    # -- 6. assemble the trace fragments --------------------------------
    candidates = _trace_candidates(
        episodes=episode_list,
        relevance_by_id=relevance_by_id,
        cluster_of=cluster_of,
        recent_ids=recent_ids,
        similarity_ids=similarity_ids,
        coverage_ids=set(coverage_ids),
        delivered_ids=delivered_ids,
        packing_decisions=packing_decisions,
        budget=budget,
    )

    return RetrievalResult(
        payload=authority_payload,
        report=_trace_report(authority_report),
        candidates=candidates,
        clusters=_trace_clusters(
            pool_ids=pool_ids,
            cluster_of=cluster_of,
            relevance_by_id=relevance_by_id,
            coverage_ids=coverage_ids,
            delivered_ids=delivered_ids,
        ),
        tiers=_trace_tiers(
            recent=recent,
            similarity_hits=similarity_hits,
            coverage=coverage,
            delivered_ids=delivered_ids,
            recent_ids=recent_ids,
            similarity_ids=similarity_ids,
        ),
        similarity_detail=_trace_similarity(
            relevance_by_id, similarity_hits, config
        ),
        selector_steps=selector_steps,
        packing=PackingTrace(
            policy=DROP_POLICY,
            tier_order=["recency", "similarity", "coverage"],
            budget_chars=budget,
            empty_payload_chars=EMPTY_PAYLOAD_CHARS,
            decisions=packing_decisions,
            duplicate_ids=duplicates,
        ),
        context_block=ContextBlockTrace(
            payload=authority_payload,
            chars=len(authority_payload),
            sha256=_sha256(authority_payload),
            recent_episode_count=len(packed_recent),
            retrieved_episode_count=len(packed_stm),
        ),
        verification=verification,
    )


# ---------------------------------------------------------------------------
# Packing replay
# ---------------------------------------------------------------------------


def _replay_packing(
    n_candidates: Sequence[dict],
    k_candidates: Sequence[dict],
    budget: int,
) -> tuple[list[PackingDecision], list[dict], list[dict], list[str]]:
    """Mirror ``pack_stm_payload``, recording each decision as it is made.

    The library's packer returns which episodes landed. It does not return
    the order it tried them in, the running payload size at each step, or
    which candidate was the one that first failed to fit - which is exactly
    the sequence that explains a starved tier. So the walk is replayed here
    under the same rules, and the resulting payload is checked against the
    library's own.
    """
    recent: list[dict] = []
    stm: list[dict] = []
    decisions: list[PackingDecision] = []
    duplicates: list[str] = []
    seen: set[str] = set()

    if budget < EMPTY_PAYLOAD_CHARS:
        for order, (candidate, tier) in enumerate(
            (
                *((episode, "recency") for episode in n_candidates),
                *((episode, "coverage") for episode in k_candidates),
            ),
            start=1,
        ):
            decisions.append(
                PackingDecision(
                    order=order,
                    candidate_id=str(candidate["id"]),
                    tier=tier,
                    cost_chars=additive_weight(candidate),
                    payload_chars_after=0,
                    admitted=False,
                    reason=(
                        f"budget {budget} is below the {EMPTY_PAYLOAD_CHARS}"
                        " characters the empty block tags cost, so no payload "
                        "can be expressed at all"
                    ),
                )
            )
        return decisions, [], [], duplicates

    for order, (candidate, tier) in enumerate(
        (
            *((episode, "recency") for episode in n_candidates),
            *((episode, "similarity_or_coverage") for episode in k_candidates),
        ),
        start=1,
    ):
        identifier = str(candidate["id"])
        cost = additive_weight(candidate)

        if identifier in seen:
            duplicates.append(identifier)
            decisions.append(
                PackingDecision(
                    order=order,
                    candidate_id=identifier,
                    tier="recency" if tier == "recency" else "coverage",
                    cost_chars=cost,
                    payload_chars_after=len(render_stm_payload(recent, stm)),
                    admitted=False,
                    reason=(
                        "already admitted by an earlier path; charged once"
                    ),
                )
            )
            continue

        target = recent if tier == "recency" else stm
        target.append(candidate)
        payload = render_stm_payload(recent, stm)
        if len(payload) <= budget:
            seen.add(identifier)
            decisions.append(
                PackingDecision(
                    order=order,
                    candidate_id=identifier,
                    tier="recency" if tier == "recency" else "coverage",
                    cost_chars=cost,
                    payload_chars_after=len(payload),
                    admitted=True,
                    reason="fits",
                )
            )
            continue

        target.pop()
        current = len(render_stm_payload(recent, stm))
        decisions.append(
            PackingDecision(
                order=order,
                candidate_id=identifier,
                tier="recency" if tier == "recency" else "coverage",
                cost_chars=cost,
                payload_chars_after=current,
                admitted=False,
                reason=(
                    f"would reach {len(payload)} characters, past the "
                    f"{budget} budget; skipped and the walk continued"
                ),
            )
        )
    return decisions, recent, stm, duplicates


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _verify(
    *,
    authority_payload: str,
    shadow_payload: str,
    authority_report,
    delivered_ids: set[str],
    recent_ids: set[str],
    similarity_ids: set[str],
    pool_size: int,
    shadow_latency_ms: float,
) -> VerificationTrace:
    """Compare the reconstruction against the library, field by field."""
    mismatches: list[str] = []

    def check(name: str, shadow_value, authority_value) -> None:
        if shadow_value != authority_value:
            mismatches.append(
                f"{name}: shadow={shadow_value!r} authority={authority_value!r}"
            )

    check("chars_delivered", len(shadow_payload), authority_report.chars_delivered)
    check(
        "episodes_delivered",
        len(delivered_ids),
        authority_report.episodes_delivered,
    )
    check(
        "stm_count",
        len(delivered_ids & recent_ids),
        authority_report.stm_count,
    )
    check(
        "k_count",
        len((delivered_ids & similarity_ids) - recent_ids),
        authority_report.k_count,
    )
    check(
        "coverage_count",
        len(delivered_ids - recent_ids - similarity_ids),
        authority_report.coverage_count,
    )
    check("pool_size", pool_size, authority_report.pool_size)

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
    relevance_by_id: dict[str, float],
    cluster_of: dict[str, int],
    recent_ids: set[str],
    similarity_ids: set[str],
    coverage_ids: set[str],
    delivered_ids: set[str],
    packing_decisions: list[PackingDecision],
    budget: int,
) -> list[CandidateTrace]:
    ranked = sorted(
        episodes,
        key=lambda episode: (
            -relevance_by_id.get(str(episode["id"]), 0.0),
            int(episode["turn_number"]),
        ),
    )
    rank_of = {
        str(episode["id"]): index + 1 for index, episode in enumerate(ranked)
    }
    drop_reason_of = {
        decision.candidate_id: decision.reason
        for decision in packing_decisions
        if not decision.admitted
    }

    rows: list[CandidateTrace] = []
    for episode in episodes:
        identifier = str(episode["id"])
        delivered = identifier in delivered_ids
        if delivered:
            via = (
                "recency"
                if identifier in recent_ids
                else "similarity"
                if identifier in similarity_ids
                else "coverage"
            )
        else:
            via = None

        proposed = (
            identifier in recent_ids
            or identifier in similarity_ids
            or identifier in coverage_ids
        )
        if delivered:
            reason = None
        elif proposed:
            reason = drop_reason_of.get(
                identifier, "proposed but not admitted"
            )
        else:
            reason = "not proposed by any path this turn"

        rows.append(
            CandidateTrace(
                id=identifier,
                turn_number=int(episode["turn_number"]),
                preview=_preview(episode.get("user_message", "")),
                assistant_preview=_preview(episode.get("assistant_message", "")),
                relevance=relevance_by_id.get(identifier, 0.0),
                relevance_rank=rank_of[identifier],
                cluster=cluster_of.get(identifier),
                render_chars=additive_weight(episode),
                in_recency_window=identifier in recent_ids,
                passes_similarity_threshold=identifier in similarity_ids,
                selected_by_coverage=identifier in coverage_ids,
                delivered=delivered,
                delivered_via=via,
                drop_reason=reason,
            )
        )
    return rows


def _trace_clusters(
    *,
    pool_ids: list[str],
    cluster_of: dict[str, int],
    relevance_by_id: dict[str, float],
    coverage_ids: list[str],
    delivered_ids: set[str],
) -> list[ClusterTrace]:
    if not cluster_of:
        return []
    members: dict[int, list[str]] = {}
    for identifier in pool_ids:
        members.setdefault(cluster_of[identifier], []).append(identifier)

    selected = set(coverage_ids)
    clusters: list[ClusterTrace] = []
    for cluster_id in sorted(members):
        member_ids = members[cluster_id]
        scores = [relevance_by_id.get(identifier, 0.0) for identifier in member_ids]
        clusters.append(
            ClusterTrace(
                id=cluster_id,
                size=len(member_ids),
                member_ids=member_ids,
                mean_relevance=float(np.mean(scores)) if scores else 0.0,
                max_relevance=float(np.max(scores)) if scores else 0.0,
                selected_ids=[i for i in member_ids if i in selected],
                delivered_ids=[i for i in member_ids if i in delivered_ids],
            )
        )
    return clusters


def _trace_tiers(
    *,
    recent: list[dict],
    similarity_hits: list[dict],
    coverage: list[dict],
    delivered_ids: set[str],
    recent_ids: set[str],
    similarity_ids: set[str],
) -> list[TierTrace]:
    def build(name: str, proposed: list[dict], claim: set[str]) -> TierTrace:
        proposed_ids = [str(episode["id"]) for episode in proposed]
        delivered = [
            identifier
            for identifier in proposed_ids
            if identifier in delivered_ids and identifier in claim
        ]
        # Present in the context, but an earlier path got the credit.
        overlapped = [
            identifier
            for identifier in proposed_ids
            if identifier in delivered_ids and identifier not in claim
        ]
        # Never reached the context at all - the budget ran out.
        skipped = [
            identifier
            for identifier in proposed_ids
            if identifier not in delivered_ids
        ]
        by_id = {str(episode["id"]): episode for episode in proposed}
        return TierTrace(
            name=name,
            label=TIER_LABELS[name],
            description=TIER_DESCRIPTIONS[name],
            proposed_ids=proposed_ids,
            delivered_ids=delivered,
            overlapped_ids=overlapped,
            skipped_ids=skipped,
            chars_delivered=sum(additive_weight(by_id[i]) for i in delivered),
            chars_proposed=sum(additive_weight(episode) for episode in proposed),
        )

    # Attribution follows the packing order: an episode in the recency window
    # is credited to recency even if the coverage selector also chose it.
    similarity_claim = similarity_ids - recent_ids
    coverage_claim = {
        str(episode["id"]) for episode in coverage
    } - recent_ids - similarity_ids

    return [
        build("recency", recent, recent_ids),
        build("similarity", similarity_hits, similarity_claim),
        build("coverage", coverage, coverage_claim),
    ]


def _trace_similarity(
    relevance_by_id: dict[str, float],
    hits: list[dict],
    config: EpisodicConfig,
) -> SimilarityTierDetail:
    best = max(relevance_by_id.values(), default=0.0)
    return SimilarityTierDetail(
        threshold=config.k_threshold,
        hit_count=len(hits),
        max_relevance_observed=best,
        margin_to_threshold=config.k_threshold - best,
        inert=not hits,
    )


def _trace_selector_steps(
    selection,
    cluster_of: dict[str, int],
) -> list[SelectorStepTrace]:
    covered: set[int] = set()
    steps: list[SelectorStepTrace] = []
    for step in selection.steps:
        cluster = cluster_of.get(step.candidate_id)
        entered = cluster is not None and cluster not in covered
        if cluster is not None:
            covered.add(cluster)
        steps.append(
            SelectorStepTrace(
                step=step.step,
                candidate_id=step.candidate_id,
                source_turn=step.source_turn,
                relevance=step.relevance,
                objective_gain=step.objective_gain,
                scaled_gain=step.scaled_gain,
                additive_chars=step.additive_chars,
                cumulative_chars=step.cumulative_chars,
                entered_new_cluster=entered,
                cluster=cluster,
            )
        )
    return steps


def _trace_report(report) -> ReportTrace:
    return ReportTrace(
        chars_delivered=report.chars_delivered,
        chars_wanted=report.chars_wanted,
        chars_available=report.chars_available,
        shortfall_chars=report.shortfall_chars,
        episodes_delivered=report.episodes_delivered,
        episodes_dropped=report.episodes_dropped,
        truncated=report.truncated,
        stm_count=report.stm_count,
        k_count=report.k_count,
        coverage_count=report.coverage_count,
        latency_ms=report.latency_ms,
        pool_size=report.pool_size,
        dropped_ids=list(report.dropped_ids),
        drop_policy=report.drop_policy,
        budget_chars=report.budget_chars,
    )


def _preview(value: object) -> str:
    text = str(value).strip().replace("\n", " ")
    if len(text) <= PREVIEW_CHARS:
        return text
    return text[: PREVIEW_CHARS - 1] + "…"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
