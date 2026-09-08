import type { Workspace } from '../App.tsx'

interface ResearchResult {
  ok: boolean
  sources: string[]
  error?: string
}

export function recordResearchResult(workspace: Workspace, result: ResearchResult): Workspace {
  return {
    ...workspace,
    phase: result.ok ? 'synthesizing' : 'failed',
    sources: result.sources,
    researchNote: result.error ?? null,
    failure: result.ok ? null : (result.error || 'The research run did not complete.'),
  }
}

export function finishWorkspace(
  workspace: Workspace,
  answer: string,
  error: string | null,
): Workspace {
  // The main model may explain a failed research run. That explanation is a
  // usable chat reply, but it cannot turn the research result into a success.
  if (workspace.phase === 'failed') return workspace
  const failure = error || (answer.trim() ? null :
    (workspace.researchNote || 'The subagent returned no answer.'))
  return { ...workspace, phase: failure ? 'failed' : 'complete', failure }
}
