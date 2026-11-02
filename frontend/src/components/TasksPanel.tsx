import { ExternalLink, GitBranch, GitMerge } from 'lucide-react'
import { api } from '../api'
import { tabId, useApp } from '../store'
import type { Task, TaskStatus } from '../types'

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

export function TasksPanel({ repo }: { repo: string }) {
  const tasks = useApp((s) => s.tasks).filter((t) => t.repo === repo && t.status !== 'discarded')
  const workspace = useApp((s) => s.workspace)
  const setWorkspace = useApp((s) => s.setWorkspace)
  const openTab = useApp((s) => s.openTab)
  const showError = useApp((s) => s.showError)
  const showInfo = useApp((s) => s.showInfo)

  async function openPr(t: Task) {
    try {
      const { url } = await api.openPr(t.id)
      window.open(url, '_blank', 'noopener')
    } catch (e) {
      showError(e)
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

  if (tasks.length === 0) {
    return (
      <p className="pad faint small">
        No tasks. Describe a change in a chat linked to this repo and the agent starts a task in its own git worktree.
      </p>
    )
  }

  return (
    <div className="panel-body">
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
                <button className="btn sm" onClick={() => merge(t)} title="Commit the task's changes if needed and merge its branch into your checkout">
                  <GitMerge size={12} /> Merge
                </button>
              )}
              {t.status === 'ready' && !t.pr_url && (
                <button className="btn sm" onClick={() => openPr(t)} title="Push the branch and open a pull request. Commit first.">
                  Open PR
                </button>
              )}
              {t.pr_url && (
                <a className="btn sm" href={t.pr_url} target="_blank" rel="noreferrer">
                  Pull request <ExternalLink size={12} />
                </a>
              )}
              {!active && !merged && <button className="btn sm danger" onClick={() => discard(t)}>Discard</button>}
            </div>
          </div>
        )
      })}
    </div>
  )
}
