import { useEffect, useRef, useState } from 'react'

import { decideBuild } from '../api/tasks.ts'
import type { ResearchTask } from '../types/tasks.ts'

/**
 * The connect / build go-no-go, rendered where the ask was made so the
 * buttons sit in the conversation turn rather than only in the task card.
 *
 * It shows only while the task still carries an open proposal; the snapshot
 * clears it the moment the user decides, so the card leaves with the answer.
 */
export function ProposalDecision({ task }: { task: ResearchTask }) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [receipt, setReceipt] = useState<string | null>(null)
  const active = useRef<AbortController | null>(null)
  useEffect(() => () => active.current?.abort(), [])

  const connect = task.connect_proposal ?? null
  const build = task.build_proposal ?? null
  if (!connect && !build) return null

  const decide = async (approve: boolean) => {
    if (pending) return
    const abort = new AbortController()
    active.current = abort
    setPending(true)
    setError(null)
    setReceipt(null)
    try {
      await decideBuild(task.session_id, task.task_id, approve, abort.signal)
      if (abort.signal.aborted) return
      let receiptText: string
      if (connect) {
        receiptText = approve
          ? 'Opening the sign-in in your browser.'
          : 'Not connecting it.'
      } else {
        receiptText = approve ? 'Building the capability.' : 'Not building it.'
      }
      setReceipt(receiptText)
    } catch (failure) {
      if (!abort.signal.aborted) setError((failure as Error).message)
    } finally {
      if (!abort.signal.aborted) setPending(false)
    }
  }

  return (
    <>
      {/* The card sits inside the inspectable turn bubble; keep its clicks
          and keys from also selecting (or double-firing on) that turn. */}
      <div className="callout callout--warn task__build"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => event.stopPropagation()}>
        {connect
          ? <p>Connect {connect.name} for {connect.missing_capability || 'this'}? You allow it on
            {connect.name}'s own page, and your request resumes afterwards.</p>
          : <p>Build the missing capability{build && build.missing_capability
            ? `: ${build.missing_capability}` : ''}?</p>}
        <div className="rowflex">
          <button type="button" className="btn" disabled={pending}
            onClick={() => void decide(true)}>{connect ? 'Connect' : 'Build it'}</button>
          <button type="button" className="btn btn--ghost" disabled={pending}
            onClick={() => void decide(false)}>{connect ? 'Not now' : "Don't build"}</button>
        </div>
      </div>
      {(receipt || error) && (
        <p className={error ? 'callout callout--bad' : 'faint'} role="status">{error || receipt}</p>
      )}
    </>
  )
}
