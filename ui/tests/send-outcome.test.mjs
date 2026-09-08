import assert from 'node:assert/strict'
import test from 'node:test'

import { initialSendOutcome, updateSendOutcome } from '../src/lib/send-outcome.ts'
import { finishWorkspace, recordResearchResult } from '../src/lib/workspace.ts'

function done(text = 'A completed answer.', error = null, committed = true) {
  return {
    type: 'done', turn_id: 'turn-1', total_ms: 10,
    generation: { response_text: text, error }, committed,
  }
}

test('a valid final explanation after research failure is speakable and counted once', () => {
  const failure = { type: 'error', message: 'Research connection was lost.' }
  let outcome = updateSendOutcome(initialSendOutcome, failure)
  assert.equal(outcome.failed, true)
  assert.equal(outcome.committed, false)
  assert.equal(outcome.speechTurnId, null)
  outcome = updateSendOutcome(outcome, done('I could not finish that lookup.'))
  assert.equal(outcome.failed, false)
  assert.equal(outcome.committed, true)
  assert.equal(outcome.speechTurnId, 'turn-1')

  const research = recordResearchResult({
    run_id: 'research', task: 'Find current prices', effort: 'focused',
    steps: [], sources: [], phase: 'researching', researchNote: null, failure: null,
  }, { ok: false, sources: [], error: failure.message })
  const workspace = finishWorkspace(research, 'I could not finish that lookup.', null)
  assert.equal(workspace.phase, 'failed')
  assert.equal(workspace.failure, failure.message)
})

for (const [text, error] of [['', null], ['Partial text', 'Generation failed.']]) {
  test(`unsuccessful final generation is neither counted nor spoken: ${error ?? 'empty'}`, () => {
    const outcome = updateSendOutcome(initialSendOutcome, done(text, error, false))
    assert.equal(outcome.completedId, 'turn-1')
    assert.equal(outcome.committed, false)
    assert.equal(outcome.failed, true)
    assert.equal(outcome.speechTurnId, null)
  })
}

test('an incomplete stream never becomes speakable or counted from partial text alone', () => {
  const outcome = updateSendOutcome(initialSendOutcome, {
    type: 'error', message: 'The connection ended before the reply completed.',
  })
  assert.equal(outcome.completedId, null)
  assert.equal(outcome.committed, false)
  assert.equal(outcome.speechTurnId, null)
})

test('counting follows the backend commit even when stored text has nothing to speak', () => {
  const whitespace = updateSendOutcome(initialSendOutcome, done('   ', null, true))
  assert.equal(whitespace.committed, true)
  assert.equal(whitespace.speechTurnId, null)
  const notStored = updateSendOutcome(initialSendOutcome, done('Answer', null, false))
  assert.equal(notStored.committed, false)
})

test('legacy completion events use nonempty successful generation for counting', () => {
  const event = done()
  delete event.committed
  assert.equal(updateSendOutcome(initialSendOutcome, event).committed, true)
  event.generation.error = 'Failed'
  assert.equal(updateSendOutcome(initialSendOutcome, event).committed, false)
  event.generation.error = null
  event.generation.response_text = ''
  assert.equal(updateSendOutcome(initialSendOutcome, event).committed, false)
})

test('an error after the terminal done event cannot undo a persisted completion', () => {
  const complete = updateSendOutcome(initialSendOutcome, done())
  const outcome = updateSendOutcome(complete, { type: 'error', message: 'Late socket error' })
  assert.equal(outcome.committed, true)
  assert.equal(outcome.speechTurnId, 'turn-1')
})
