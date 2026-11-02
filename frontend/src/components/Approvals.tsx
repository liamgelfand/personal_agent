import { ShieldAlert } from 'lucide-react'
import { send } from '../socket'
import { useApp } from '../store'
import type { Approval } from '../types'

// Agents pause on risky commands (push, commit, installs, ...) until you decide here.
export function Approvals() {
  const approvals = useApp((s) => s.approvals)
  const tasks = useApp((s) => s.tasks)
  const conversations = useApp((s) => s.conversations)

  if (approvals.length === 0) return null
  const answer = (id: string, allow: boolean) => send({ type: 'approve', id, allow })
  const origin = (a: Approval) => {
    if (a.task_id) return `Task: ${tasks.find((t) => t.id === a.task_id)?.title ?? a.task_id}`
    if (a.workspace) return `Review of ${a.workspace}`
    return `Chat: ${conversations.find((c) => c.id === a.conversation_id)?.title ?? ''}`
  }

  return (
    <div className="approvals" role="alertdialog" aria-label="Approval required">
      {approvals.map((a) => (
        <div key={a.id} className="approval">
          <div className="approval-head">
            <ShieldAlert size={15} />
            Approval required
          </div>
          <div className="approval-body">
            <div className="faint small ellipsis">{origin(a)}</div>
            <div>The agent wants to {a.reason}.</div>
            <pre className="approval-cmd">{a.summary || a.tool}</pre>
          </div>
          <div className="approval-actions">
            <button className="btn" onClick={() => answer(a.id, false)}>Deny</button>
            <button className="btn primary" onClick={() => answer(a.id, true)}>Allow</button>
          </div>
        </div>
      ))}
    </div>
  )
}
