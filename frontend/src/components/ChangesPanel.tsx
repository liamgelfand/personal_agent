import { Check, CircleAlert, GitBranch, LoaderCircle, Minus, Plus, Undo2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { tabId, useApp } from '../store'
import { wsKey } from '../types'
import type { FileStatus, Finding, GitStatus, WorkspaceRef } from '../types'

export const SEVERITY_LABEL = { blocking: 'Blocking', should_fix: 'Should fix', nit: 'Nit' }

function letter(f: FileStatus, staged: boolean) {
  if (f.untracked) return 'U'
  return (staged ? f.index : f.worktree).trim() || 'M'
}

export function FindingRow({ finding, onClick, showDetail = false }: { finding: Finding; onClick?: () => void; showDetail?: boolean }) {
  return (
    <button className="finding" onClick={onClick}>
      <span className={`sev sev-${finding.severity}`}>{SEVERITY_LABEL[finding.severity]}</span>
      <span className="finding-title">{finding.title}</span>
      <span className="where">{finding.file}:{finding.line}</span>
      {showDetail && <span className="detail">{finding.detail}</span>}
    </button>
  )
}

export function ChangesPanel({ ws }: { ws: WorkspaceRef }) {
  const key = wsKey(ws)
  const version = useApp((s) => s.fsVersion[key] ?? 0)
  const reviewState = useApp((s) => s.reviews[key])
  const activeTab = useApp((s) => s.activeTab)
  const openTab = useApp((s) => s.openTab)
  const loadReview = useApp((s) => s.loadReview)
  const showError = useApp((s) => s.showError)
  const [status, setStatus] = useState<GitStatus | null>(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(() => {
    api.status(ws).then(setStatus, showError)
  }, [key]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(refresh, [refresh, version])
  useEffect(() => {
    loadReview(ws).catch(showError)
  }, [key]) // eslint-disable-line react-hooks/exhaustive-deps

  const readOnly = status?.access === 'read'
  const staged = status?.files.filter((f) => f.staged) ?? []
  const unstaged = status?.files.filter((f) => f.unstaged) ?? []

  async function act(action: 'stage' | 'unstage' | 'discard', paths: string[]) {
    if (action === 'discard' && !confirm(`Discard changes to ${paths.length} file(s)? This cannot be undone.`)) return
    try {
      await api.gitAction(ws, action, paths)
    } catch (e) {
      showError(e)
    }
    refresh()
  }

  async function commit() {
    if (!message.trim()) return
    setBusy(true)
    try {
      if (staged.length === 0) {
        if (!confirm('Nothing is staged. Stage all changes and commit?')) return
        await api.gitAction(ws, 'stage', unstaged.map((f) => f.path))
      }
      await api.commit(ws, message)
      setMessage('')
    } catch (e) {
      showError(e)
    } finally {
      setBusy(false)
      refresh()
    }
  }

  function openDiff(path: string, isStaged: boolean, line?: number) {
    openTab({ id: tabId.diff(ws, path, isStaged), kind: 'diff', ws, path, staged: isStaged, line })
  }

  function openFinding(f: Finding) {
    const changed = status?.files.find((s) => s.path === f.file)
    if (changed) openDiff(f.file, changed.staged && !changed.unstaged, f.line)
    else openTab({ id: tabId.file(ws, f.file), kind: 'file', ws, path: f.file })
  }

  if (status && !status.is_repo) return <p className="pad faint">Not a git repository.</p>

  const row = (f: FileStatus, isStaged: boolean) => {
    const slash = f.path.lastIndexOf('/')
    const active = activeTab === tabId.diff(ws, f.path, isStaged)
    return (
      <div
        key={`${isStaged}-${f.path}`}
        className={`change-row ${active ? 'active' : ''}`}
        onClick={() => openDiff(f.path, isStaged)}
        title={f.path}
      >
        <span className="change-path">
          <span>{f.path.slice(slash + 1)}</span>
          {slash > 0 && <span className="dir">{f.path.slice(0, slash)}</span>}
        </span>
        {!readOnly && (
          <span className="row-actions" onClick={(e) => e.stopPropagation()}>
            {!isStaged && (
              <button className="icon-btn" title="Discard changes" aria-label="Discard changes" onClick={() => act('discard', [f.path])}>
                <Undo2 size={13} />
              </button>
            )}
            {isStaged ? (
              <button className="icon-btn" title="Unstage" aria-label="Unstage" onClick={() => act('unstage', [f.path])}>
                <Minus size={13} />
              </button>
            ) : (
              <button className="icon-btn" title="Stage" aria-label="Stage" onClick={() => act('stage', [f.path])}>
                <Plus size={13} />
              </button>
            )}
          </span>
        )}
        <span className={`status-letter s-${letter(f, isStaged)}`}>{letter(f, isStaged)}</span>
      </div>
    )
  }

  const review = reviewState?.review
  return (
    <div className="panel-body">
      <div className="branch">
        <GitBranch size={13} />
        <span className="ellipsis mono">{status?.branch.split('...')[0] ?? ''}</span>
      </div>

      {readOnly ? (
        <p className="pad faint small">Read-only repo. You can review diffs; staging and commits are disabled.</p>
      ) : (
        <div className="commit-box">
          <textarea
            placeholder="Commit message"
            value={message}
            rows={2}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && e.ctrlKey) commit()
            }}
          />
          <button className="btn primary block" disabled={busy || !message.trim() || !status?.files.length} onClick={commit}>
            {staged.length ? `Commit ${staged.length} staged` : 'Commit all'}
          </button>
        </div>
      )}

      {staged.length > 0 && (
        <section>
          <h3 className="section-title">
            Staged <span className="count">{staged.length}</span>
            {!readOnly && <button className="text-btn" onClick={() => act('unstage', staged.map((f) => f.path))}>Unstage all</button>}
          </h3>
          {staged.map((f) => row(f, true))}
        </section>
      )}
      <section>
        <h3 className="section-title">
          Changes <span className="count">{unstaged.length}</span>
          {!readOnly && unstaged.length > 0 && (
            <button className="text-btn" onClick={() => act('stage', unstaged.map((f) => f.path))}>Stage all</button>
          )}
        </h3>
        {unstaged.map((f) => row(f, false))}
        {status && status.files.length === 0 && <p className="pad faint small">Working tree clean.</p>}
      </section>

      <section className="review">
        <h3 className="section-title">
          Agent review
          <button
            className="text-btn"
            disabled={reviewState?.running || !status?.files.length}
            onClick={() => api.startReview(ws).catch(showError)}
          >
            {reviewState?.running ? 'Reviewing' : review ? 'Run again' : 'Request review'}
          </button>
        </h3>
        {reviewState?.running && (
          <div className="progress-line">
            <div className="working"><LoaderCircle size={13} className="spin" /> Reviewing changes</div>
            {reviewState.progress.slice(-3).map((p, i) => (
              <div key={i} className="faint mono ellipsis">{p}</div>
            ))}
          </div>
        )}
        {!review && !reviewState?.running && (
          <p className="pad faint small">The reviewer checks uncommitted changes and marks findings on the diff.</p>
        )}
        {review && !reviewState?.running && (
          <>
            <div className={`verdict ${review.verdict}`}>
              {review.verdict === 'approve' ? <Check size={14} /> : <CircleAlert size={14} />}
              {review.verdict === 'approve' ? 'Approved' : 'Changes requested'}
            </div>
            <p className="review-summary">{review.summary}</p>
            {review.findings.map((f, i) => (
              <FindingRow key={i} finding={f} onClick={() => openFinding(f)} />
            ))}
          </>
        )}
      </section>
    </div>
  )
}
