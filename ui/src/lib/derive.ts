/**
 * The Pydantic `@property` accessors, recomputed.
 *
 * `model_dump()` omits properties, so none of these arrive on the wire. Each
 * function below reproduces the body of the corresponding property in
 * `trace.py` exactly; if that file changes, these change with it.
 */
import type {
  CandidateTrace,
  PackPhase,
  PromptCacheTrace,
  ReportTrace,
  TierName,
  TierTrace,
  TurnTrace,
  VerificationTrace,
} from '../types/trace.ts'
import { TIER_ORDER } from '../types/trace.ts'

/**
 * TierTrace.starved — proposed episodes that never reached the context.
 *
 * Deliberately not "delivered nothing": a path can deliver nothing because
 * an earlier path already claimed everything it proposed, which is overlap,
 * not starvation. Only the budget-exhausted case is the packing-order fault.
 */
export function isStarved(tier: TierTrace): boolean {
  return tier.skipped_ids.length > 0 && tier.delivered_ids.length === 0
}

/** TierTrace.fully_overlapped — everything it proposed was already claimed. */
export function isFullyOverlapped(tier: TierTrace): boolean {
  return (
    tier.proposed_ids.length > 0 &&
    tier.delivered_ids.length === 0 &&
    tier.skipped_ids.length === 0
  )
}

/** TierTrace.contributed — added at least one episode nothing else had. */
export function hasContributed(tier: TierTrace): boolean {
  return tier.delivered_ids.length > 0
}

/** ReportTrace.chars_available — unused long-term allowance. */
export function charsAvailable(report: ReportTrace): number {
  const delivered =
    report.retrieval_chars_delivered === null
      ? report.chars_delivered
      : report.retrieval_chars_delivered
  const budget =
    report.retrieval_budget_chars === null
      ? report.budget_chars
      : report.retrieval_budget_chars
  return budget - delivered
}

/** ReportTrace.shortfall_chars — more allowance the selection would have needed. */
export function shortfallChars(report: ReportTrace): number {
  const delivered =
    report.retrieval_chars_delivered === null
      ? report.chars_delivered
      : report.retrieval_chars_delivered
  return Math.max(0, report.chars_wanted - delivered)
}

/** VerificationTrace.trustworthy */
export function isTrustworthy(v: VerificationTrace): boolean {
  return v.payload_identical && v.report_fields_identical
}

/** PromptCacheTrace.cache_hit_ratio */
export function cacheHitRatio(c: PromptCacheTrace): number | null {
  if (!c.prompt_tokens || c.cached_tokens === null) return null
  return Math.min(1, c.cached_tokens / c.prompt_tokens)
}

/** TurnTrace.starved_tiers */
export function starvedTiers(trace: TurnTrace): TierName[] {
  return trace.tiers.filter(isStarved).map((t) => t.name)
}

/**
 * TurnTrace.budget_utilization — measured on the retrieval pair, not the
 * total: recent continuity is additive and renders outside the allowance.
 */
export function budgetUtilization(trace: TurnTrace): number {
  const budget = trace.report.retrieval_budget_chars
  const delivered = trace.report.retrieval_chars_delivered
  if (!budget || budget <= 0 || delivered === null) return 0
  return delivered / budget
}

/** TurnTrace.tier(name) */
export function tierOf(trace: TurnTrace, name: TierName): TierTrace | undefined {
  return trace.tiers.find((t) => t.name === name)
}

/** Tiers in packing order, regardless of the order the server serialized them. */
export function orderedTiers(trace: TurnTrace): TierTrace[] {
  return TIER_ORDER.map((name) => tierOf(trace, name)).filter(
    (t): t is TierTrace => Boolean(t),
  )
}

/** The phases that ran this turn, if the serialized list is empty. */
export function phasesOf(trace: TurnTrace): PackPhase[] {
  return trace.packing.phases
}

export function candidateIndex(trace: TurnTrace): Map<string, CandidateTrace> {
  const index = new Map<string, CandidateTrace>()
  for (const candidate of trace.candidates) index.set(candidate.id, candidate)
  return index
}

/**
 * Which tier proposed a candidate, for display when it was never delivered.
 * `delivered_via` is null for everything dropped, so the Scores table would
 * otherwise show no tier at all for the rows that matter most. Everything
 * outside the recency window is ranked by CC80, so semantic proposes it.
 */
export function proposingTiers(candidate: CandidateTrace): TierName[] {
  const tiers: TierName[] = []
  if (candidate.in_recency_window) tiers.push('recency')
  if (candidate.in_semantic_initial || candidate.returned_semantic) {
    tiers.push('semantic')
  }
  if (candidate.selected_by_aspect) tiers.push('aspect')
  return tiers
}

/** The spread→packing gap: chosen by the ASPECT greedy, never delivered. */
export function selectedButDropped(trace: TurnTrace): Set<string> {
  const dropped = new Set<string>()
  for (const candidate of trace.candidates) {
    if (candidate.selected_by_aspect && !candidate.delivered) {
      dropped.add(candidate.id)
    }
  }
  return dropped
}

export interface TurnHeadline {
  delivered: number
  dropped: number
  charsDelivered: number
  /** The long-term allowance (not the total: recency is additive). */
  budget: number
  retrievalCharsDelivered: number
  utilization: number
  recency: number
  semantic: number
  aspect: number
  aspectMode: 'off' | 'protected' | 'fallback'
  trustworthy: boolean
  starved: TierName[]
  totalMs: number | null
}

export function headline(trace: TurnTrace): TurnHeadline {
  const report = trace.report
  return {
    delivered: report.episodes_delivered,
    dropped: report.episodes_dropped,
    charsDelivered: report.chars_delivered,
    budget: report.budget_chars,
    retrievalCharsDelivered:
      report.retrieval_chars_delivered === null
        ? report.chars_delivered
        : report.retrieval_chars_delivered,
    utilization: budgetUtilization(trace),
    recency: report.recency_count,
    semantic: report.semantic_count,
    aspect: report.aspect_count,
    aspectMode: trace.aspect_detail.mode,
    trustworthy: isTrustworthy(trace.verification),
    starved: starvedTiers(trace),
    totalMs: trace.total_ms,
  }
}
