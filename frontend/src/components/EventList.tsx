import {
  ArrowUpRight, Check, ChevronDown, ChevronRight, CircleAlert, CircleDot, FilePen, FileText, Globe, ListTodo, Pencil,
  Play, Search, Square, Terminal, TriangleAlert, Undo2, Workflow,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api'
import { beforeUserMessage } from '../socket'
import { tabId, useApp } from '../store'
import type { AgentEvent } from '../types'
import { FindingRow } from './ChangesPanel'
import { markdownComponents } from './ExternalLink'

type ToolEvent = Extract<AgentEvent, { type: 'tool' }>

const TOOLS: Record<string, { verb: string; icon: LucideIcon }> = {
  Read: { verb: 'Read', icon: FileText },
  Edit: { verb: 'Edited', icon: FilePen },
  MultiEdit: { verb: 'Edited', icon: FilePen },
  Write: { verb: 'Wrote', icon: FilePen },
  NotebookEdit: { verb: 'Edited', icon: FilePen },
  Bash: { verb: 'Ran', icon: Terminal },
  Grep: { verb: 'Searched', icon: Search },
  Glob: { verb: 'Listed', icon: Search },
  WebSearch: { verb: 'Searched the web', icon: Globe },
  WebFetch: { verb: 'Fetched', icon: Globe },
  TodoWrite: { verb: 'Updated plan', icon: ListTodo },
  Agent: { verb: 'Delegated', icon: Workflow },
  Task: { verb: 'Delegated', icon: Workflow },
  StructuredOutput: { verb: 'Reported findings', icon: Check },
  mcp__workspace__start_task: { verb: 'Started task', icon: Play },
  mcp__workspace__list_tasks: { verb: 'Listed tasks', icon: ListTodo },
}

const PATH_TOOLS = new Set(['Read', 'Edit', 'MultiEdit', 'Write', 'NotebookEdit'])

// Absolute paths are long and repetitive; the last few segments identify the file.
function shortPath(p: string) {
  const parts = p.split(/[\\/]/).filter(Boolean)
  return parts.length > 3 ? parts.slice(-3).join('/') : parts.join('/')
}

function ToolRow({ event }: { event: ToolEvent }) {
  const known = TOOLS[event.name]
  const Icon = known?.icon ?? CircleDot
  const verb = event.subagent ? `Delegated to ${event.subagent}` : (known?.verb ?? event.name.replace(/^mcp__\w+?__/, ''))
  const target = event.subagent ? event.summary : PATH_TOOLS.has(event.name) ? shortPath(event.summary) : event.summary
  return (
    <div className={`tool-row ${event.agent !== 'main' ? 'sub' : ''}`} title={event.summary}>
      <Icon size={13} />
      <span className="verb">{verb}</span>
      {target && <span className="target">{target}</span>}
    </div>
  )
}

// After an agent turn that edited files: what changed, with a one-click undo.
function CheckpointRow({ id, files, restored }: { id: string; files: string[]; restored: boolean }) {
  const undone = useApp((s) => restored || !!s.undone[id])
  const showError = useApp((s) => s.showError)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  async function undo() {
    const list = files.map(shortPath).join('\n')
    if (!confirm(`Restore these files to how they were before this turn?\n\n${list}\n\nAny edits made to them since will be lost.`)) return
    setBusy(true)
    try {
      await api.restoreCheckpoint(id)
      useApp.setState((s) => ({ undone: { ...s.undone, [id]: true } }))
    } catch (e) {
      showError(e)
    } finally {
      setBusy(false)
    }
  }

  const Chevron = open ? ChevronDown : ChevronRight
  return (
    <div className="checkpoint">
      <button className="text-btn" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <Chevron size={12} style={{ verticalAlign: -2 }} /> Changed {files.length} file{files.length === 1 ? '' : 's'}
      </button>
      <div className="spacer" />
      {undone ? (
        <span className="faint small">Undone</span>
      ) : (
        <button className="btn sm" onClick={undo} disabled={busy}>
          <Undo2 size={12} /> Undo
        </button>
      )}
      {open && (
        <div className="checkpoint-files">
          {files.map((f) => (
            <div key={f} className="mono faint ellipsis" title={f}>{shortPath(f)}</div>
          ))}
        </div>
      )}
    </div>
  )
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

// A user message in a conversation. Editing it rewinds the conversation to just before it.
function UserMessage({ text, convId, index }: { text: string; convId: string; index: number }) {
  const busy = useApp((s) => !!s.busyConvs[convId])
  const showError = useApp((s) => s.showError)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(text)
  const [saving, setSaving] = useState(false)

  function start() {
    setDraft(text)
    setEditing(true)
  }

  async function save() {
    const next = draft.trim()
    if (!next || saving || busy) return
    // What the rewind throws away: this message's reply and everything after it.
    const { convEvents, undone } = useApp.getState()
    const all = convEvents[convId] ?? []
    const later = all.slice(beforeUserMessage(all, index).length + 1)
    const messages = later.filter((e) => e.type === 'user' || (e.type === 'text' && e.agent === 'main')).length
    const files = new Set(
      later.flatMap((e) => (e.type === 'checkpoint' && !e.restored && !undone[e.id] ? e.files : [])),
    )
    if (later.length) {
      const ok = confirm(
        'Edit this message and rewind the conversation?\n\n' +
          `This discards ${plural(messages, 'later message')} and undoes the file changes to ` +
          `${plural(files.size, 'file')} made since this message (edits to them since then are lost too). ` +
          'Changes the agent made with shell commands are not undone.',
      )
      if (!ok) return
    }
    // Close right away: the new turn's message takes this message's place as it streams in.
    setSaving(true)
    setEditing(false)
    try {
      await api.editMessage(convId, index, next)
    } catch (e) {
      setEditing(true)
      showError(e)
    } finally {
      setSaving(false)
    }
  }

  if (editing) {
    return (
      <div className="msg-user editing">
        <textarea
          value={draft}
          autoFocus
          rows={Math.min(12, draft.split('\n').length + 1)}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') setEditing(false)
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              save()
            }
          }}
        />
        <div className="edit-actions">
          <button className="btn sm" onClick={() => setEditing(false)} disabled={saving}>Cancel</button>
          <button className="btn sm primary" onClick={save} disabled={saving || busy || !draft.trim()}>Save</button>
        </div>
      </div>
    )
  }
  return (
    <div className="user-turn">
      <div className="msg-user">{text}</div>
      {!busy && (
        <button className="icon-btn edit-btn" onClick={start} title="Edit message" aria-label="Edit message">
          <Pencil size={13} />
        </button>
      )}
    </div>
  )
}

const AGENT_NAMES: Record<string, string> = { subagent: 'Subagent', implementer: 'Implementer', reviewer: 'Reviewer' }

// `edit` makes user messages editable (conversations only, not task logs).
export function EventView({ event, edit }: { event: AgentEvent; edit?: { convId: string; index: number } }) {
  const openTab = useApp((s) => s.openTab)
  switch (event.type) {
    case 'user':
      if (edit) return <UserMessage text={event.text} convId={edit.convId} index={edit.index} />
      return <div className="msg-user">{event.text}</div>
    case 'text':
      if (event.agent === 'pipeline') return <div className="phase">{event.text}</div>
      return (
        <div className={event.agent === 'main' ? '' : 'msg-sub'}>
          {event.agent !== 'main' && <div className="agent-label">{AGENT_NAMES[event.agent] ?? event.agent}</div>}
          <div className="msg-text">
            <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{event.text}</Markdown>
          </div>
        </div>
      )
    case 'tool':
      return <ToolRow event={event} />
    case 'task_link':
      return (
        <div className="event-card">
          <button
            className="link-row"
            onClick={() => openTab({ id: tabId.task(event.task_id), kind: 'task', taskId: event.task_id })}
          >
            <Play size={13} />
            <span className="muted">Task started</span>
            <span className="title ellipsis">{event.title}</span>
            <ArrowUpRight size={13} className="faint" />
          </button>
        </div>
      )
    case 'review':
      return (
        <div className="event-card">
          <div className="agent-label">Reviewer</div>
          <div className={`verdict ${event.review.verdict}`} style={{ margin: 0 }}>
            {event.review.verdict === 'approve' ? <Check size={14} /> : <CircleAlert size={14} />}
            {event.review.verdict === 'approve' ? 'Approved' : 'Changes requested'}
          </div>
          <div className="muted">{event.review.summary}</div>
          {event.review.findings.map((f, i) => (
            <FindingRow key={i} finding={f} />
          ))}
        </div>
      )
    case 'result':
      return (
        <div className={`meta-line ${event.is_error ? 'error' : ''}`}>
          {event.is_error ? 'Ended with an error' : 'Completed'} · {event.num_turns} turns
          {event.cost_usd != null && ` · $${event.cost_usd.toFixed(3)} API-equivalent`}
          {event.errors?.length ? ` · ${event.errors.join('; ')}` : ''}
        </div>
      )
    case 'error':
      return (
        <div className="meta-line error">
          <CircleAlert size={12} style={{ verticalAlign: -2, marginRight: 6 }} />
          {event.message}
        </div>
      )
    case 'warning':
      return (
        <div className="meta-line warning">
          <TriangleAlert size={12} style={{ verticalAlign: -2, marginRight: 6 }} />
          {event.message}
        </div>
      )
    case 'checkpoint':
      return <CheckpointRow id={event.id} files={event.files} restored={!!event.restored} />
    case 'stopped':
      return (
        <div className="meta-line">
          <Square size={10} style={{ verticalAlign: -1, marginRight: 6 }} />
          Stopped
        </div>
      )
    case 'resumed':
    case 'phase':
    case 'timing':
      return <div className="phase">{event.text}</div>
    default:
      return null
  }
}
