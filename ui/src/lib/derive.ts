/**
 * The Pydantic `@property` accessors, recomputed.
 *
 * `model_dump()` omits properties, so none of these arrive on the wire. Each
 * function below reproduces the body of the corresponding property in
 * `trace.py` exactly; if that file changes, these change with it.
 */
import type {
  CandidateTrace,
  PromptCacheTrace,
  SelectionPath,
  TurnTrace,
  VerificationTrace,
} from '../types/trace.ts'

/** VerificationTrace.trustworthy */
export function isTrustworthy(v: VerificationTrace): boolean {
  return v.payload_identical && v.report_fields_identical
}

/** PromptCacheTrace.cache_hit_ratio */
export function cacheHitRatio(c: PromptCacheTrace): number | null {
  if (!c.prompt_tokens || c.cached_tokens === null) return null
  return Math.min(1, c.cached_tokens / c.prompt_tokens)
}

/**
 * TurnTrace.relevance_only_ids — delivered on relevance alone.
 *
 * The one number that says whether retrieval earned its place this turn. A
 * block can look full and be nothing but the last N exchanges.
 */
export function relevanceOnlyIds(trace: TurnTrace): string[] {
  const recent = new Set(trace.timeline.recent_ids)
  return trace.timeline.selected_ids.filter((id) => !recent.has(id))
}

/**
 * How much of the delivered block continuity would have supplied anyway.
 *
 * 1 means the threshold added nothing: every relevant episode was already
 * inside the window. 0 means continuity was empty and relevance carried the
 * whole block.
 */
export function continuityShare(trace: TurnTrace): number {
  const selected = trace.timeline.selected_ids.length
  if (selected === 0) return 0
  return trace.timeline.recent_ids.length / selected
}

export function candidateIndex(trace: TurnTrace): Map<string, CandidateTrace> {
  const index = new Map<string, CandidateTrace>()
  for (const candidate of trace.candidates) index.set(candidate.id, candidate)
  return index
}

/**
 * Which condition a candidate satisfied, for display when it was never
 * delivered. `delivered_via` is null for everything that missed, so the
 * Scores table would otherwise show nothing at all for the rows that matter
 * most — the near misses.
 */
export function qualifyingPaths(candidate: CandidateTrace): SelectionPath[] {
  const paths: SelectionPath[] = []
  if (candidate.relevant) paths.push('relevance')
  if (candidate.in_recency_window) paths.push('continuity')
  if (candidate.is_anchor) paths.push('anchor')
  return paths
}

/**
 * Candidates that missed the threshold, nearest first.
 *
 * "What nearly made it" is the only remaining question about an episode the
 * mechanism turned away, and a sorted answer is cheaper to read than a full
 * table. Withheld episodes are excluded deliberately: they cleared the
 * threshold, so their margin is positive and they would sort straight to the
 * top of a list of things that missed it — saying the opposite of the truth.
 */
export function nearMisses(
  trace: TurnTrace,
  limit = 5,
): CandidateTrace[] {
  return trace.candidates
    .filter((c) => !c.delivered && c.eligible && !c.withheld)
    .sort((a, b) => b.margin - a.margin)
    .slice(0, limit)
}

export interface TurnHeadline {
  delivered: number
  eligible: number
  charsDelivered: number
  threshold: number
  window: number
  /** Delivered by continuity, whatever they scored. */
  recency: number
  /** Cleared the threshold AND were already recent. */
  overlap: number
  /** Cleared the threshold and nothing else would have delivered them. */
  relevanceOnly: number
  /** Fraction of the block continuity alone would have supplied. */
  continuityShare: number
  trustworthy: boolean
  totalMs: number | null
}

export function headline(trace: TurnTrace): TurnHeadline {
  const report = trace.report
  const timeline = trace.timeline
  return {
    delivered: report.episodes_delivered,
    eligible: report.eligible_count,
    charsDelivered: report.chars_delivered,
    threshold: timeline.relevance_threshold,
    window: timeline.recency_window_n,
    recency: report.recency_count,
    overlap: timeline.overlap_ids.length,
    relevanceOnly: timeline.relevance_only_count,
    continuityShare: continuityShare(trace),
    trustworthy: isTrustworthy(trace.verification),
    totalMs: trace.total_ms,
  }
}
