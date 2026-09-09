import { useEffect, useRef, useState } from 'react'

import { taskSnapshot } from '../api/tasks.ts'
import type { TaskNotification, TaskSnapshot } from '../types/tasks.ts'

const empty: TaskSnapshot = { enabled: false, tasks: [], notifications: [], cursor: 0 }

export function useTasks(
  sessionId: string | null, enabled: boolean,
  onNotifications: (notifications: TaskNotification[], snapshot: TaskSnapshot) => void,
) {
  const [view, setView] = useState({ sessionId, snapshot: empty, error: null as string | null })
  const notify = useRef(onNotifications)
  notify.current = onNotifications

  useEffect(() => {
    if (!sessionId || !enabled) return
    let stopped = false
    let cursor: number | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let active: AbortController | null = null
    const poll = async () => {
      if (stopped || document.hidden || active) return
      const abort = new AbortController()
      active = abort
      try {
        const snapshot = await taskSnapshot(sessionId, abort.signal)
        if (stopped || abort.signal.aborted) return
        setView({ sessionId, snapshot, error: null })
        if (cursor !== null) {
          notify.current(snapshot.notifications.filter((item) => item.seq > cursor!), snapshot)
        }
        cursor = snapshot.cursor
      } catch (error) {
        if (!stopped && !abort.signal.aborted) {
          setView((current) => ({
            sessionId,
            snapshot: current.sessionId === sessionId ? current.snapshot : empty,
            error: (error as Error).message,
          }))
        }
      } finally {
        if (active === abort) active = null
        if (!stopped && !document.hidden) timer = setTimeout(() => void poll(), 1000)
      }
    }
    const visibility = () => {
      clearTimeout(timer)
      cursor = null
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
  }, [sessionId, enabled])

  return enabled && view.sessionId === sessionId ? view : { sessionId, snapshot: empty, error: null }
}
