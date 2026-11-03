// One WebSocket for all live events (see src/personal_agent/server.py). Reconnects on drop.
import { notify } from './notify'
import { useApp } from './store'
import type { AdminState, AgentEvent, Approval, Conversation, Review, Task } from './types'

type ServerEvent = { type: string; [key: string]: unknown }

let socket: WebSocket | null = null
let retry = 0

const PERSISTED = new Set([
  'user', 'text', 'tool', 'result', 'task_link', 'review', 'error', 'warning', 'stopped', 'resumed', 'checkpoint',
])

function append(map: Record<string, AgentEvent[]>, key: string, event: AgentEvent) {
  return { ...map, [key]: [...(map[key] ?? []), event] }
}

// The events before the index-th user message (what remains after that message is edited).
export function beforeUserMessage(events: AgentEvent[], index: number) {
  let seen = -1
  const cut = events.findIndex((e) => e.type === 'user' && ++seen === index)
  return cut < 0 ? events : events.slice(0, cut)
}

function handle(e: ServerEvent) {
  const set = useApp.setState
  const s = useApp.getState()
  const conv = e.conversation_id as string | undefined
  const task = e.task_id as string | undefined

  switch (e.type) {
    case 'delta':
      if (conv) set({ streaming: { ...s.streaming, [conv]: (s.streaming[conv] ?? '') + (e.text as string) } })
      return
    case 'chat_status':
      if (conv) {
        const streaming = { ...s.streaming }
        delete streaming[conv]
        set({ busyConvs: { ...s.busyConvs, [conv]: e.busy as boolean }, streaming })
        if (!e.busy && s.busyConvs[conv]) {
          notify('Agent replied', s.conversations.find((c) => c.id === conv)?.title ?? 'Chat', conv)
        }
      }
      return
    case 'conversation_updated': {
      const c = e.conversation as Conversation
      const exists = s.conversations.some((x) => x.id === c.id)
      set({
        conversations: exists
          ? s.conversations.map((x) => (x.id === c.id ? c : x)).sort((a, b) => b.updated_at - a.updated_at)
          : [c, ...s.conversations],
      })
      return
    }
    case 'conversation_deleted':
      set({
        conversations: s.conversations.filter((c) => c.id !== e.id),
        activeConv: s.activeConv === e.id ? null : s.activeConv,
      })
      return
    case 'task_updated': {
      const t = e.task as Task
      const exists = s.tasks.some((x) => x.id === t.id)
      const before = s.tasks.find((x) => x.id === t.id)?.status
      if (before !== t.status && (t.status === 'ready' || t.status === 'failed')) {
        notify(t.status === 'ready' ? 'Task ready for review' : 'Task failed', t.title, t.id)
      }
      set({ tasks: exists ? s.tasks.map((x) => (x.id === t.id ? t : x)) : [t, ...s.tasks] })
      return
    }
    case 'rate_limit':
      set({ windows: e.windows as typeof s.windows })
      return
    case 'approval_request':
      if (!s.approvals.some((a) => a.id === e.id)) {
        set({ approvals: [...s.approvals, e as unknown as Approval] })
        notify('Approval needed', `The agent wants to ${e.reason}.`, e.id as string)
      }
      return
    case 'approval_resolved':
      set({ approvals: s.approvals.filter((a) => a.id !== e.id) })
      return
    case 'fs_changed': {
      const key = e.task ? `${e.repo}@${e.task}` : (e.repo as string)
      set({ fsVersion: { ...s.fsVersion, [key]: (s.fsVersion[key] ?? 0) + 1 } })
      return
    }
    case 'hello': {
      // A new boot id after a reconnect means the server restarted, possibly with new code.
      const admin = e as unknown as AdminState
      if (s.admin && s.admin.boot_id !== admin.boot_id) {
        location.reload()
        return
      }
      // Events may have been missed while disconnected (e.g. a phone asleep). Pending approvals are
      // re-sent right after this message, so start that list fresh and re-fetch everything else.
      set({ admin, restarting: false, approvals: [], streaming: {} })
      if (s.admin) s.resync().catch(() => {})
      return
    }
    case 'admin_status':
      set({ admin: e as unknown as AdminState })
      return
    case 'admin_log':
      set({ adminLog: [...s.adminLog.slice(-200), e.line as string] })
      return
    case 'admin_build_done':
      set({ admin: e as unknown as AdminState })
      if (!e.ok) s.showError('UI build failed. See the build log.')
      return
    case 'admin_restarting':
      set({ restarting: true })
      return
    case 'conversation_rewound':
      // A message was edited: it and everything after it are gone; the new turn streams in next.
      if (conv && s.convEvents[conv]) {
        set({ convEvents: { ...s.convEvents, [conv]: beforeUserMessage(s.convEvents[conv], e.index as number) } })
      }
      return
    case 'checkpoint_restored':
      set({ undone: { ...s.undone, [e.id as string]: true } })
      return
    case 'review_started':
    case 'review_progress':
    case 'review':
    case 'review_failed': {
      const key = e.workspace as string | undefined
      if (!key) break // a task's own copy of the review event, handled below
      const prev = s.reviews[key] ?? { review: null, running: false, progress: [] }
      const next =
        e.type === 'review_started' ? { ...prev, running: true, progress: [] }
        : e.type === 'review_progress' ? { ...prev, progress: [...prev.progress, `${e.name} ${e.summary}`] }
        : e.type === 'review' ? { review: e.review as Review, running: false, progress: [] }
        : { ...prev, running: false }
      set({ reviews: { ...s.reviews, [key]: next } })
      if (e.type === 'review_failed') s.showError(`Review failed: ${e.message}`)
      return
    }
  }

  if (!PERSISTED.has(e.type)) return
  const event = e as unknown as AgentEvent
  if (conv) {
    const streaming = { ...s.streaming }
    if (e.type === 'text') delete streaming[conv] // the final text replaces the streamed preview
    set({ convEvents: append(s.convEvents, conv, event), streaming })
  } else if (task) {
    set({ taskEvents: append(s.taskEvents, task, event) })
  }
}

export function connect() {
  if (socket) return // StrictMode runs effects twice in dev
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  socket = new WebSocket(`${proto}://${location.host}/api/ws`)
  socket.onopen = () => {
    retry = 0
  }
  socket.onmessage = (msg) => handle(JSON.parse(msg.data))
  socket.onclose = () => {
    socket = null
    setTimeout(connect, Math.min(10_000, 500 * 2 ** retry++))
  }
}

export function send(message: Record<string, unknown>): boolean {
  if (socket?.readyState !== WebSocket.OPEN) return false
  socket.send(JSON.stringify(message))
  return true
}
