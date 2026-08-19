/**
 * The research subagent's live work, as it happens.
 *
 * Ephemeral by contract: this view is React state fed by mid-turn SSE events,
 * and nothing here is ever written to the episode store. Reload the page and
 * it is gone, while the chat and the one-line trace summary remain.
 */
import { useEffect, useState } from 'react'

import { chars, ms } from '../lib/format.ts'
import type { Workspace } from '../App.tsx'

interface Props {
  workspace: Workspace
}

export function Workspace({ workspace }: Props) {
  const [expanded, setExpanded] = useState(false)
  const active =
    workspace.phase === 'researching' || workspace.phase === 'synthesizing'
  const failed = workspace.phase === 'failed'

  // Research is supporting activity, not the main reply. Every new run starts
  // as a quiet pill; opening it is an explicit choice that remains stable as
  // the run moves from searching to synthesis to completion.
  useEffect(() => {
    setExpanded(false)
  }, [workspace.run_id])

  const phaseLabel =
    workspace.phase === 'researching'
      ? workspace.steps.length
        ? 'Researching'
        : 'Preparing'
      : workspace.phase === 'synthesizing'
        ? 'Synthesizing answer'
        : failed
          ? 'No answer returned'
          : 'Complete'

  return (
    <section className="workpane" data-open={expanded} data-phase={workspace.phase}>
      <button
        type="button"
        className="workpane__head"
        aria-expanded={expanded}
        onClick={() => setExpanded((open) => !open)}
        title={workspace.task}
      >
        <span className="workpane__agent" aria-hidden>
          R
        </span>
        <span className="workpane__label">Research subagent</span>
        <span className="workpane__summary">
          {active ? (
            <span className="workpane__phase">
              <span className="spin" aria-hidden />
              {phaseLabel}
              {workspace.steps.length > 0 && (
                <span className="workpane__live-count">
                  <span aria-hidden>·</span> {workspace.steps.length} steps
                </span>
              )}
            </span>
          ) : failed ? (
            <span className="badge badge--bad">failed</span>
          ) : (
            <span className="workpane__count">
              {workspace.steps.length} steps
              <span aria-hidden>·</span>
              {workspace.sources.length} sources
            </span>
          )}
          <span className="workpane__chevron" aria-hidden>
            ›
          </span>
        </span>
      </button>

      {expanded && (
        <div className="workpane__body">
          <div className="workpane__brief">
            <span className="workpane__eyebrow">Task</span>
            <span className="workpane__task-full">{workspace.task}</span>
          </div>

          <div className="workpane__steps">
            {workspace.steps.map((step) => (
              <div key={step.index} className="workpane__row">
                <span className="workpane__num">{String(step.index).padStart(2, '0')}</span>
                <span className="workpane__step-main">
                  <span className="workpane__step-line">
                    <span className="workpane__tool">{toolLabel(step.tool)}</span>
                    <code className="workpane__args" title={stepTarget(step.args)}>
                      {stepTarget(step.args)}
                    </code>
                    <span className="workpane__ms">{ms(step.ms)}</span>
                  </span>
                  <span className="workpane__obs" title={step.observation}>
                    {summarizeObservation(step.observation)}
                  </span>
                </span>
              </div>
            ))}

            {active && (
              <div className="workpane__activity" aria-live="polite">
                <span className="spin" aria-hidden />
                <span>
                  {workspace.phase === 'synthesizing'
                    ? `Turning ${workspace.sources.length} sources into an answer…`
                    : workspace.steps.length
                      ? 'Choosing the next source…'
                      : 'Preparing the first search…'}
                </span>
              </div>
            )}
          </div>

          {failed && (
            <div className="callout callout--bad workpane__failure">
              <span className="callout__mark">!</span>
              <div className="callout__body">
                <div className="callout__title">The subagent returned no answer</div>
                {workspace.failure ?? 'The research run ended without usable output.'}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  )
}

function toolLabel(tool: string): string {
  return tool.replace(/^web_/, '')
}

/** The one argument that identifies a step: the query, or the URL fetched. */
function stepTarget(args: Record<string, unknown>): string {
  const target =
    typeof args.query === 'string' ? args.query : typeof args.url === 'string' ? args.url : null
  if (target) return truncateMiddle(target, 48)
  try {
    return truncateMiddle(JSON.stringify(args), 48)
  } catch {
    return ''
  }
}

/**
 * One short phrase per observation, whatever the tool did wrong or well.
 * The observations are JSON documents, so ask each shape for its headline
 * number instead of dumping the body — that body is what must stay out of
 * every persisted place.
 */
function summarizeObservation(observation: string): string {
  let doc: unknown
  try {
    doc = JSON.parse(observation)
  } catch {
    doc = null
  }
  if (doc && typeof doc === 'object') {
    const record = doc as Record<string, unknown>
    if (Array.isArray(record.results)) {
      const found = record.results.length
      const errors = Array.isArray(record.errors) ? record.errors.length : 0
      if (found) return `${found} result${found === 1 ? '' : 's'}`
      return errors ? `${errors} leg${errors === 1 ? '' : 's'} failed` : 'no results'
    }
    if (typeof record.text === 'string') {
      const status = typeof record.status === 'number' ? record.status : null
      const note = record.truncated ? ' · truncated' : ''
      return `${status ?? '?'} · ${chars(record.text.length)} chars${note}`
    }
    if (typeof record.error === 'string') return record.error
  }
  const plain = observation.replace(/\s+/g, ' ').trim()
  return truncateMiddle(plain, 48) || '(empty)'
}

function truncateMiddle(text: string, size: number): string {
  if (text.length <= size) return text
  if (size <= 1) return '…'
  const head = Math.ceil((size - 1) / 2)
  return `${text.slice(0, head)}…${text.slice(text.length - Math.floor((size - 1) / 2))}`
}
