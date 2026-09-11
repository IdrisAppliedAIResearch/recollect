import assert from 'node:assert/strict'
import test from 'node:test'

import { pendingNotifications } from '../src/lib/task-notifications.ts'

test('seven-second heartbeats do not replace unheard findings', () => {
  const now = Date.now()
  const notice = (seq, kind) => ({ notification_id: `n-${seq}`, task_id: 'task',
    seq, kind, text: `Report ${seq}`, created_at: new Date(now).toISOString() })
  const findings = [notice(1, 'finding'), notice(2, 'finding')]
  const updates = [notice(3, 'progress'), notice(4, 'progress')]
  const pending = pendingNotifications(findings, updates, [{ task_id: 'task' }], now)
  assert.deepEqual(pending.map((n) => n.seq), [1, 2, 4])
  assert.deepEqual(pendingNotifications(pending, updates, [{ task_id: 'task' }], now)
    .map((n) => n.seq), [1, 2, 4])
})
