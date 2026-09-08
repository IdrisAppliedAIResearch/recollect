/**
 * A mock TurnTrace, simulated rather than hard-coded.
 *
 * The deployed CC-007 read path is replayed here in TypeScript against the
 * corpus: the additive recency window rendered outside the budget, CC80
 * fusion over the complete store (dense cosine and BM25 each min-max
 * normalized per query, fused 0.8 / 0.2), the protected 50/50 ASPECT split
 * and its greedy facet-saturation, and skip-on-overflow packing with
 * slack return. Every number in the resulting trace agrees with every other
 * number, and `context_block.payload` really is what the reported packing
 * decisions produced.
 *
 * What is stood in for:
 *   - dense cosine: the topic-affinity model scaled to the measured ceiling
 *     (~0.28) the research recorded for known-relevant content;
 *   - BM25: real Robertson BM25 (k1 1.2, b 0.75) over tokenized text;
 *   - the frozen spacy facets: per-episode subsets of the topic word banks,
 *     with the library's idf formula log((N+1)/(df+1)) + 1.
 */
import type {
  AspectDetail,
  AspectStepTrace,
  CandidateTrace,
  CC80Detail,
  GenerationTrace,
  PackingDecision,
  PackPhase,
  ReportTrace,
  TierName,
  TierTrace,
  TurnTrace,
} from '../types/trace.ts'
import { TIER_DESCRIPTIONS, TIER_LABELS } from '../types/trace.ts'
import {
  EMPTY_PAYLOAD_CHARS,
  additiveWeight,
  renderStmPayload,
} from '../lib/render.ts'
import { TOPICS, buildCorpus, mulberry32, type MockEpisode } from './corpus.ts'

// -- mechanism constants, from the store-pinned EpisodicConfig ---------------
const RECENCY_WINDOW_N = 32
const RETRIEVAL_BUDGET_CHARS = 32_000
const DENSE_WEIGHT = 0.8
const BM25_K1 = 1.2
const BM25_B = 0.75
const ASPECT_SHARE = 0.5
const ASPECT_MODEL = 'en_core_web_sm'
const DROP_POLICY = 'marginal_gain_order_skip_on_overflow'
const LIBRARY_VERSION = '0.2.0'
const CARRIED_EMBEDDER_SHA256 =
  '06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439'
const ASPECT_ENABLED = true // Recollect deploys ASPECT on by default

const STORE_SIZE = 120

export type MockScenario = 'deployed' | 'diverged'

// ---------------------------------------------------------------------------
// Pseudo-embeddings and pseudo-lexical scores
// ---------------------------------------------------------------------------

/** Fixed cross-topic affinity. Same every run, so cosines are reproducible. */
const TOPIC_AFFINITY: number[][] = (() => {
  const random = mulberry32(0x5eed)
  const size = TOPICS.length
  const matrix: number[][] = Array.from({ length: size }, () =>
    new Array<number>(size).fill(0),
  )
  for (let i = 0; i < size; i += 1) {
    matrix[i]![i] = 1
    for (let j = i + 1; j < size; j += 1) {
      const value = 0.05 + random() * 0.55
      matrix[i]![j] = value
      matrix[j]![i] = value
    }
  }
  return matrix
})()

/**
 * Dense cosines for one query. Scaled so the best in the store lands near
 * 0.2779 - the highest score the research ever recorded for known-relevant
 * content.
 */
function denseFor(
  episodes: MockEpisode[],
  queryTopic: number,
  seed: number,
): Map<string, number> {
  const random = mulberry32(seed)
  const raw = episodes.map((episode) => {
    const affinity = TOPIC_AFFINITY[queryTopic]![episode.topic]!
    // Recency has a mild lexical pull in real embeddings; keep a little of it.
    const drift = 1 - Math.min(0.25, (episodes.length - episode.turn_number) / 900)
    return Math.max(0.005, affinity * drift + (random() - 0.5) * 0.22)
  })
  const peak = Math.max(...raw)
  const ceiling = 0.2779 - (seed % 7) * 0.004
  const scale = ceiling / peak
  const result = new Map<string, number>()
  episodes.forEach((episode, index) => {
    result.set(episode.id, Number((raw[index]! * scale).toFixed(6)))
  })
  return result
}

const TOKEN = /[a-z0-9]+(?:[-'][a-z0-9]+)*/g

function tokenize(text: string): string[] {
  return text.toLowerCase().match(TOKEN) ?? []
}

/**
 * Robertson BM25 straight from the library's frozen constants, over the
 * episode's user + assistant text. A query with no lexical overlap at all
 * yields an all-zero (constant) component, which min-max scaling turns into
 * zeros - the same degenerate branch the real ranker takes.
 */
function bm25For(
  episodes: MockEpisode[],
  queryText: string,
): Map<string, number> {
  const queryTokens = tokenize(queryText)
  const result = new Map<string, number>()
  if (queryTokens.length === 0) {
    for (const episode of episodes) result.set(episode.id, 0)
    return result
  }
  const docs = episodes.map((episode) =>
    tokenize(`${episode.user_message} ${episode.assistant_message}`),
  )
  const uniqueQuery = [...new Set(queryTokens)]
  const df = new Map<string, number>()
  for (const token of uniqueQuery) {
    let count = 0
    for (const doc of docs) {
      if (doc.includes(token)) count += 1
    }
    df.set(token, count)
  }
  const totalLength = docs.reduce((sum, doc) => sum + doc.length, 0)
  const avgLength = totalLength / Math.max(1, docs.length)
  const n = docs.length

  episodes.forEach((episode, index) => {
    const doc = docs[index]!
    const frequency = new Map<string, number>()
    for (const token of doc) frequency.set(token, (frequency.get(token) ?? 0) + 1)
    let score = 0
    for (const token of uniqueQuery) {
      const tf = frequency.get(token) ?? 0
      if (tf === 0) continue
      const docFreq = df.get(token) ?? 0
      const idf = Math.log(1 + (n - docFreq + 0.5) / (docFreq + 0.5))
      const lengthNorm =
        1 - BM25_B + BM25_B * (doc.length / Math.max(1e-9, avgLength))
      score += idf * ((tf * (BM25_K1 + 1)) / (tf + BM25_K1 * lengthNorm))
    }
    result.set(episode.id, Number(score.toFixed(6)))
  })
  return result
}

/**
 * Per-episode facet sets, standing in for the frozen spacy extraction. The
 * topic word banks carry the structure idf needs: the shared subject is
 * common (low idf), the per-episode nouns are rare (high idf).
 */
function facetsFor(episode: MockEpisode): Set<string> {
  const topic = TOPICS[episode.topic]!
  const salt = episode.turn_number
  const facets = new Set<string>()
  facets.add(`entity:misc:${topic.subject}`)
  facets.add(`noun:${topic.nouns[salt % topic.nouns.length]}`)
  const action = topic.actions[(salt * 3) % topic.actions.length]!
  facets.add(`event:${tokenize(action)[0] ?? 'act'}`)
  if (mulberry32(0xf4c5 * (salt + 1))() < 0.5) {
    facets.add(`number:${[32, 16, 8, 5][salt % 4]}`)
  }
  return facets
}

function facetIdf(all: Set<string>[]): Map<string, number> {
  const count = all.length
  const df = new Map<string, number>()
  for (const set of all) {
    for (const facet of set) df.set(facet, (df.get(facet) ?? 0) + 1)
  }
  const idf = new Map<string, number>()
  for (const [facet, frequency] of df) {
    idf.set(facet, Math.log((count + 1) / (frequency + 1)) + 1.0)
  }
  return idf
}

// ---------------------------------------------------------------------------
// Normalization and ranking
// ---------------------------------------------------------------------------

interface Normalized {
  values: number[]
  min: number
  max: number
  constant: boolean
}

function minMax(values: number[]): Normalized {
  const min = Math.min(...values)
  const max = Math.max(...values)
  if (max === min) return { values: values.map(() => 0), min, max, constant: true }
  return {
    values: values.map((value) => (value - min) / (max - min)),
    min,
    max,
    constant: false,
  }
}

interface RankedEpisode {
  episode: MockEpisode
  dense: number
  denseNormalized: number
  bm25: number
  bm25Normalized: number
  score: number
  /** 1 = highest, ties broken by turn number then id, as the library orders. */
  rank: number
}

function rankAll(
  episodes: MockEpisode[],
  dense: Map<string, number>,
  bm25: Map<string, number>,
): { ranked: RankedEpisode[]; detail: CC80Detail } {
  const denseNorm = minMax(episodes.map((e) => dense.get(e.id) ?? 0))
  const bm25Norm = minMax(episodes.map((e) => bm25.get(e.id) ?? 0))
  const rows: RankedEpisode[] = episodes.map((episode, index) => {
    const d = dense.get(episode.id) ?? 0
    const b = bm25.get(episode.id) ?? 0
    const dn = denseNorm.values[index]!
    const bn = bm25Norm.values[index]!
    return {
      episode,
      dense: d,
      denseNormalized: dn,
      bm25: b,
      bm25Normalized: bn,
      score: DENSE_WEIGHT * dn + (1 - DENSE_WEIGHT) * bn,
      rank: 0,
    }
  })
  const order = rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      if (b.row.score !== a.row.score) return b.row.score - a.row.score
      if (a.row.episode.turn_number !== b.row.episode.turn_number) {
        return a.row.episode.turn_number - b.row.episode.turn_number
      }
      return a.row.episode.id < b.row.episode.id ? -1 : a.row.episode.id > b.row.episode.id ? 1 : 0
    })
    .map(({ index }) => index)
  order.forEach((index, position) => {
    rows[index]!.rank = position + 1
  })
  return {
    ranked: rows,
    detail: {
      dense_weight: DENSE_WEIGHT,
      bm25_k1: BM25_K1,
      bm25_b: BM25_B,
      dense_min: denseNorm.min,
      dense_max: denseNorm.max,
      dense_constant: denseNorm.constant,
      bm25_min: bm25Norm.min,
      bm25_max: bm25Norm.max,
      bm25_constant: bm25Norm.constant,
    },
  }
}

// ---------------------------------------------------------------------------
// The mechanism, replayed
// ---------------------------------------------------------------------------

/**
 * `pack_stm_payload` over the long-term block only: recent continuity is
 * additive and never charged here, so the running serialization is the
 * two-block payload with an empty recent block. Skip-on-overflow.
 */
function packWalk(
  candidates: RankedEpisode[],
  allowance: number,
  phase: PackPhase,
  tier: TierName,
  decisions: PackingDecision[],
): string[] {
  const picked: RankedEpisode[] = []
  for (const candidate of candidates) {
    const test = [...picked, candidate]
    const payloadChars = renderStmPayload([], test.map((row) => row.episode)).length
    if (payloadChars <= allowance) {
      picked.push(candidate)
      decisions.push({
        order: decisions.length + 1,
        candidate_id: candidate.episode.id,
        tier,
        phase,
        cost_chars: additiveWeight(candidate.episode),
        payload_chars_after: payloadChars,
        admitted: true,
        reason: 'fits',
      })
    } else {
      decisions.push({
        order: decisions.length + 1,
        candidate_id: candidate.episode.id,
        tier,
        phase,
        cost_chars: additiveWeight(candidate.episode),
        payload_chars_after: renderStmPayload(
          [],
          picked.map((row) => row.episode),
        ).length,
        admitted: false,
        reason:
          `would reach ${payloadChars} characters, past the ${allowance} ` +
          'allowance; skipped and the walk continued',
      })
    }
  }
  return [
    ...picked.map((row) => row.episode.id),
  ]
}

interface SpreadResult {
  chosen: RankedEpisode[]
  steps: AspectStepTrace[]
  soloChars: number
  stoppingReason: 'no_complete_candidate_fits' | 'no_positive_marginal'
}

/**
 * `aspect_spread`: greedy CC80-weighted facet saturation over the half.
 * Recency is excluded by identity: the library never long-term-admits an
 * episode the recency block already carries.
 */
function runSpread(
  ranked: RankedEpisode[],
  initial: RankedEpisode[],
  half: number,
  idf: Map<string, number>,
  facetsById: Map<string, Set<string>>,
  recentIds: Set<string>,
): SpreadResult {
  const storeIndex = new Map(ranked.map((row, index) => [row.episode.id, index]))
  const scoreOf = new Map(ranked.map((row) => [row.episode.id, row.score]))
  const rankOf = new Map(ranked.map((row) => [row.episode.id, row.rank]))

  const covered = new Map<string, number>()
  for (const seed of initial) {
    for (const facet of facetsById.get(seed.episode.id) ?? []) {
      const value = (scoreOf.get(seed.episode.id) ?? 0) * (idf.get(facet) ?? 0)
      covered.set(facet, Math.max(covered.get(facet) ?? 0, value))
    }
  }

  const excluded = new Set<string>(initial.map((row) => row.episode.id))
  for (const id of recentIds) excluded.add(id)
  const chosen: RankedEpisode[] = []
  const steps: AspectStepTrace[] = []
  let spent = EMPTY_PAYLOAD_CHARS
  let stoppingReason: SpreadResult['stoppingReason'] = 'no_positive_marginal'

  for (;;) {
    const fit = ranked.filter(
      (row) =>
        !excluded.has(row.episode.id) &&
        spent + additiveWeight(row.episode) <= half,
    )
    if (fit.length === 0) {
      stoppingReason = 'no_complete_candidate_fits'
      break
    }
    let best: RankedEpisode | null = null
    let bestKey: [number, number, number] | null = null
    let bestMarginal = 0
    for (const row of fit) {
      const score = row.score
      let marginal = 0
      for (const facet of facetsById.get(row.episode.id) ?? []) {
        marginal += Math.max(
          0,
          score * (idf.get(facet) ?? 0) - (covered.get(facet) ?? 0),
        )
      }
      const ratio = marginal / additiveWeight(row.episode)
      const key: [number, number, number] = [
        ratio,
        -(rankOf.get(row.episode.id) ?? 0),
        -(storeIndex.get(row.episode.id) ?? 0),
      ]
      if (bestKey === null || compareKey(key, bestKey) > 0) {
        best = row
        bestKey = key
        bestMarginal = marginal
      }
    }
    if (best === null || bestMarginal <= 1e-12) {
      stoppingReason = 'no_positive_marginal'
      break
    }

    excluded.add(best.episode.id)
    chosen.push(best)
    spent += additiveWeight(best.episode)
    const score = best.score
    for (const facet of facetsById.get(best.episode.id) ?? []) {
      covered.set(facet, Math.max(covered.get(facet) ?? 0, score * (idf.get(facet) ?? 0)))
    }
    steps.push({
      step: steps.length + 1,
      candidate_id: best.episode.id,
      source_turn: best.episode.turn_number,
      score: best.score,
      marginal: bestMarginal,
      ratio: bestMarginal / additiveWeight(best.episode),
      additive_chars: additiveWeight(best.episode),
      cumulative_chars: spent,
      covered_total: covered.size,
    })
  }
  return { chosen, steps, soloChars: spent, stoppingReason }
}

function compareKey(a: [number, number, number], b: [number, number, number]): number {
  if (a[0] !== b[0]) return a[0] - b[0]
  if (a[1] !== b[1]) return a[1] - b[1]
  return a[2] - b[2]
}

// ---------------------------------------------------------------------------
// Turn assembly
// ---------------------------------------------------------------------------

const PREVIEW_CHARS = 240

function preview(value: string): string {
  const text = value.trim().replace(/\n/g, ' ')
  if (text.length <= PREVIEW_CHARS) return text
  return `${text.slice(0, PREVIEW_CHARS - 1)}…`
}

/** Not a real SHA-256 — a stable stand-in of the right shape and length. */
function fakeSha(input: string): string {
  let h1 = 0x811c9dc5
  let h2 = 0x01000193
  for (let i = 0; i < input.length; i += 1) {
    h1 = Math.imul(h1 ^ input.charCodeAt(i), 0x01000193) >>> 0
    h2 = Math.imul(h2 + input.charCodeAt(i) * (i + 1), 0x85ebca6b) >>> 0
  }
  let out = ''
  let state = (h1 ^ h2) >>> 0
  const random = mulberry32(state)
  for (let i = 0; i < 64; i += 1) {
    out += Math.floor(random() * 16).toString(16)
  }
  return out
}

function storeConfigJson(): string {
  return JSON.stringify({
    recency_window_n: RECENCY_WINDOW_N,
    retrieval_budget_chars: RETRIEVAL_BUDGET_CHARS,
    semantic_dense_weight: DENSE_WEIGHT,
    bm25_k1: BM25_K1,
    bm25_b: BM25_B,
    aspect_enabled: ASPECT_ENABLED,
    aspect_share: ASPECT_SHARE,
    aspect_model: ASPECT_MODEL,
    k_threshold: 0.48,
    candidate_policy: 'full_store',
    unsafe_cosine_top_n: 100,
    selector: 'A3',
    selector_lambda: 0.1,
    selector_cost_exponent: 0.0,
    selector_cluster_count: 16,
    budget_accounting: 'exact_serialized',
    embedder_sha256: CARRIED_EMBEDDER_SHA256,
    embed_call_shape: 'solo',
    seed: 5005,
  })
}

export interface MockTurnInput {
  sessionId: string
  turnIndex: number
  queryText: string
  queryTopic: number
  storeSize?: number
  scenario?: MockScenario
  withGeneration?: boolean
  responseText?: string
}

export function generateTurn(input: MockTurnInput): TurnTrace {
  const {
    sessionId,
    turnIndex,
    queryText,
    queryTopic,
    storeSize = STORE_SIZE,
    scenario = 'deployed',
    withGeneration = true,
  } = input

  const episodes = buildCorpus(storeSize)
  const byId = new Map(episodes.map((e) => [e.id, e]))
  const seed = 5005 + turnIndex * 17
  const dense = denseFor(episodes, queryTopic, seed)
  const bm25 = bm25For(episodes, queryText)
  const { ranked, detail: cc80Detail } = rankAll(episodes, dense, bm25)
  const rankByEpisode = new Map(ranked.map((row) => [row.episode.id, row]))

  // -- recency: additive, outside the allowance --------------------------
  const recent = episodes.slice(Math.max(0, episodes.length - RECENCY_WINDOW_N))
  const recentIds = new Set(recent.map((e) => e.id))
  const eligible = ranked.filter((row) => !recentIds.has(row.episode.id))

  // -- the long-term walk --------------------------------------------------
  const budget = RETRIEVAL_BUDGET_CHARS
  const half = Math.floor(budget * ASPECT_SHARE)
  const decisions: PackingDecision[] = []
  const phases: PackPhase[] = []
  const markPhase = (phase: PackPhase) => {
    if (!phases.includes(phase)) phases.push(phase)
  }
  let mode: 'protected' | 'fallback' = 'protected'
  let initial: RankedEpisode[] = []
  let initialIds: string[] = []
  let spreadChosen: RankedEpisode[] = []
  let spreadIds: string[] = []
  let spreadInputIds: string[] = []
  let returned: RankedEpisode[] = []
  let spread: SpreadResult | null = null
  let facetLatencyMs: number | null = null

  if (!ASPECT_ENABLED || eligible.length === 0) {
    if (ASPECT_ENABLED) mode = 'fallback'
    markPhase('full')
    initialIds = packWalk(eligible, budget, 'full', 'semantic', decisions)
  } else {
    markPhase('initial')
    initialIds = packWalk(eligible, half, 'initial', 'semantic', decisions)
    initial = initialIds.map((id) => rankByEpisode.get(id)!)
    if (initial.length === 0) {
      // The initial half admitted nothing: one CC80 walk owns the allowance.
      mode = 'fallback'
      decisions.length = 0
      phases.length = 0
      markPhase('full')
      initialIds = packWalk(eligible, budget, 'full', 'semantic', decisions)
      initial = initialIds.map((id) => rankByEpisode.get(id)!)
    } else {
      facetLatencyMs = 41.6 + (turnIndex % 5) * 2.2
      const facetsById = new Map(episodes.map((e) => [e.id, facetsFor(e)]))
      const idf = facetIdf([...facetsById.values()])
      spread = runSpread(ranked, initial, half, idf, facetsById, recentIds)
      spreadChosen = spread.chosen
      spreadInputIds = spread.chosen.map((row) => row.episode.id)
      markPhase('spread')
      spreadIds = packWalk(spreadChosen, half, 'spread', 'aspect', decisions)
    }
  }

  const initialSet = new Set(initialIds)
  const spreadSet = new Set(spreadIds)
  const finalLongTerm = [
    ...initialIds.map((id) => byId.get(id)!),
    ...spreadIds.map((id) => byId.get(id)!),
  ]
  let currentChars = renderStmPayload([], finalLongTerm).length
  const admittedSet = new Set([...initialIds, ...spreadIds])
  if (ASPECT_ENABLED && eligible.length > 0 && mode === 'protected') {
    markPhase('slack')
    for (const row of eligible) {
      if (admittedSet.has(row.episode.id)) continue
      const cost = additiveWeight(row.episode)
      if (currentChars + cost <= budget) {
        finalLongTerm.push(row.episode)
        admittedSet.add(row.episode.id)
        currentChars += cost
        returned.push(row)
        decisions.push({
          order: decisions.length + 1,
          candidate_id: row.episode.id,
          tier: 'semantic',
          phase: 'slack',
          cost_chars: cost,
          payload_chars_after: currentChars,
          admitted: true,
          reason: 'fits the remaining budget; returned',
        })
      }
    }
  }
  const finalIds = finalLongTerm.map((e) => e.id)
  const finalSet = new Set(finalIds)

  // -- the payload the model saw ------------------------------------------
  const payload =
    budget >= EMPTY_PAYLOAD_CHARS ? renderStmPayload(recent, finalLongTerm) : ''
  const deliveredIds = new Set([...recentIds, ...finalIds])

  // -- what the selection would have wanted --------------------------------
  const charsWanted =
    eligible.length === 0
      ? EMPTY_PAYLOAD_CHARS
      : renderStmPayload([], eligible.map((row) => row.episode)).length
  const droppedIds = eligible
    .filter((row) => !finalSet.has(row.episode.id))
    .map((row) => row.episode.id)

  // -- candidate rows -------------------------------------------------------
  const lastDecision = new Map<string, PackingDecision>()
  for (const decision of decisions) {
    if (!decision.admitted) lastDecision.set(decision.candidate_id, decision)
  }
  const returnedSet = new Set(returned.map((row) => row.episode.id))

  const candidates: CandidateTrace[] = episodes.map((episode) => {
    const row = rankByEpisode.get(episode.id)!
    const delivered = deliveredIds.has(episode.id)
    const via: TierName | null = !delivered
      ? null
      : recentIds.has(episode.id)
        ? 'recency'
        : initialSet.has(episode.id)
          ? 'semantic'
          : spreadSet.has(episode.id)
            ? 'aspect'
            : returnedSet.has(episode.id)
              ? 'semantic'
              : null
    const decision = lastDecision.get(episode.id)
    return {
      id: episode.id,
      turn_number: episode.turn_number,
      preview: preview(episode.user_message),
      assistant_preview: preview(episode.assistant_message),
      dense_cosine: row.dense,
      dense_normalized: row.denseNormalized,
      bm25_score: row.bm25,
      bm25_normalized: row.bm25Normalized,
      cc80_score: row.score,
      cc80_rank: row.rank,
      render_chars: additiveWeight(episode),
      in_recency_window: recentIds.has(episode.id),
      in_semantic_initial: initialSet.has(episode.id),
      selected_by_aspect: spreadSet.has(episode.id),
      returned_semantic: returnedSet.has(episode.id),
      delivered,
      delivered_via: via,
      drop_reason: delivered ? null : (decision?.reason ?? null),
    }
  })

  // -- tier rows ----------------------------------------------------------
  const weights = new Map(episodes.map((e) => [e.id, additiveWeight(e)]))
  const contextIds = deliveredIds

  function buildTier(
    name: TierName,
    proposed: string[],
    claim: Set<string>,
  ): TierTrace {
    const delivered = proposed.filter((id) => contextIds.has(id) && claim.has(id))
    const overlapped = proposed.filter((id) => contextIds.has(id) && !claim.has(id))
    const skipped = proposed.filter((id) => !contextIds.has(id))
    return {
      name,
      label: TIER_LABELS[name],
      description: TIER_DESCRIPTIONS[name],
      proposed_ids: proposed,
      delivered_ids: delivered,
      overlapped_ids: overlapped,
      skipped_ids: skipped,
      chars_delivered: delivered.reduce((sum, id) => sum + (weights.get(id) ?? 0), 0),
      chars_proposed: proposed.reduce((sum, id) => sum + (weights.get(id) ?? 0), 0),
    }
  }

  const semanticClaim = new Set([...initialIds, ...returned.map((r) => r.episode.id)])
  const tiers: TierTrace[] = [
    buildTier('recency', recent.map((e) => e.id), recentIds),
    buildTier('semantic', eligible.map((row) => row.episode.id), semanticClaim),
    buildTier('aspect', spreadInputIds, spreadSet),
  ]

  // -- verification ---------------------------------------------------------
  const authoritySha = fakeSha(payload)
  const diverged = scenario === 'diverged'
  const shadowSha = diverged ? fakeSha(`${payload} drift`) : authoritySha

  const startedAt = new Date(Date.parse('2026-08-17T14:03:00Z') + turnIndex * 96_000)

  const generation: GenerationTrace | null = withGeneration
    ? {
        model: 'qwen3-8b-instruct',
        base_url: 'http://127.0.0.1:8000/v1',
        system_prompt_chars: 612,
        context_block_chars: payload.length,
        total_prompt_chars: 612 + payload.length + queryText.length,
        response_text: input.responseText ?? mockResponse(queryTopic, turnIndex),
        response_chars: (input.responseText ?? mockResponse(queryTopic, turnIndex)).length,
        reasoning_text:
          turnIndex % 2 === 0
            ? 'The recent block covers the same subject, so I can answer from it directly. Nothing older was delivered, so I should not claim to recall anything beyond what is here.'
            : '',
        thinking_enabled: turnIndex % 2 === 0,
        ttft_ms: 1180 + turnIndex * 37,
        total_ms: 6120 + turnIndex * 210,
        tokens_out: 214 + turnIndex * 9,
        tokens_per_sec: 41.7,
        prompt_cache: {
          prompt_tokens: 9040 + Math.floor(payload.length / 4),
          // The block is rebuilt every turn by design, so most of the prefix
          // is forfeit. That is the point of recording it.
          cached_tokens: 612,
          processed_tokens: 9040 + Math.floor(payload.length / 4) - 612,
          prefill_ms: 5870,
        },
        finish_reason: 'stop',
        error: null,
        tool_calls: [],
      }
    : null

  const report: ReportTrace = {
    chars_delivered: payload.length,
    chars_wanted: charsWanted,
    episodes_delivered: deliveredIds.size,
    episodes_dropped: droppedIds.length,
    truncated: droppedIds.length > 0,
    stm_count: recent.length,
    k_count: semanticClaim.size,
    coverage_count: spreadIds.length,
    latency_ms: 71.4 + (turnIndex % 4) * 2.6,
    pool_size: episodes.length,
    dropped_ids: droppedIds,
    drop_policy: DROP_POLICY,
    budget_chars: budget,
    retrieval_chars_delivered: currentChars,
    retrieval_budget_chars: budget,
    recency_count: recent.length,
    semantic_count: semanticClaim.size,
    aspect_count: spreadIds.length,
    returned_semantic_count: returned.length,
    aspect_enabled: ASPECT_ENABLED,
    recent_ids: recent.map((e) => e.id),
    recency_additive: true,
  }

  const aspectDetail: AspectDetail = {
    enabled: ASPECT_ENABLED,
    share: ASPECT_SHARE,
    model: ASPECT_MODEL,
    mode,
    facet_latency_ms: facetLatencyMs,
    initial_ids: initialIds,
    spread_ids: spreadIds,
    returned_ids: returned.map((row) => row.episode.id),
    solo_chars: spread?.soloChars ?? null,
    stopping_reason: spread?.stoppingReason ?? null,
    steps: spread?.steps ?? [],
  }

  return {
    schema_version: 2,
    turn_id: `turn-${sessionId}-${String(turnIndex).padStart(3, '0')}`,
    session_id: sessionId,
    turn_index: turnIndex,
    started_at: startedAt.toISOString(),
    total_ms: withGeneration ? 6420 + turnIndex * 210 : null,

    query: {
      text: queryText,
      chars: queryText.length,
      embedding_sha256: fakeSha(`query:${queryText}`),
      embedding_norm: 1.0,
      embed_latency_ms: 54.8 + (turnIndex % 5) * 1.3,
      embed_cache_hit: false,
    },
    store: {
      path: `var/sessions/${sessionId}/episodes.sqlite`,
      episode_count: episodes.length,
      config_json: storeConfigJson(),
      sentinel_sha256: fakeSha('sentinel'),
      embedder_model_sha256: fakeSha('Qwen3-Embedding-0.6B-Q8_0.gguf'),
    },

    candidates,
    tiers,
    cc80_detail: cc80Detail,
    aspect_detail: aspectDetail,
    packing: {
      policy: DROP_POLICY,
      phases,
      budget_chars: budget,
      half_chars: mode === 'protected' ? half : mode === 'fallback' ? half : 0,
      empty_payload_chars: EMPTY_PAYLOAD_CHARS,
      decisions,
      duplicate_ids: [],
    },

    context_block: {
      payload,
      chars: payload.length,
      sha256: authoritySha,
      recent_episode_count: recent.length,
      retrieved_episode_count: finalIds.length,
    },
    report,
    verification: {
      payload_identical: !diverged,
      report_fields_identical: !diverged,
      authority_payload_sha256: authoritySha,
      shadow_payload_sha256: shadowSha,
      mismatched_fields: diverged
        ? [
            'chars_delivered: shadow=31284 authority=31996',
            'semantic_count: shadow=1 authority=0',
          ]
        : [],
      library_version: LIBRARY_VERSION,
      shadow_latency_ms: 8.4 + (turnIndex % 3) * 0.7,
    },
    generation,
    subagent: null,
  }
}

function mockResponse(topicIndex: number, turnIndex: number): string {
  const topic = TOPICS[topicIndex % TOPICS.length]!
  return [
    `Going on what we worked through before: the thing to check first is ${topic.nouns[turnIndex % topic.nouns.length]}.`,
    `You mentioned ${topic.issues[turnIndex % topic.issues.length]} last time, and that is the same shape of problem.`,
    '',
    `Try this: ${topic.actions[turnIndex % topic.actions.length]}. If it works you should see ${topic.outcomes[turnIndex % topic.outcomes.length]} fairly quickly.`,
    '',
    'If that does not move it, tell me what changed and we will narrow it down from there.',
  ].join('\n')
}

// ---------------------------------------------------------------------------
// A whole session
// ---------------------------------------------------------------------------

export interface MockTurnSeed {
  query: string
  topic: number
  storeSize: number
}

export const MOCK_TURN_SEEDS: MockTurnSeed[] = [
  { query: 'remind me what we decided about the compost heap', topic: 6, storeSize: 116 },
  { query: 'is the achilles thing something I should see someone about', topic: 1, storeSize: 117 },
  { query: 'what was the storage advice for the puerh again', topic: 13, storeSize: 118 },
  { query: 'did we settle on mirrors or raidz for the eight drives', topic: 11, storeSize: 119 },
  {
    query: 'pulling this together — what are the open threads across everything we have talked about?',
    topic: 7,
    storeSize: STORE_SIZE,
  },
]

export function generateMockSession(
  sessionId: string,
  scenario: MockScenario = 'deployed',
): TurnTrace[] {
  return MOCK_TURN_SEEDS.map((seed, index) =>
    generateTurn({
      sessionId,
      turnIndex: index + 1,
      queryText: seed.query,
      queryTopic: seed.topic,
      storeSize: seed.storeSize,
      // Only the last turn carries the divergence, so the healthy badge is
      // still reachable in the diverged scenario.
      scenario: scenario === 'diverged' && index === MOCK_TURN_SEEDS.length - 1
        ? 'diverged'
        : 'deployed',
    }),
  )
}
