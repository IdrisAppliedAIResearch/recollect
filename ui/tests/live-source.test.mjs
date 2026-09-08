import assert from 'node:assert/strict'
import test from 'node:test'

import { liveSource } from '../src/api/live.ts'

test('typed chat keeps its original request and voice chat explicitly requests voice input mode', async (t) => {
  const original = globalThis.fetch
  const requests = []
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options })
    return new Response('', { headers: { 'Content-Type': 'text/event-stream' } })
  }
  t.after(() => { globalThis.fetch = original })
  const abort = new AbortController()
  await liveSource.chat('session', 'typed question', () => {})
  await liveSource.chat('session', 'spoken question', () => {}, abort.signal, 'voice')
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    session_id: 'session', message: 'typed question',
  })
  assert.deepEqual(JSON.parse(requests[1].options.body), {
    session_id: 'session', message: 'spoken question', input_mode: 'voice',
  })
  assert.equal(requests[1].options.signal, abort.signal)
})

test('live completion events preserve explicit commit results and accept older servers', async (t) => {
  const original = globalThis.fetch
  const records = [
    { turn_id: 'stored', committed: true },
    { turn_id: 'failed', committed: false },
    { turn_id: 'legacy' },
  ]
  globalThis.fetch = async () => new Response(records.map((record) =>
    `event: done\ndata: ${JSON.stringify({
      ...record, generation: { response_text: 'Answer', error: null }, total_ms: 10,
    })}\n\n`,
  ).join(''), { headers: { 'Content-Type': 'text/event-stream' } })
  t.after(() => { globalThis.fetch = original })
  const events = []
  await liveSource.chat('session', 'question', (event) => events.push(event))
  assert.deepEqual(events.map((event) => [event.turn_id, event.committed]), [
    ['stored', true], ['failed', false], ['legacy', undefined],
  ])
})
