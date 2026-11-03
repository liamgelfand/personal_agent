import { ExternalLink, GitBranch, GitMerge, X } from 'lucide-react'
import { useState } from 'react'
import { api } from '../api'
import { tabId, useApp } from '../store'
import { taskNotices } from '../taskNotices'
import type { Task, TaskStatus } from '../types'
import { openExternal, openUrl, reserveTab } from './ExternalLink'

export const ACTIVE_STATUSES = new Set<TaskStatus>(['queued', 'implementing', 'reviewing', 'fixing'])

export function StatusDot({ status }: { status: TaskStatus }) {
  const tone = ACTIVE_STATUSES.has(status) ? 'live'
    : status === 'ready' || status === 'merged' ? 'ok'
    : status === 'failed' ? 'danger' : ''
  return (
    <span className="status">
      <span className={`dot ${tone}`} />
      {status}
    </span>
  )
}

// Finished tasks (any repo) that still need a look, until dismissed. The Tasks tab shows their count.
export function useTaskNotices() {
  const tasks = useApp((s) => s.tasks)
  const dismissed = useApp((s) => s.dismissedTasks)
  return taskNotices(tasks, dismissed)
}

function TaskNotices() {
  const notices = useTaskNotices()
  const dismissTask = useApp((s) => s.dismissTask)

  // Show the task's worktree with its changes listed, where each file opens as a diff.
  function viewDiff(t: Task) {
    useApp.setState({ workspace: { repo: t.repo, task: t.id }, sideTab: 'changes', mobileView: 'changes' })
  }

  if (notices.length === 0) return null
  return (
    <div className="notices" aria-label="Task notifications">
      {notices.map((t) => (
        <div key={t.id} className={`notice ${t.status}`}>
          <div className="notice-head">
            <span className="status">
              <span className={`dot ${t.status === 'ready' ? 'ok' : 'danger'}`} />
              {t.status === 'ready' ? 'Ready for review' : 'Failed'}
            </span>
            <span className="faint small ellipsis">{t.repo}</span>
            <button className="icon-btn notice-close" onClick={() => dismissTask(t.id)} aria-label="Dismiss" title="Dismiss">
              <X size={13} />
            </button>
          </div>
          <div className="task-title">{t.title}</div>
          {t.status === 'failed' && t.error && <div className="error-text small">{t.error}</div>}
          {t.status === 'ready' && t.finish_error && <div className="warn-text small">{t.finish_error}</div>}
          <div className="task-actions">
            {t.pr_url && (
              <a className="btn sm primary" href={t.pr_url} target="_blank" rel="noreferrer" onClick={(e) => openExternal(e, t.pr_url!)}>
                {t.pr_state === 'compare' ? 'Open compare page' : 'Review PR'} <ExternalLink size={12} />
              </a>
            )}
            <button className="btn sm" onClick={() => viewDiff(t)}>View diff</button>
          </div>
        </div>
      ))}
    </div>
  )
}

export function TasksPanel({ repo }: { repo: string }) {
  const tasks = useApp((s) => s.tasks).filter((t) => t.repo === repo && t.status !== 'discarded')
  const workspace = useApp((s) => s.workspace)
  const setWorkspace = useApp((s) => s.setWorkspace)
  const openTab = useApp((s) => s.openTab)
  const showError = useApp((s) => s.showError)
  const showInfo = useApp((s) => s.showInfo)

  // Tasks whose Open PR request is in flight; the push and gh call take seconds, so block repeat clicks.
  const [opening, setOpening] = useState<Record<string, boolean>>({})

  async function openPr(t: Task) {
    const tab = reserveTab()
    setOpening((o) => ({ ...o, [t.id]: true }))
    try {
      openUrl((await api.openPr(t.id)).url, tab)
    } catch (e) {
      tab?.close()
      showError(e)
    } finally {
      setOpening((o) => ({ ...o, [t.id]: false }))
    }
  }

  async function merge(t: Task) {
    const message = prompt(
      `Merge "${t.title}" into ${t.repo}'s checked-out branch.\n\n` +
        'Commit message for any uncommitted task changes:',
      t.title,
    )
    if (message === null) return
    try {
      const { sha } = await api.mergeTask(t.id, message)
      if (workspace?.task === t.id) setWorkspace({ repo: t.repo, task: null })
      showInfo(`Merged into ${t.repo} as ${sha}`)
    } catch (e) {
      showError(e)
    }
  }

  async function discard(t: Task) {
    if (!confirm(`Discard "${t.title}"? This deletes its worktree and the branch ${t.branch}.`)) return
    if (workspace?.task === t.id) setWorkspace({ repo: t.repo, task: null })
    await api.discardTask(t.id).catch(showError)
  }

  return (
    <div className="panel-body">
      <TaskNotices />
      {tasks.length === 0 && (
        <p className="pad faint small">
          No tasks. Describe a change in a chat linked to this repo and the agent starts a task in its own git worktree.
        </p>
      )}
      {tasks.map((t) => {
        const active = ACTIVE_STATUSES.has(t.status)
        const merged = t.status === 'merged'
        return (
          <div key={t.id} className={`task-row ${workspace?.task === t.id ? 'current' : ''}`}>
            <div className="task-title">{t.title}</div>
            <div className="task-meta">
              <StatusDot status={t.status} />
              <GitBranch size={12} />
              <span className="ellipsis mono" title={t.branch}>{t.branch}</span>
            </div>
            {t.error && <div className="error-text small">{t.error}</div>}
            {t.timing && <div className="faint small" title="Time per phase">{t.timing}</div>}
            {t.finish_error && !merged && <div className="warn-text small">{t.finish_error}</div>}
            <div className="task-actions">
              <button className="btn sm" onClick={() => openTab({ id: tabId.task(t.id), kind: 'task', taskId: t.id })}>
                Log
              </button>
              {merged ? null : (
                <button className="btn sm" onClick={() => setWorkspace({ repo: t.repo, task: t.id })} disabled={workspace?.task === t.id}>
                  Open workspace
                </button>
              )}
              {active && <button className="btn sm" onClick={() => api.stopTask(t.id).catch(showError)}>Stop</button>}
              {!active && !merged && (
                <button className="btn sm" onClick={() => merge(t)} disabled={opening[t.id]} title="Commit the task's changes if needed and merge its branch into your checkout">
                  <GitMerge size={12} /> Merge
                </button>
              )}
              {t.status === 'ready' && (!t.pr_url || t.pr_state === 'compare') && (
                <button
                  className="btn sm"
                  onClick={() => openPr(t)}
                  disabled={opening[t.id]}
                  title="Commit any changes, push the branch and open a pull request"
                >
                  {opening[t.id] ? 'Opening PR…' : 'Open PR'}
                </button>
              )}
              {t.pr_url && (
                <a
                  className="btn sm"
                  href={t.pr_url}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(e) => openExternal(e, t.pr_url!)}
                >
                  {t.pr_state === 'compare' ? 'Compare page' : 'Pull request'} <ExternalLink size={12} />
                </a>
              )}
              {!active && !merged && <button className="btn sm danger" onClick={() => discard(t)} disabled={opening[t.id]}>Discard</button>}
            </div>
          </div>
        )
      })}
    </div>
  )
}
