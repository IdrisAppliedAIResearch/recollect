import assert from 'node:assert/strict'
import test from 'node:test'

import { liveSource } from '../src/api/live.ts'
import { restoreHistory } from '../src/lib/chat-history.ts'

test('refresh restores every full message including multiline text and final characters', () => {
  const history = Array.from({ length: 35 }, (_, i) => ({
    turn_id: `turn-${i}`,
    user_message: `${'Long question café — '.repeat(30)}\n\nLAST USER LINE ${i}`,
    assistant_message: `${'Long answer — £400. '.repeat(40)}\n\n**LAST ANSWER LINE ${i}**`,
    reasoning_text: '',
    error: null,
  }))
  const restored = restoreHistory(history)
  assert.equal(restored.length, 35)
  for (let i = 0; i < history.length; i++) {
    assert.equal(restored[i].user, history[i].user_message)
    assert.equal(restored[i].assistant, history[i].assistant_message)
    assert.equal(restored[i].trace, null)
    assert.equal(restored[i].streaming, false)
  }
})

test('restored failures keep their partial text, saved reasoning and error', () => {
  const [turn] = restoreHistory([{
    turn_id: 'failed', user_message: 'Question', assistant_message: 'Partial answer',
    reasoning_text: 'Saved reasoning.\n'.repeat(30), error: 'Generation failed.',
  }])
  assert.equal(turn.assistant, 'Partial answer')
  assert.equal(turn.error, 'Generation failed.')
  assert.equal(turn.reasoning, 'Saved reasoning.\n'.repeat(30))
  assert.equal(turn.workspace, null)
  assert.deepEqual(restoreHistory([]), [])
})

test('history loads full messages in one request without fetching individual traces', async (t) => {
  const original = globalThis.fetch
  const requests = []
  const history = [{
    turn_id: 'saved', user_message: 'Question '.repeat(100),
    assistant_message: 'Answer '.repeat(200), reasoning_text: '', error: null,
  }]
  globalThis.fetch = async (url) => {
    requests.push(url)
    return Response.json(history)
  }
  t.after(() => { globalThis.fetch = original })
  const restored = restoreHistory(await liveSource.chatHistory('session id'))
  assert.deepEqual(requests, ['/api/sessions/session%20id/history'])
  assert.equal(restored[0].assistant, history[0].assistant_message)
})
