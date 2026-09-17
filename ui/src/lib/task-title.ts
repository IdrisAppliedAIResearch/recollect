import type { ResearchTask } from '../types/tasks.ts'

/** A resumed request's objective is an internal brief; show the user's words. */
export function taskTitle(task: Pick<ResearchTask, 'objective' | 'original_message' | 'checkpoint'>): string {
  return task.checkpoint?.selfmod_continuation
    ? `Resumed: ${task.original_message}` : task.objective
}
