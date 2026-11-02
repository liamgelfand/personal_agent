import { ChevronRight } from 'lucide-react'
import { useEffect, useState } from 'react'
import { api } from '../api'
import { useApp } from '../store'
import type { AuthStatus } from '../types'
import { PhonePanel } from './PhonePanel'

const WINDOW_LABELS: Record<string, string> = { five_hour: '5-hour', seven_day: 'Weekly' }

export function TopBar() {
  const repos = useApp((s) => s.repos)
  const workspace = useApp((s) => s.workspace)
  const tasks = useApp((s) => s.tasks)
  const windows = useApp((s) => s.windows)
  const setWorkspace = useApp((s) => s.setWorkspace)

  // Only tasks that still have a worktree can be opened as a workspace.
  const repoTasks = tasks.filter((t) => t.repo === workspace?.repo && t.status !== 'discarded' && t.status !== 'merged')
  const access = repos.find((r) => r.name === workspace?.repo)?.access
  const [auth, setAuth] = useState<AuthStatus | null>(null)

  useEffect(() => {
    api.auth().then(setAuth, () => setAuth(null))
  }, [])

  const onPlan = auth?.authMethod === 'claude.ai' && !auth.api_key_in_env
  const plan = auth?.subscriptionType ? auth.subscriptionType[0].toUpperCase() + auth.subscriptionType.slice(1) : ''

  return (
    <header className="topbar">
      <span className="wordmark">Personal Agent</span>
      <select
        className="crumb"
        aria-label="Project"
        value={workspace?.repo ?? ''}
        onChange={(e) => setWorkspace({ repo: e.target.value, task: null })}
      >
        {repos.map((r) => (
          <option key={r.name} value={r.name}>{r.name}</option>
        ))}
      </select>
      <ChevronRight size={14} className="crumb-sep" />
      <select
        className="crumb"
        aria-label="Workspace"
        value={workspace?.task ?? ''}
        onChange={(e) => workspace && setWorkspace({ repo: workspace.repo, task: e.target.value || null })}
      >
        <option value="">Main checkout</option>
        {repoTasks.map((t) => (
          <option key={t.id} value={t.id}>Task: {t.title}</option>
        ))}
      </select>
      {access && <span className="tag">{workspace?.task ? 'Task worktree' : access === 'write' ? 'Read / write' : 'Read only'}</span>}
      <div className="spacer" />
      <div className="usage" title="Claude plan usage">
        {Object.entries(windows).map(([key, w]) => (
          <div key={key} className="usage-item" title={`Resets ${new Date(w.resetsAt * 1000).toLocaleString()}`}>
            <span>{WINDOW_LABELS[key] ?? key}</span>
            <div className="usage-bar">
              <div className={`usage-fill ${w.utilization > 0.8 ? 'hot' : ''}`} style={{ width: `${Math.min(100, w.utilization * 100)}%` }} />
            </div>
            <span className="num">{Math.round(w.utilization * 100)}%</span>
          </div>
        ))}
      </div>
      {auth && (
        <span
          className={`billing ${onPlan ? '' : 'warn'}`}
          title={auth.error ?? `${auth.email ?? ''} · ${auth.orgName ?? ''}
Billing mode: ${auth.billing}`}
        >
          <span className={`dot ${onPlan ? 'ok' : 'warn'}`} />
          {onPlan ? `Claude ${plan} plan` : auth.error ? 'Login unknown' : 'API billing'}
        </span>
      )}
      <PhonePanel />
    </header>
  )
}
