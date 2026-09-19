import assert from 'node:assert/strict'
import test from 'node:test'

import { MOCK_TURN_SEEDS, generateTurn } from '../src/mock/generate.ts'

const THRESHOLD = 0.48

function turns() {
  return MOCK_TURN_SEEDS.map((seed, index) =>
    generateTurn({
      sessionId: 'mock-0001',
      turnIndex: index,
      queryText: seed.query,
      queryTopic: seed.topic,
      storeSize: seed.storeSize,
    }),
  )
}

test('the mock selection is a real union of the two conditions', () => {
  for (const trace of turns()) {
    const { relevant_ids, recent_ids, selected_ids, overlap_ids } = trace.timeline
    const union = new Set([...relevant_ids, ...recent_ids])
    assert.deepEqual(
      new Set(selected_ids),
      union,
      'delivered set must be exactly relevance ∪ continuity',
    )
    assert.equal(
      selected_ids.length,
      new Set(selected_ids).size,
      'an episode qualifying twice must still be delivered once',
    )
    assert.deepEqual(
      new Set(overlap_ids),
      new Set(relevant_ids.filter((id) => recent_ids.includes(id))),
    )
  }
})

test('the mock renders its selection chronologically', () => {
  for (const trace of turns()) {
    const turnOf = new Map(trace.candidates.map((c) => [c.id, c.turn_number]))
    const order = trace.timeline.selected_ids.map((id) => turnOf.get(id))
    assert.deepEqual(order, [...order].sort((a, b) => a - b))
  }
})

/**
 * The calibration guard.
 *
 * The mock's cosine model was once scaled to a 0.2779 ceiling carried from
 * the CC80 research. Under a 0.48 threshold nothing would ever have cleared
 * it, so every demo turn would have shown pure continuity — a mechanism
 * that silently never fires. Nothing else in the suite would have noticed.
 */
test('the mock cosines straddle the threshold', () => {
  const all = turns().flatMap((trace) => trace.candidates.map((c) => c.cosine))
  assert.ok(
    all.some((value) => value >= THRESHOLD),
    'no mock episode ever clears the threshold; the demo shows nothing retrieved',
  )
  assert.ok(
    all.some((value) => value < THRESHOLD),
    'every mock episode clears the threshold; the demo shows nothing refused',
  )
  assert.ok(Math.max(...all) <= 1, 'a cosine above 1 is not a cosine')
  assert.ok(Math.min(...all) >= -1, 'a cosine below -1 is not a cosine')
})

test('the mock shows long-term memory contributing on some turn', () => {
  const contributed = turns().filter(
    (trace) => trace.timeline.relevance_only_count > 0,
  )
  assert.ok(
    contributed.length > 0,
    'no turn retrieves anything the continuity window would not already carry',
  )
})

test('the mock exercises the deployment ceiling', () => {
  const engaged = turns().filter((trace) => trace.ceiling.engaged)
  assert.ok(
    engaged.length > 0,
    'the ceiling never engages, so the demo never shows it working',
  )
})

test('the ceiling never withholds continuity, and never lies about why', () => {
  for (const trace of turns()) {
    const recent = new Set(trace.timeline.recent_ids)
    const delivered = new Set(trace.timeline.selected_ids)
    const byId = new Map(trace.candidates.map((c) => [c.id, c]))

    for (const id of trace.ceiling.withheld_ids) {
      assert.ok(!recent.has(id), 'continuity must never be withheld')
      assert.ok(!delivered.has(id), 'a withheld episode cannot be delivered')
      const candidate = byId.get(id)
      assert.equal(candidate.withheld, true)
      assert.equal(candidate.relevant, true, 'withheld means it DID qualify')
      assert.ok(candidate.margin >= 0)
      assert.equal(candidate.delivered_via, null)
    }

    assert.equal(
      trace.ceiling.store_episodes - trace.ceiling.considered_episodes,
      trace.ceiling.withheld_ids.length,
    )
    assert.equal(trace.ceiling.store_episodes, trace.candidates.length)
  }
})

test('the ceiling sheds the weakest first', () => {
  for (const trace of turns()) {
    if (!trace.ceiling.engaged) continue
    const byId = new Map(trace.candidates.map((c) => [c.id, c]))
    const withheld = trace.ceiling.withheld_ids.map((id) => byId.get(id))
    const keptRelevance = trace.candidates.filter(
      (c) => c.relevant && !c.withheld && !c.in_recency_window,
    )
    if (keptRelevance.length === 0) continue
    assert.ok(
      Math.max(...withheld.map((c) => c.cosine)) <=
        Math.min(...keptRelevance.map((c) => c.cosine)),
      'a stronger episode was shed while a weaker one was kept',
    )
  }
})

test('the mock report agrees with its own timeline detail', () => {
  for (const trace of turns()) {
    const { report, timeline } = trace
    assert.equal(report.episodes_delivered, timeline.selected_ids.length)
    assert.equal(report.recency_count, timeline.recent_ids.length)
    assert.equal(report.stm_count, timeline.recent_ids.length)
    assert.equal(report.k_count, timeline.relevance_only_count)
    assert.equal(report.relevance_threshold, timeline.relevance_threshold)
    assert.equal(report.chars_delivered, trace.context_block.chars)
    assert.equal(trace.schema_version, 3)
    assert.equal(report.read_policy, 'timeline')
  }
})

test('every candidate row agrees with the threshold it reports', () => {
  for (const trace of turns()) {
    for (const candidate of trace.candidates) {
      assert.equal(candidate.relevant, candidate.cosine >= THRESHOLD)
      assert.ok(
        Math.abs(candidate.margin - (candidate.cosine - THRESHOLD)) < 1e-6,
        'margin must be the distance from the threshold',
      )
      if (!candidate.delivered) assert.equal(candidate.delivered_via, null)
      if (candidate.relevant && candidate.in_recency_window) {
        assert.equal(candidate.delivered_via, 'both')
      }
    }
  }
})
