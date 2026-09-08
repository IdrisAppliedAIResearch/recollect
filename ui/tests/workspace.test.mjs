import assert from 'node:assert/strict'
import test from 'node:test'

import { finishWorkspace, recordResearchResult } from '../src/lib/workspace.ts'

function workspace(steps = []) {
  return {
    run_id: 'research-1', task: 'Research current rental prices', effort: 'deep',
    steps, phase: 'researching', sources: [], researchNote: null, failure: null,
  }
}

test('failed research stays failed when the main model returns an explanation', () => {
  const result = recordResearchResult(workspace(), {
    ok: false, sources: [], error: 'Cannot reach the Docker engine. Start Docker Desktop.',
  })
  assert.equal(result.phase, 'failed')
  assert.equal(result.failure, 'Cannot reach the Docker engine. Start Docker Desktop.')
  const finished = finishWorkspace(result, 'I could not finish that research.', null)
  assert.equal(finished.phase, 'failed')
  assert.equal(finished.failure, result.failure)
  assert.deepEqual(finished.steps, [])
  assert.deepEqual(finished.sources, [])
})

test('partial sources and completed steps do not mask a failed research result', () => {
  const steps = [{ index: 1, tool: 'web_search', args: {}, observation: 'one result', ms: 12 }]
  const result = recordResearchResult(workspace(steps), {
    ok: false, sources: ['https://example.org/report'], error: 'Research connection lost.',
  })
  const finished = finishWorkspace(result, 'Here is the limited information I found.', null)
  assert.equal(finished.phase, 'failed')
  assert.equal(finished.failure, 'Research connection lost.')
  assert.deepEqual(finished.steps, steps)
  assert.deepEqual(finished.sources, ['https://example.org/report'])
})

test('an incomplete research result without a specific error still remains visibly failed', () => {
  const result = recordResearchResult(workspace(), { ok: false, sources: [] })
  const finished = finishWorkspace(result, 'Please try again.', null)
  assert.equal(finished.phase, 'failed')
  assert.equal(finished.failure, 'The research run did not complete.')
})

test('successful research completes only after the answer succeeds', () => {
  const result = recordResearchResult(workspace(), {
    ok: true, sources: ['https://example.org/report'],
  })
  assert.equal(result.phase, 'synthesizing')
  assert.equal(result.failure, null)
  assert.equal(finishWorkspace(result, 'A supported answer.', null).phase, 'complete')
  const interrupted = finishWorkspace(result, 'A partially generated answer', 'Reply stopped.')
  assert.equal(interrupted.phase, 'failed')
  assert.equal(interrupted.failure, 'Reply stopped.')
  assert.equal(finishWorkspace(result, ' ', null).phase, 'failed')
})
