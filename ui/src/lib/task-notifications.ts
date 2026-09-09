import type { ResearchTask, TaskNotification } from '../types/tasks.ts'

/** Coalesce unattended progress without losing a final result or question. */
export function pendingNotifications(
  pending: TaskNotification[], incoming: TaskNotification[], tasks: ResearchTask[], now = Date.now(),
): TaskNotification[] {
  const allowed = new Set(tasks.filter((task) => !task.quiet).map((task) => task.task_id))
  const selected = new Map<string, TaskNotification>()
  for (const item of [...pending, ...incoming]) {
    const created = Date.parse(item.created_at)
    if (!allowed.has(item.task_id) || !item.text.trim() ||
        !Number.isFinite(created) || now - created > 120_000) continue
    const key = `${item.task_id}:${['result', 'question', 'blocked'].includes(item.kind) ? item.kind : 'progress'}`
    if (!selected.has(key) || selected.get(key)!.seq < item.seq) selected.set(key, item)
  }
  for (const item of selected.values()) {
    if (['result', 'question', 'blocked'].includes(item.kind)) {
      const progress = selected.get(`${item.task_id}:progress`)
      if (progress && progress.seq <= item.seq) selected.delete(`${item.task_id}:progress`)
    }
  }
  return [...selected.values()].sort((a, b) => a.seq - b.seq).slice(-8)
}

export function safeSourceUrl(value: string): string | null {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : null
  } catch {
    return null
  }
}
