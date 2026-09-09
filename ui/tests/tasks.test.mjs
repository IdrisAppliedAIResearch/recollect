import assert from 'node:assert/strict'
import test from 'node:test'

import { liveSource } from '../src/api/live.ts'
import { artifactUrl, deleteSavedTask, resetSavedTasks, sendTaskMessage, taskSnapshot } from '../src/api/tasks.ts'
import { pendingNotifications, safeSourceUrl } from '../src/lib/task-notifications.ts'
import { conversationEvents } from '../src/lib/conversation-events.ts'
import { canDeleteSavedWork } from '../src/lib/task-retention.ts'

test('task polling and commands use conversation scoped paths and cancelable fetches', async (t) => {
  const requests = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options })
    return Response.json({ tasks: [], notifications: [], cursor: 0, enabled: true })
  }
  t.after(() => { globalThis.fetch = original })
  const abort = new AbortController()
  await taskSnapshot('chat /1', abort.signal)
  const command = { request_id: 'stable-id', operation: 'steer', text: 'Include installation.' }
  await sendTaskMessage('chat /1', 'task /2', command, abort.signal)
  await sendTaskMessage('chat /1', 'task /2', command, abort.signal)
  assert.equal(requests[0].url, '/api/sessions/chat%20%2F1/tasks')
  assert.equal(requests[0].options.cache, 'no-store')
  assert.equal(requests[0].options.signal, abort.signal)
  assert.equal(requests[1].url, '/api/sessions/chat%20%2F1/tasks/task%20%2F2/messages')
  assert.deepEqual(JSON.parse(requests[1].options.body), command)
  assert.equal(requests[1].options.body, requests[2].options.body)
  assert.equal(artifactUrl('chat /1', 'task /2', 'file /3'),
    '/api/sessions/chat%20%2F1/tasks/task%20%2F2/artifacts/file%20%2F3')
})

test('task errors preserve a specific server explanation', async (t) => {
  const original = globalThis.fetch
  globalThis.fetch = async () => Response.json({ detail: 'The queue is full.' }, { status: 409 })
  t.after(() => { globalThis.fetch = original })
  await assert.rejects(sendTaskMessage('chat', 'task', {
    request_id: 'one', operation: 'continue',
  }), /The queue is full/)
})

test('chat retries can preserve the originating request identifier', async (t) => {
  const original = globalThis.fetch
  const bodies = []
  globalThis.fetch = async (_url, options) => {
    bodies.push(JSON.parse(options.body))
    return new Response('')
  }
  t.after(() => { globalThis.fetch = original })
  for (let index = 0; index < 2; index++) {
    await liveSource.chat('chat', 'Research this', () => {}, undefined, 'text', 'same-request')
  }
  assert.deepEqual(bodies, Array(2).fill({
    session_id: 'chat', message: 'Research this', request_id: 'same-request',
  }))
})

const now = Date.parse('2026-09-09T12:00:00Z')
const tasks = [{ task_id: 'task', quiet: false }, { task_id: 'quiet', quiet: true }]
function notice(seq, changes = {}) {
  return { notification_id: `notice-${seq}`, task_id: 'task', session_id: 'chat',
    seq, kind: 'progress', text: `Update ${seq}`, created_at: new Date(now).toISOString(),
    source_refs: [], ...changes }
}

test('announcements coalesce progress and a final result supersedes pending status', () => {
  const earlier = [notice(1)]
  const pending = pendingNotifications(earlier, [notice(2), notice(3)], tasks, now)
  assert.deepEqual(pending.map((item) => item.seq), [3])
  const completed = pendingNotifications(pending, [notice(4, { kind: 'result' })], tasks, now)
  assert.deepEqual(completed.map((item) => item.seq), [4])
})

test('quiet, stale, unknown-task, invalid-date and empty announcements stay silent', () => {
  const result = pendingNotifications([], [
    notice(1, { task_id: 'quiet' }), notice(2, { task_id: 'other-chat-task' }),
    notice(3, { created_at: new Date(now - 120001).toISOString() }),
    notice(4, { created_at: 'invalid' }), notice(5, { text: ' ' }), notice(6),
  ], tasks, now)
  assert.deepEqual(result.map((item) => item.seq), [6])
  assert.deepEqual(pendingNotifications(result, [], [{ task_id: 'task', quiet: true }], now), [])
})

test('a task question survives later ordinary progress without duplication', () => {
  const question = notice(2, { kind: 'question' })
  const result = pendingNotifications([question], [question, notice(3)], tasks, now)
  assert.deepEqual(result.map((item) => item.seq), [2, 3])
})

test('source links allow web evidence without script or local file navigation', () => {
  assert.equal(safeSourceUrl('https://example.com/source'), 'https://example.com/source')
  for (const url of ['javascript:alert(1)', 'file:///secret', 'data:text/html,x', '/api/private', 'bad']) {
    assert.equal(safeSourceUrl(url), null)
  }
})

test('saved notifications merge chronologically without becoming retrieval turns', () => {
  const exchanges = [
    { id: 'old', startedAt: '2026-09-09T11:59:00Z' },
    { id: 'new', startedAt: '2026-09-09T12:01:00Z' },
  ]
  const notification = notice(1)
  const timeline = conversationEvents(exchanges, [notification, notification])
  assert.deepEqual(timeline.map((item) => [item.kind, item.id]), [
    ['turn', 'old'], ['notification', 'notification-notice-1'], ['turn', 'new'],
  ])
  assert.equal(timeline[1].exchange, undefined)
  assert.equal(exchanges.length, 2)
})

test('saved work deletion is unavailable while any task can still own or await execution', () => {
  assert.equal(canDeleteSavedWork([]), false)
  for (const state of ['queued', 'running', 'blocked', 'cancel-requested']) {
    assert.equal(canDeleteSavedWork([{ state }]), false)
    assert.equal(canDeleteSavedWork([{ state: 'completed' }, { state }]), false)
  }
  assert.equal(canDeleteSavedWork([
    { state: 'completed' }, { state: 'canceled' }, { state: 'interrupted' },
  ]), true)
})

test('confirmed deletion addresses only the selected task or conversation', async (t) => {
  const original = globalThis.fetch
  const requests = []
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options })
    return Response.json({ deleted: true })
  }
  t.after(() => { globalThis.fetch = original })
  const abort = new AbortController()
  await deleteSavedTask('chat /1', 'task /2', abort.signal)
  await resetSavedTasks('chat /1', abort.signal)
  assert.deepEqual(requests.map((item) => [item.url, item.options.method]), [
    ['/api/sessions/chat%20%2F1/tasks/task%20%2F2', 'DELETE'],
    ['/api/sessions/chat%20%2F1/tasks/reset', 'POST'],
  ])
  assert.equal(requests[0].options.signal, abort.signal)
  assert.equal(requests[1].options.signal, abort.signal)
})
