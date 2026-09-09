import assert from 'node:assert/strict'
import test from 'node:test'
import { liveSource } from '../src/api/live.ts'

test('reset targets the selected chat and returns the fresh identity', async (t) => {
  const original = globalThis.fetch
  t.after(() => { globalThis.fetch = original })
  const fresh = { session_id: 'fresh', title: 'New conversation', turn_count: 0 }
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/sessions/current%20chat/reset')
    assert.equal(init.method, 'POST')
    return Response.json(fresh)
  }
  assert.deepEqual(await liveSource.resetSession('current chat'), fresh)
})

test('reset failures are surfaced instead of reporting an empty chat', async (t) => {
  const original = globalThis.fetch
  t.after(() => { globalThis.fetch = original })
  globalThis.fetch = async () => Response.json({ detail: 'Storage unavailable' }, { status: 500 })
  await assert.rejects(liveSource.resetSession('current'), /500.*Storage unavailable/)
})
