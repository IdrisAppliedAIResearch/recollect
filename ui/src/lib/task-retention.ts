import type { ResearchTask } from '../types/tasks.ts'

export function canDeleteSavedWork(tasks: ResearchTask[]): boolean {
  return tasks.length > 0 && tasks.every((task) =>
    ['completed', 'canceled', 'interrupted'].includes(task.state))
}
