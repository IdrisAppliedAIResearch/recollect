import { useEffect, useRef, useState } from 'react'

import { implementationStatus, stopImplementation } from '../api/selfmod.ts'
import type { ImplementationStatus } from '../api/selfmod.ts'
import { decideBuild } from '../api/tasks.ts'
import { clock, stamp } from '../lib/format.ts'

const LABELS: Record<string, string> = {
  idle: 'idle',
  awaiting_approval: 'waiting for your answer',
  running: 'building',
  finished: 'finished',
  failed: 'failed',
  stopped: 'stopped',
  declined: 'not built',
}

/** Polls the build status while the page is visible. */
export function useImplementation(): ImplementationStatus | null {
  const [status, setStatus] = useState<ImplementationStatus | null>(null)
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let active: AbortController | null = null
    const poll = async () => {
      if (stopped || document.hidden || active) return
      const abort = new AbortController()
      active = abort
      try {
        const next = await implementationStatus(abort.signal)
        if (!stopped && !abort.signal.aborted) setStatus(next)
      } catch {
        // Keep the last known status; the next poll retries.
      } finally {
        if (active === abort) active = null
        if (!stopped && !document.hidden) timer = setTimeout(() => void poll(), 2000)
      }
    }
    const visibility = () => {
      clearTimeout(timer)
      if (document.hidden) active?.abort()
      else void poll()
    }
    document.addEventListener('visibilitychange', visibility)
    void poll()
    return () => {
      stopped = true
      active?.abort()
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [])
  return status
}

/** The implementation agents' workspace, beside the research workspace. */
export function Implementation({ sessionId }: { sessionId: string | null }) {
  const status = useImplementation()
  const [pending, setPending] = useState(false)
  const [feedback, setFeedback] = useState<string | null>(null)
  const active = useRef<AbortController | null>(null)
  useEffect(() => () => active.current?.abort(), [])
  if (!status?.enabled) return null
  const state = status.state ?? 'idle'
  const activity = status.activity ?? []
  const here = status.session_id === sessionId

  const act = async (run: (signal: AbortSignal) => Promise<unknown>, done: string) => {
    if (pending) return
    const abort = new AbortController()
    active.current = abort
    setPending(true)
    setFeedback(null)
    try {
      await run(abort.signal)
      if (!abort.signal.aborted) setFeedback(done)
    } catch (error) {
      if (!abort.signal.aborted) setFeedback((error as Error).message)
    } finally {
      if (!abort.signal.aborted) setPending(false)
    }
  }

  return (
    <details className="tasks implementation">
      <summary>
        <strong>Implementation workspace</strong>
        <span className="faint">{LABELS[state] ?? state}
          {state === 'running' && status.attempt ? ` · attempt ${status.attempt}` : ''}</span>
      </summary>
      <div className="tasks__body">
        {state === 'idle' ? <p className="faint">
          When a task needs a capability Recollect doesn't have, you'll be asked
          whether to build it. The agents' work appears here.
        </p> : <>
          <div className="implementation__state">
            <span className={'badge' + (state === 'failed' || state === 'stopped'
              ? ' badge--warn' : state === 'finished' ? ' badge--ok' : '')}>{LABELS[state] ?? state}</span>
            {status.missing_capability && <span>{status.missing_capability}</span>}
          </div>
          {!here && <p className="faint">This build belongs to another conversation.</p>}
          {state === 'awaiting_approval' && status.session_id && status.task_id &&
            <div className="rowflex">
              <button type="button" className="btn" disabled={pending}
                onClick={() => void act((signal) => decideBuild(
                  status.session_id!, status.task_id!, true, signal), 'Building the capability.')}>
                Build it</button>
              <button type="button" className="btn btn--ghost" disabled={pending}
                onClick={() => void act((signal) => decideBuild(
                  status.session_id!, status.task_id!, false, signal), 'Not building it.')}>
                Don't build</button>
            </div>}
          {state === 'running' && <div className="rowflex">
            <button type="button" className="btn btn--ghost" disabled={pending}
              onClick={() => void act(stopImplementation, 'Stop requested.')}>Stop building</button>
          </div>}
          {feedback && <p className="faint" role="status">{feedback}</p>}
          {activity.length > 0 && <ol className="implementation__log">
            {activity.map((item, index) => <li key={`${index}:${item.at}`}>
              <time dateTime={item.at} title={stamp(item.at)}>{clock(item.at)}</time>
              <span>{item.text}</span>
            </li>)}
          </ol>}
        </>}
      </div>
    </details>
  )
}
