// Notification cards for finished tasks: pure logic, kept free of imports so tests/test_task_notices.py
// can run it under Node.

export const NOTICE_MAX_AGE = 7 * 24 * 3600 // seconds; older finished tasks do not nag

type Finished = { id: string; status: string; updated_at: number }

// Ready or failed tasks from the last week that have not been dismissed, newest first.
export function taskNotices<T extends Finished>(tasks: T[], dismissed: string[], now = Date.now() / 1000): T[] {
  const gone = new Set(dismissed)
  return tasks
    .filter((t) => (t.status === 'ready' || t.status === 'failed') && !gone.has(t.id) && now - t.updated_at < NOTICE_MAX_AGE)
    .sort((a, b) => b.updated_at - a.updated_at)
}

// Cleans saved dismissed ids: strings only, no duplicates, and only the most recent `keep`.
export function pruneDismissed(ids: unknown, keep = 200): string[] {
  if (!Array.isArray(ids)) return []
  return ids.filter((id, i): id is string => typeof id === 'string' && ids.indexOf(id) === i).slice(-keep)
}
