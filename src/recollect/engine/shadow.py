"""The verified shadow trace.

One turn of retrieval, computed twice.

The **authority** is the library: ``build_chat_context`` runs untouched and
returns the payload the model will actually see, plus its ``ContextReport``.
That result is what ships. Nothing in this module can change it.

The **shadow** is a reconstruction of the same pipeline, assembled from the
library's own primitives in the same order, instrumented at every stage. It
records the dense cosine and BM25 term of every episode, the fusion scaling
each went through, the CC80 rank, every ASPECT saturation step with the
marginal arithmetic behind it, and every packing decision with its running
character cost, across the full/initial/spread/slack phases the turn took.

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
    AspectDetail,
    AspectStepTrace,
    CandidateTrace,
    CC80Detail,
    ContextBlockTrace,
    PackingDecision,
    PackingTrace,
    ReportTrace,
    TierName,
    TierTrace,
    VerificationTrace,
)
from ._internals import (
    DROP_POLICY,
    EMPTY_PAYLOAD_CHARS,
    LIBRARY_VERSION,
    EpisodicConfig,
    additive_weight,
    aspect_spread,
    build_chat_context,
    prepare_facets,
    rank_cc80,
    recency_window,
    render_episode_element,
    render_stm_payload,
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
        tiers: list[TierTrace],
        cc80_detail: CC80Detail,
        aspect_detail: AspectDetail,
        packing: PackingTrace,
        context_block: ContextBlockTrace,
        verification: VerificationTrace,
    ) -> None:
        self.payload = payload
        self.report = report
        self.candidates = candidates
        self.tiers = tiers
        self.cc80_detail = cc80_detail
        self.aspect_detail = aspect_detail
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
    query_text: str,
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
    authority_payload, authority_report = build_chat_context(
        episodes=list(episodes),
        query_text=query_text,
        query_embedding=query_embedding,
        budget=budget,
        config=config,
    )

    # -- 2. the shadow, instrumented -----------------------------------
    shadow_started = time.perf_counter()
    episode_list = list(episodes)
    episode_ids = [str(episode["id"]) for episode in episode_list]

    recent = recency_window(episode_list, config.recency_window_n)
    recent_ids = tuple(str(episode["id"]) for episode in recent)
    recent_set = set(recent_ids)

    ranking = rank_cc80(
        episode_list,
        query_text,
        query_embedding,
        dense_weight=config.semantic_dense_weight,
        bm25_k1=config.bm25_k1,
        bm25_b=config.bm25_b,
    )
    eligible_order = tuple(
        index
        for index in ranking.order
        if episode_ids[index] not in recent_set
    )
    excluded_indices = tuple(
        index for index, identifier in enumerate(episode_ids)
        if identifier in recent_set
    )
    effective_budget = max(budget, 0)

    decisions: list[PackingDecision] = []
    final_records: list[dict] = []
    initial_ids: list[str] = []
    aspect_ids: list[str] = []
    aspect_input_ids: list[str] = []
    returned_ids: list[str] = []
    aspect_trace_obj = None
    aspect_steps: list[AspectStepTrace] = []
    facet_latency_ms: float | None = None
    half_chars = 0
    mode = "off" if not config.aspect_enabled else "protected"

    def append_pack(
        candidates: Sequence[dict],
        *,
        allowance: int,
        phase: str,
        tier: TierName,
    ) -> list[str]:
        return _replay_pack(
            candidates,
            allowance=allowance,
            phase=phase,
            tier=tier,
            decisions=decisions,
        )

    by_id = {
        identifier: episode
        for identifier, episode in zip(episode_ids, episode_list, strict=True)
    }
    eligible_records = [episode_list[index] for index in eligible_order]
    eligible_ids = [episode_ids[index] for index in eligible_order]

    if not config.aspect_enabled or not eligible_order:
        # No spread at all: one CC80 walk owns the whole allowance.
        if config.aspect_enabled:
            mode = "fallback"
        initial_ids = append_pack(
            eligible_records, allowance=effective_budget, phase="full", tier="semantic"
        )
        final_records = [by_id[identifier] for identifier in initial_ids]
    else:
        half = int(effective_budget * config.aspect_share)
        half_chars = half
        fallback_start = len(decisions)
        initial_ids = append_pack(
            eligible_records, allowance=half, phase="initial", tier="semantic"
        )
        if not initial_ids:
            # The initial half admitted nothing: the library spends the full
            # allowance on one CC80 walk instead, and the spread never runs.
            mode = "fallback"
            del decisions[fallback_start:]
            initial_ids = append_pack(
                eligible_records,
                allowance=effective_budget,
                phase="full",
                tier="semantic",
            )
            final_records = [by_id[identifier] for identifier in initial_ids]
        else:
            facet_started = time.perf_counter()
            facets, idf, _families = prepare_facets(
                episode_list, config.aspect_model
            )
            facet_latency_ms = (time.perf_counter() - facet_started) * 1_000.0
            index_of = {
                identifier: index for index, identifier in enumerate(episode_ids)
            }
            initial_indices = tuple(index_of[identifier] for identifier in initial_ids)
            aspect_trace_obj = aspect_spread(
                episode_list,
                facets,
                idf,
                ranking.scores,
                ranking.order,
                initial_indices,
                half,
                excluded=excluded_indices,
            )
            aspect_steps = _trace_aspect_steps(
                aspect_trace_obj, episode_list, ranking.scores
            )
            admitted_set = set(initial_ids)
            spread_candidates = [
                episode_list[index]
                for index in aspect_trace_obj.order
                if episode_ids[index] not in admitted_set
            ]
            aspect_input_ids = [str(ep["id"]) for ep in spread_candidates]
            aspect_ids = append_pack(
                spread_candidates, allowance=half, phase="spread", tier="aspect"
            )
            admitted_set.update(aspect_ids)
            final_records = [
                by_id[identifier]
                for identifier in (*initial_ids, *aspect_ids)
            ]
            current_chars = len(render_stm_payload([], final_records))
            for index in eligible_order:
                identifier = episode_ids[index]
                if identifier in admitted_set:
                    continue
                cost = additive_weight(episode_list[index])
                if current_chars + cost <= effective_budget:
                    final_records.append(episode_list[index])
                    admitted_set.add(identifier)
                    returned_ids.append(identifier)
                    current_chars += cost
                    decisions.append(
                        PackingDecision(
                            order=len(decisions) + 1,
                            candidate_id=identifier,
                            tier="semantic",
                            phase="slack",
                            cost_chars=cost,
                            payload_chars_after=current_chars,
                            admitted=True,
                            reason="fits the remaining budget; returned",
                        )
                    )
                else:
                    decisions.append(
                        PackingDecision(
                            order=len(decisions) + 1,
                            candidate_id=identifier,
                            tier="semantic",
                            phase="slack",
                            cost_chars=cost,
                            payload_chars_after=current_chars,
                            admitted=False,
                            reason=(
                                f"would reach {current_chars + cost} "
                                f"characters, past the {effective_budget} "
                                "budget; skipped and the walk continued"
                            ),
                        )
                    )

    final_ids = tuple(str(record["id"]) for record in final_records)
    dropped_ids = tuple(
        episode_ids[index] for index in eligible_order
        if episode_ids[index] not in set(final_ids)
    )
    shadow_retrieval_payload = (
        render_stm_payload([], final_records)
        if effective_budget >= EMPTY_PAYLOAD_CHARS
        else ""
    )
    shadow_payload = render_stm_payload(recent, final_records)

    # -- 3. what the selection would have wanted, for shortfall ---------
    chars_wanted = (
        EMPTY_PAYLOAD_CHARS
        if not eligible_records
        else len(render_stm_payload([], eligible_records))
    )

    shadow_latency_ms = (time.perf_counter() - shadow_started) * 1_000.0

    # -- 4. verification ------------------------------------------------
    verification = _verify(
        authority_payload=authority_payload,
        shadow_payload=shadow_payload,
        authority_report=authority_report,
        recent_ids=recent_ids,
        final_ids=final_ids,
        dropped_ids=dropped_ids,
        initial_count=len(initial_ids),
        aspect_count=len(aspect_ids),
        returned_count=len(returned_ids),
        retrieval_chars=len(shadow_retrieval_payload),
        chars_wanted=chars_wanted,
        pool_size=len(episode_list),
        budget=budget,
        aspect_enabled=config.aspect_enabled,
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

    # -- 5. assemble the trace fragments --------------------------------
    phases: list[str] = []
    for decision in decisions:
        if decision.phase not in phases:
            phases.append(decision.phase)

    return RetrievalResult(
        payload=authority_payload,
        report=_trace_report(authority_report),
        candidates=_trace_candidates(
            episodes=episode_list,
            ranking=ranking,
            recent_set=recent_set,
            initial_ids=set(initial_ids),
            aspect_ids=set(aspect_ids),
            returned_ids=set(returned_ids),
            final_ids=set(final_ids),
            packing_decisions=decisions,
        ),
        tiers=_trace_tiers(
            recent=recent,
            eligible_ids=eligible_ids,
            initial_ids=initial_ids,
            returned_ids=returned_ids,
            aspect_input_ids=aspect_input_ids,
            aspect_ids=aspect_ids,
            context_ids=recent_set | set(final_ids),
            by_id=by_id,
        ),
        cc80_detail=_trace_cc80(ranking, config),
        aspect_detail=AspectDetail(
            enabled=config.aspect_enabled,
            share=config.aspect_share,
            model=config.aspect_model,
            mode=mode,
            facet_latency_ms=facet_latency_ms,
            initial_ids=list(initial_ids),
            spread_ids=list(aspect_ids),
            returned_ids=list(returned_ids),
            solo_chars=(
                aspect_trace_obj.solo_chars if aspect_trace_obj is not None else None
            ),
            stopping_reason=(
                aspect_trace_obj.stopping_reason
                if aspect_trace_obj is not None
                else None
            ),
            steps=aspect_steps,
        ),
        packing=PackingTrace(
            policy=DROP_POLICY,
            phases=phases,
            budget_chars=budget,
            half_chars=half_chars,
            empty_payload_chars=EMPTY_PAYLOAD_CHARS,
            decisions=decisions,
        ),
        context_block=ContextBlockTrace(
            payload=authority_payload,
            chars=len(authority_payload),
            sha256=_sha256(authority_payload),
            recent_episode_count=len(recent),
            retrieved_episode_count=len(final_records),
        ),
        verification=verification,
    )


# ---------------------------------------------------------------------------
# Packing replay
# ---------------------------------------------------------------------------


def _replay_pack(
    candidates: Sequence[dict],
    *,
    allowance: int,
    phase: str,
    tier: TierName,
    decisions: list[PackingDecision],
) -> list[str]:
    """Mirror the library's packer, recording each decision as it is made.

    The library's packer returns which episodes landed. It does not return
    the order it tried them in, the running payload size at each step, or
    which candidate was the one that first failed to fit - which is exactly
    the sequence that explains a starved tier. So the walk is replayed here
    under the same rules: a candidate is tentatively appended, the whole
    block is rendered, and it is kept only if the result still fits. The
    resulting payload is checked against the library's own.
    """
    admitted: list[str] = []
    records: list[dict] = []
    seen: set[str] = set()
    current_chars = 0

    for candidate in candidates:
        identifier = str(candidate["id"])
        cost = additive_weight(candidate)

        if identifier in seen:
            decisions.append(
                PackingDecision(
                    order=len(decisions) + 1,
                    candidate_id=identifier,
                    tier=tier,
                    phase=phase,
                    cost_chars=cost,
                    payload_chars_after=current_chars,
                    admitted=False,
                    reason="already admitted by an earlier phase; charged once",
                )
            )
            continue

        records.append(candidate)
        payload = render_stm_payload([], records)
        if len(payload) <= allowance:
            seen.add(identifier)
            admitted.append(identifier)
            current_chars = len(payload)
            decisions.append(
                PackingDecision(
                    order=len(decisions) + 1,
                    candidate_id=identifier,
                    tier=tier,
                    phase=phase,
                    cost_chars=cost,
                    payload_chars_after=current_chars,
                    admitted=True,
                    reason="fits",
                )
            )
            continue

        records.pop()
        decisions.append(
            PackingDecision(
                order=len(decisions) + 1,
                candidate_id=identifier,
                tier=tier,
                phase=phase,
                cost_chars=cost,
                payload_chars_after=current_chars,
                admitted=False,
                reason=(
                    f"would reach {len(payload)} characters, past the "
                    f"{allowance} character allowance; skipped and the "
                    "walk continued"
                ),
            )
        )
    return admitted


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _verify(
    *,
    authority_payload: str,
    shadow_payload: str,
    authority_report,
    recent_ids: tuple[str, ...],
    final_ids: tuple[str, ...],
    dropped_ids: tuple[str, ...],
    initial_count: int,
    aspect_count: int,
    returned_count: int,
    retrieval_chars: int,
    chars_wanted: int,
    pool_size: int,
    budget: int,
    aspect_enabled: bool,
    shadow_latency_ms: float,
) -> VerificationTrace:
    """Compare the reconstruction against the library, field by field.

    Every field is the shadow's independently re-derived value against the
    authority's report. Latency is the one field excluded, for the same
    reason it never was: wall time is not byte-reproducible.
    """
    mismatches: list[str] = []

    def check(name: str, shadow_value, authority_value) -> None:
        if shadow_value != authority_value:
            mismatches.append(
                f"{name}: shadow={shadow_value!r} authority={authority_value!r}"
            )

    check("chars_delivered", len(shadow_payload), authority_report.chars_delivered)
    check("chars_wanted", chars_wanted, authority_report.chars_wanted)
    check(
        "episodes_delivered",
        len(recent_ids) + len(final_ids),
        authority_report.episodes_delivered,
    )
    check(
        "episodes_dropped",
        len(dropped_ids),
        authority_report.episodes_dropped,
    )
    check("truncated", bool(dropped_ids), authority_report.truncated)
    check("stm_count", len(recent_ids), authority_report.stm_count)
    check(
        "k_count",
        initial_count + returned_count,
        authority_report.k_count,
    )
    check("coverage_count", aspect_count, authority_report.coverage_count)
    check("recency_count", len(recent_ids), authority_report.recency_count)
    check(
        "semantic_count",
        initial_count + returned_count,
        authority_report.semantic_count,
    )
    check("aspect_count", aspect_count, authority_report.aspect_count)
    check(
        "returned_semantic_count",
        returned_count,
        authority_report.returned_semantic_count,
    )
    check("pool_size", pool_size, authority_report.pool_size)
    check("dropped_ids", dropped_ids, tuple(authority_report.dropped_ids))
    check("drop_policy", DROP_POLICY, authority_report.drop_policy)
    check("budget_chars", budget, authority_report.budget_chars)
    check(
        "retrieval_chars_delivered",
        retrieval_chars,
        authority_report.retrieval_chars_delivered,
    )
    check(
        "retrieval_budget_chars",
        budget,
        authority_report.retrieval_budget_chars,
    )
    check("recent_ids", recent_ids, tuple(authority_report.recent_ids))
    check("recency_additive", True, authority_report.recency_additive)
    check("aspect_enabled", aspect_enabled, authority_report.aspect_enabled)

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
    ranking,
    recent_set: set[str],
    initial_ids: set[str],
    aspect_ids: set[str],
    returned_ids: set[str],
    final_ids: set[str],
    packing_decisions: list[PackingDecision],
) -> list[CandidateTrace]:
    rank_of = {
        index: position + 1
        for position, index in enumerate(ranking.order)
    }
    drop_reason_of: dict[str, str] = {}
    for decision in packing_decisions:
        if not decision.admitted:
            drop_reason_of[decision.candidate_id] = decision.reason

    rows: list[CandidateTrace] = []
    for position, episode in enumerate(episodes):
        identifier = str(episode["id"])
        delivered = identifier in final_ids or identifier in recent_set
        if delivered:
            via: TierName | None = (
                "recency"
                if identifier in recent_set
                else "semantic"
                if identifier in initial_ids or identifier in returned_ids
                else "aspect"
            )
        else:
            via = None

        if delivered:
            reason = None
        else:
            # Every non-recent candidate is proposed by the semantic walk
            # this turn, so an undelivered candidate always has a recorded
            # last refusal - the get() default is defensive, not a branch.
            reason = drop_reason_of.get(identifier, "proposed but not admitted")

        rows.append(
            CandidateTrace(
                id=identifier,
                turn_number=int(episode["turn_number"]),
                preview=_preview(episode.get("user_message", "")),
                assistant_preview=_preview(episode.get("assistant_message", "")),
                dense_cosine=float(ranking.dense_scores[position]),
                dense_normalized=float(ranking.dense_normalized[position]),
                bm25_score=float(ranking.bm25_scores[position]),
                bm25_normalized=float(ranking.bm25_normalized[position]),
                cc80_score=float(ranking.scores[position]),
                cc80_rank=rank_of[position],
                render_chars=len(render_episode_element(episode)),
                in_recency_window=identifier in recent_set,
                in_semantic_initial=identifier in initial_ids,
                selected_by_aspect=identifier in aspect_ids,
                returned_semantic=identifier in returned_ids,
                delivered=delivered,
                delivered_via=via,
                drop_reason=reason,
            )
        )
    return rows


def _trace_tiers(
    *,
    recent: list[dict],
    eligible_ids: list[str],
    initial_ids: list[str],
    returned_ids: list[str],
    aspect_input_ids: list[str],
    aspect_ids: list[str],
    context_ids: set[str],
    by_id: dict[str, dict],
) -> list[TierTrace]:
    """What each path proposed, and what became of it.

    ``context_ids`` is the complete in-context set: the additive recent
    window plus the long-term admissions. The long-term-only tuple would
    misfile every recent episode as "never reached the context".

    The tiers are near-disjoint by construction: recency is excluded from
    long-term admission, and the admitted set deduplicates across the
    initial, spread, and slack phases. The overlap that remains is exactly
    the story worth keeping: a spread candidate that did not fit its half
    yet still reached the context through the slack return, credited to the
    path that actually admitted it.
    """
    ids = [
        *[str(episode["id"]) for episode in recent],
        *eligible_ids,
        *aspect_input_ids,
    ]
    weights = {
        identifier: additive_weight(by_id[identifier])
        for identifier in dict.fromkeys(ids)
    }
    recent_claim = {str(episode["id"]) for episode in recent}

    def build(
        name: TierName,
        proposed_ids: list[str],
        claim: set[str],
    ) -> TierTrace:
        # Reached the context AND credited to this path.
        delivered = [
            identifier
            for identifier in proposed_ids
            if identifier in context_ids and identifier in claim
        ]
        # Present in the context, but an earlier path got the credit.
        overlapped = [
            identifier
            for identifier in proposed_ids
            if identifier in context_ids and identifier not in claim
        ]
        # Never reached the context at all.
        skipped = [
            identifier
            for identifier in proposed_ids
            if identifier not in context_ids
        ]
        return TierTrace(
            name=name,
            label=TIER_LABELS[name],
            description=TIER_DESCRIPTIONS[name],
            proposed_ids=proposed_ids,
            delivered_ids=delivered,
            overlapped_ids=overlapped,
            skipped_ids=skipped,
            chars_delivered=sum(weights[i] for i in delivered),
            chars_proposed=sum(weights.get(i, 0) for i in proposed_ids),
        )

    # Attribution follows decision order: recency first (additive, outside
    # the allowance), then the semantic phase admissions in the order they
    # happened (initial, then slack return), and finally the spread-only
    # admissions.
    semantic_claim = set(initial_ids) | set(returned_ids)
    aspect_claim = set(aspect_ids)
    return [
        build("recency", [str(episode["id"]) for episode in recent], recent_claim),
        build("semantic", eligible_ids, semantic_claim),
        build("aspect", aspect_input_ids, aspect_claim),
    ]


def _trace_cc80(ranking, config: EpisodicConfig) -> CC80Detail:
    dense = ranking.dense_scores
    sparse = ranking.bm25_scores
    dense_min = float(min(dense)) if dense else 0.0
    dense_max = float(max(dense)) if dense else 0.0
    sparse_min = float(min(sparse)) if sparse else 0.0
    sparse_max = float(max(sparse)) if sparse else 0.0
    return CC80Detail(
        dense_weight=config.semantic_dense_weight,
        bm25_k1=config.bm25_k1,
        bm25_b=config.bm25_b,
        dense_min=dense_min,
        dense_max=dense_max,
        dense_constant=dense_max - dense_min <= 0.0,
        bm25_min=sparse_min,
        bm25_max=sparse_max,
        bm25_constant=sparse_max - sparse_min <= 0.0,
    )


def _trace_aspect_steps(
    aspect_trace_obj,
    episodes: Sequence[dict],
    scores: tuple[float, ...],
) -> list[AspectStepTrace]:
    """Re-derive the per-step ratio and cost the library's trace holds only as sums.

    The library returns the chosen order, each step's marginal, the running
    coverage size, the total characters spent, and why it stopped. The ratio
    (marginal over additive cost) and the per-step running cost are
    recomputed here from the same primitives; the totals they imply are
    checked against the library's ``solo_chars`` by the payload and report
    verification.
    """
    steps: list[AspectStepTrace] = []
    spent = EMPTY_PAYLOAD_CHARS
    for position, index in enumerate(aspect_trace_obj.order):
        episode = episodes[index]
        cost = additive_weight(episode)
        marginal = float(aspect_trace_obj.marginal[position])
        spent += cost
        steps.append(
            AspectStepTrace(
                step=position + 1,
                candidate_id=str(episode["id"]),
                source_turn=int(episode["turn_number"]),
                score=float(scores[index]),
                marginal=marginal,
                ratio=marginal / cost,
                additive_chars=cost,
                cumulative_chars=spent,
                covered_total=int(aspect_trace_obj.covered_counts[position]),
            )
        )
    return steps


def _trace_report(report) -> ReportTrace:
    return ReportTrace(
        chars_delivered=report.chars_delivered,
        chars_wanted=report.chars_wanted,
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
        retrieval_chars_delivered=report.retrieval_chars_delivered,
        retrieval_budget_chars=report.retrieval_budget_chars,
        recency_count=report.recency_count,
        semantic_count=report.semantic_count,
        aspect_count=report.aspect_count,
        returned_semantic_count=report.returned_semantic_count,
        aspect_enabled=report.aspect_enabled,
        recent_ids=list(report.recent_ids),
        recency_additive=report.recency_additive,
    )


def _preview(value: object) -> str:
    text = str(value).strip().replace("\n", " ")
    if len(text) <= PREVIEW_CHARS:
        return text
    return text[: PREVIEW_CHARS - 1] + "…"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
