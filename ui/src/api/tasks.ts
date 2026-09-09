import type { ResearchTask, TaskCommand, TaskSnapshot } from '../types/tasks.ts'

function taskPath(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}/tasks`
}

async function responseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    throw new Error(typeof detail?.detail === 'string'
      ? detail.detail : `Task request failed (${response.status}).`)
  }
  return response.json() as Promise<T>
}

export async function taskSnapshot(sessionId: string, signal?: AbortSignal): Promise<TaskSnapshot> {
  return responseJson(await fetch(taskPath(sessionId), {
    headers: { Accept: 'application/json' }, cache: 'no-store', signal,
  }))
}

export async function sendTaskMessage(
  sessionId: string, taskId: string, command: TaskCommand, signal?: AbortSignal,
): Promise<ResearchTask> {
  return responseJson(await fetch(`${taskPath(sessionId)}/${encodeURIComponent(taskId)}/messages`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(command), signal,
  }))
}

export function artifactUrl(sessionId: string, taskId: string, artifactId: string): string {
  return `${taskPath(sessionId)}/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifactId)}`
}

export async function deleteSavedTask(
  sessionId: string, taskId: string, signal?: AbortSignal,
): Promise<void> {
  await responseJson(await fetch(`${taskPath(sessionId)}/${encodeURIComponent(taskId)}`, {
    method: 'DELETE', signal,
  }))
}

export async function resetSavedTasks(sessionId: string, signal?: AbortSignal): Promise<void> {
  await responseJson(await fetch(`${taskPath(sessionId)}/reset`, { method: 'POST', signal }))
}
