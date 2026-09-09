import { useEffect, useRef, useState } from 'react'

import { artifactUrl, deleteSavedTask, resetSavedTasks, sendTaskMessage } from '../api/tasks.ts'
import { safeSourceUrl } from '../lib/task-notifications.ts'
import { canDeleteSavedWork } from '../lib/task-retention.ts'
import { clock, stamp } from '../lib/format.ts'
import type { ResearchTask, TaskCommand, TaskNotification, TaskSnapshot } from '../types/tasks.ts'
import { Markdown } from './Markdown.tsx'

export function Tasks({ snapshot, error }: { snapshot: TaskSnapshot; error: string | null }) {
  if (!snapshot.enabled && snapshot.tasks.length === 0 && !error) return null
  const active = snapshot.tasks.filter((task) => ['running', 'blocked', 'cancel-requested'].includes(task.state))
  const queued = snapshot.tasks.filter((task) => task.state === 'queued')
  return (
    <details className="tasks">
      <summary>
        <strong>Research workspace</strong>
        <span className="faint">{active.length} active · {queued.length} queued</span>
      </summary>
      <div className="tasks__body">
        {!snapshot.enabled && <p className="callout">New delegated work is disabled. Saved work remains available.</p>}
        {error && <p className="callout callout--bad" role="status">{error} Saved status may be out of date.</p>}
        {snapshot.tasks.length === 0 && <p className="faint">Ask the assistant to research something. You can keep talking while it works.</p>}
        {queued.length > 0 && <p className="faint">One research task runs at a time. Queued work waits for the active task, including when it needs your input; there is no estimated start time.</p>}
        {snapshot.tasks.map((task) => <TaskCard key={task.task_id} task={task}
          enabled={snapshot.enabled}
          dependents={snapshot.tasks.filter((item) => item.parent_task_id === task.task_id).length}
          notifications={snapshot.notifications.filter((item) => item.task_id === task.task_id)} />)}
        {snapshot.tasks.length > 0 && <details className="task__management">
          <summary>Manage saved research</summary>
          <DeleteSavedWork tasks={snapshot.tasks} all />
        </details>}
      </div>
    </details>
  )
}

function TaskCard({ task, enabled, notifications, dependents }: {
  task: ResearchTask; enabled: boolean; notifications: TaskNotification[]; dependents: number
}) {
  const [direction, setDirection] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [receipt, setReceipt] = useState<string | null>(null)
  const retry = useRef<TaskCommand | null>(null)
  const active = useRef<AbortController | null>(null)
  useEffect(() => () => active.current?.abort(), [])
  const done = ['completed', 'canceled', 'interrupted'].includes(task.state)
  const canContinue = done || (task.state === 'blocked' && Boolean(task.error))
  const message = async (operation: TaskCommand['operation']) => {
    if (pending) return
    const payload = {
      operation,
      ...(operation === 'steer' || operation === 'continue' ? { text: direction.trim() } : {}),
      ...(operation === 'quiet' ? { quiet: !task.quiet } : {}),
      ...(operation === 'steer' && task.state === 'blocked'
        ? { reply_to: notifications.filter((item) => item.kind === 'question').at(-1)?.notification_id }
        : {}),
    }
    const previous = retry.current
    const { request_id: _id, ...previousPayload } = previous ?? { request_id: '' }
    const command: TaskCommand = previous && JSON.stringify(previousPayload) === JSON.stringify(payload)
      ? previous : { request_id: crypto.randomUUID(), ...payload }
    retry.current = command
    const abort = new AbortController()
    active.current = abort
    setPending(true)
    setError(null)
    setReceipt(null)
    try {
      await sendTaskMessage(task.session_id, task.task_id, command, abort.signal)
      if (abort.signal.aborted) return
      retry.current = null
      if (operation === 'steer' || operation === 'continue') setDirection('')
      setReceipt(operation === 'cancel' ? 'Cancellation requested.'
        : operation === 'quiet' ? (payload.quiet ? 'Spoken updates muted.' : 'Spoken updates enabled.')
        : 'Direction saved. Its acceptance will appear in task status.')
    } catch (failure) {
      if (!abort.signal.aborted) setError((failure as Error).message)
    } finally {
      if (!abort.signal.aborted) setPending(false)
    }
  }

  return (
    <article className="task">
      <div className="task__head">
        <strong>{task.objective}</strong>
        <span className={'badge' + (task.state === 'blocked' || task.state === 'interrupted'
          ? ' badge--warn' : task.state === 'completed' ? ' badge--ok' : '')}>
          {task.state.replaceAll('-', ' ')}{task.partial ? ' · partial findings' : ''}
        </span>
      </div>
      <details className="task__request"><summary>Original request</summary><Markdown text={task.original_message} /></details>
      {task.revision > task.accepted_revision && <p className="faint">
        {task.revision === 1 ? 'Your request' : 'Your latest direction'} is saved and awaiting acceptance.
      </p>}
      {task.result_revision !== null && task.result_revision < task.revision &&
        <p className="faint">The saved result predates your latest direction.</p>}
      {task.progress && <Markdown text={task.progress} />}
      {task.error && <p className="callout callout--bad">{task.error}</p>}
      {(task.findings.length > 0 || task.result) && <details className="task__findings">
        <summary>Saved {task.partial ? 'partial ' : ''}findings</summary>
        <Markdown text={task.result || task.findings.join('\n\n')} />
      </details>}
      {task.sources.length > 0 && <Sources sources={task.sources} />}
      {task.artifacts.length > 0 && <ul className="task__files">
        {task.artifacts.map((file) => <li key={file.artifact_id}>
          <a href={artifactUrl(task.session_id, task.task_id, file.artifact_id)} download>
            {file.filename}
          </a>
          <span className="faint"> · version {file.version} · {file.size_bytes.toLocaleString()} bytes</span>
          {file.download_path && <div className="faint">Saved on the desktop: {file.download_path}</div>}
          {file.download_error && <div role="status">Could not save to Downloads. Use the download link above.</div>}
        </li>)}
      </ul>}
      {notifications.length > 0 && <details className="task__updates">
        <summary>Conversation updates ({notifications.length})</summary>
        {notifications.map((item) => <div className="task__notification" key={item.notification_id}>
          <div className="msg__meta"><time dateTime={item.created_at} title={stamp(item.created_at)}>{clock(item.created_at)}</time>
            <span>{item.kind}</span></div>
          {item.user_message && <div className="task__user"><Markdown text={item.user_message} /></div>}
          <Markdown text={item.text} />
          {item.source_refs.length > 0 && <Sources sources={item.source_refs} />}
        </div>)}
      </details>}
      {enabled && <div className="task__controls">
        <label className="task__quiet"><input type="checkbox" checked={task.quiet}
          disabled={pending} onChange={() => void message('quiet')} /> Quiet updates</label>
        <textarea className="composer__input" aria-label={`Direction for ${task.objective}`}
          placeholder={canContinue ? 'Optional direction for continuing this work…' : 'Refine this task or answer its question…'}
          rows={2} value={direction} disabled={pending || task.state === 'cancel-requested'}
          onChange={(event) => setDirection(event.target.value)} />
        <div className="rowflex">
          <button type="button" className="btn btn--ghost" disabled={pending ||
            task.state === 'cancel-requested' || (!canContinue && !direction.trim())}
            onClick={() => void message(canContinue ? 'continue' : 'steer')}>
            {canContinue ? 'Continue work' : task.state === 'blocked' ? 'Send answer' : 'Send direction'}
          </button>
          {!done && <button type="button" className="btn btn--ghost"
            disabled={pending || task.state === 'cancel-requested'}
            onClick={() => void message('cancel')}>Cancel task</button>}
        </div>
      </div>}
      {(receipt || error) && <p className={error ? 'callout callout--bad' : 'faint'} role="status">{error || receipt}</p>}
      {done && <details className="task__management">
        <summary>Manage this saved task</summary>
        <DeleteSavedWork tasks={[task]} dependents={dependents} />
      </details>}
    </article>
  )
}

function DeleteSavedWork({ tasks, all = false, dependents = 0 }: {
  tasks: ResearchTask[]; all?: boolean; dependents?: number
}) {
  const [review, setReview] = useState(false)
  const [pending, setPending] = useState(false)
  const [feedback, setFeedback] = useState<string | null>(null)
  const [deleted, setDeleted] = useState(false)
  const active = useRef<AbortController | null>(null)
  useEffect(() => () => active.current?.abort(), [])
  const allowed = canDeleteSavedWork(tasks)
  const files = tasks.reduce((count, task) => count + task.artifacts.length, 0)
  const remove = async () => {
    if (!review || !allowed || pending || deleted) return
    const abort = new AbortController()
    active.current = abort
    setPending(true)
    setFeedback(null)
    try {
      if (all) await resetSavedTasks(tasks[0].session_id, abort.signal)
      else await deleteSavedTask(tasks[0].session_id, tasks[0].task_id, abort.signal)
      if (abort.signal.aborted) return
      setDeleted(true)
      setReview(false)
      setFeedback('Saved research deleted.')
    } catch (error) {
      if (!abort.signal.aborted) setFeedback((error as Error).message)
    } finally {
      if (!abort.signal.aborted) setPending(false)
    }
  }
  return <div className="task__deletion">
    {review ? <div className="callout callout--warn">
      <p>Delete {all ? `all ${tasks.length} saved research tasks` : 'this saved task'},
        including findings, research updates, and {files} file version{files === 1 ? '' : 's'}?
        This cannot be undone. Existing chat turns remain.</p>
      {dependents > 0 && <p>{dependents} follow-up task{dependents === 1 ? '' : 's'} refer to this saved work.</p>}
      <div className="rowflex">
        <button type="button" className="btn" disabled={!allowed || pending || deleted}
          onClick={() => void remove()}>{pending ? 'Deleting…' : 'Delete task data and files'}</button>
        <button type="button" className="btn btn--ghost" disabled={pending}
          onClick={() => setReview(false)}>Keep saved work</button>
      </div>
    </div> : <button type="button" className="btn btn--ghost"
      disabled={!allowed || pending || deleted} onClick={() => setReview(true)}>
      {all ? 'Delete all saved research' : 'Delete saved task'}
    </button>}
    {!allowed && <p className="faint">Cancel active or queued tasks before deleting saved research.</p>}
    {feedback && <p className="faint" role="status">{feedback}</p>}
  </div>
}

export function Sources({ sources }: { sources: string[] }) {
  return <details className="task__sources"><summary>Sources</summary><ul>
    {sources.map((value, index) => {
      const url = safeSourceUrl(value)
      return <li key={`${index}:${value}`}>{url
        ? <a href={url} target="_blank" rel="noreferrer">{value}</a> : value}</li>
    })}
  </ul></details>
}
