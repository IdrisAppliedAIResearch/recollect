/**
 * The Pydantic `@property` accessors, recomputed.
 *
 * `model_dump()` omits properties, so none of these arrive on the wire. Each
 * function below reproduces the body of the corresponding property in
 * `trace.py` exactly; if that file changes, these change with it.
 */
import type {
  CandidateTrace,
  ClusterTrace,
  PromptCacheTrace,
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

/** ClusterTrace.covered */
export function isCovered(cluster: ClusterTrace): boolean {
  return cluster.selected_ids.length > 0
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

/** TurnTrace.budget_utilization */
export function budgetUtilization(trace: TurnTrace): number {
  if (trace.report.budget_chars <= 0) return 0
  return trace.report.chars_delivered / trace.report.budget_chars
}

/** TurnTrace.tier(name) */
export function tierOf(trace: TurnTrace, name: TierName): TierTrace | undefined {
  return trace.tiers.find((t) => t.name === name)
}

/** Tiers in packing order, regardless of the order the server serialized them. */
export function orderedTiers(trace: TurnTrace): TierTrace[] {
  const order = trace.packing.tier_order.length
    ? trace.packing.tier_order
    : TIER_ORDER
  return order
    .map((name) => tierOf(trace, name))
    .filter((t): t is TierTrace => Boolean(t))
}

export function candidateIndex(trace: TurnTrace): Map<string, CandidateTrace> {
  const index = new Map<string, CandidateTrace>()
  for (const candidate of trace.candidates) index.set(candidate.id, candidate)
  return index
}

/**
 * Which tier proposed a candidate, for display when it was never delivered.
 * `delivered_via` is null for everything dropped, so the Scores table would
 * otherwise show no tier at all for the rows that matter most.
 */
export function proposingTiers(candidate: CandidateTrace): TierName[] {
  const tiers: TierName[] = []
  if (candidate.in_recency_window) tiers.push('recency')
  if (candidate.passes_similarity_threshold) tiers.push('similarity')
  if (candidate.selected_by_coverage) tiers.push('coverage')
  return tiers
}

/** The selector→packing gap: chosen by coverage, never delivered. */
export function selectedButDropped(trace: TurnTrace): Set<string> {
  const dropped = new Set<string>()
  for (const candidate of trace.candidates) {
    if (candidate.selected_by_coverage && !candidate.delivered) {
      dropped.add(candidate.id)
    }
  }
  return dropped
}

export interface TurnHeadline {
  delivered: number
  dropped: number
  charsDelivered: number
  budget: number
  utilization: number
  stm: number
  k: number
  coverage: number
  trustworthy: boolean
  starved: TierName[]
  inert: boolean
  totalMs: number | null
}

export function headline(trace: TurnTrace): TurnHeadline {
  return {
    delivered: trace.report.episodes_delivered,
    dropped: trace.report.episodes_dropped,
    charsDelivered: trace.report.chars_delivered,
    budget: trace.report.budget_chars,
    utilization: budgetUtilization(trace),
    stm: trace.report.stm_count,
    k: trace.report.k_count,
    coverage: trace.report.coverage_count,
    trustworthy: isTrustworthy(trace.verification),
    starved: starvedTiers(trace),
    inert: trace.similarity_detail.inert,
    totalMs: trace.total_ms,
  }
}
