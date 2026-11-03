import { create } from 'zustand'
import { api, ApiError } from './api'
import * as chatTabList from './chatTabs'
import type {
  AdminState, AgentEvent, Approval, Conversation, MobileView, Mode, Repo, Review, SideTab, Tab, Task, UsageWindow,
  WorkspaceRef,
} from './types'
import { wsKey } from './types'

const mobileQuery = window.matchMedia('(max-width: 820px)')
mobileQuery.addEventListener('change', (e) => useApp.setState({ isMobile: e.matches }))

const CHAT_TABS_KEY = 'chatTabs'

function loadChatTabs(): { ids?: unknown; active?: unknown } {
  try {
    return JSON.parse(localStorage.getItem(CHAT_TABS_KEY) ?? '{}') ?? {}
  } catch {
    return {}
  }
}

// Conversations whose full event history has been fetched. After that the socket keeps them current,
// so switching back to an open tab must not refetch (a refetch can drop events arriving meanwhile).
const loadedConvs = new Set<string>()

function cacheConvs(cache: Record<string, Conversation>, convs: Conversation[]) {
  return convs.length ? { ...cache, ...Object.fromEntries(convs.map((c) => [c.id, c])) } : cache
}

// The conversation list is capped and History search filters it, so a tab's conversation may be missing
// from it. Ask the server about those ones; only a 404 means it is gone.
async function liveChatTabs(ids: string[], conversations: Conversation[]) {
  const listed = new Set(conversations.map((c) => c.id))
  const found: Conversation[] = []
  const gone: string[] = []
  await Promise.all(
    ids.filter((id) => !listed.has(id)).map((id) =>
      api.conversation(id).then(
        (c) => void found.push(c),
        (err) => void (err instanceof ApiError && err.status === 404 && gone.push(id)),
      ),
    ),
  )
  return { gone, found }
}

type ReviewState = { review: Review | null; running: boolean; progress: string[] }

type State = {
  repos: Repo[]
  workspace: WorkspaceRef | null

  conversations: Conversation[]
  activeConv: string | null
  convEvents: Record<string, AgentEvent[]>
  streaming: Record<string, string>
  busyConvs: Record<string, boolean>
  chatTabs: string[]
  unseenConvs: Record<string, boolean>
  // Every conversation seen, by id. Tabs and the open chat read from here, because `conversations`
  // is capped and History search filters it.
  convCache: Record<string, Conversation>

  tasks: Task[]
  taskEvents: Record<string, AgentEvent[]>

  approvals: Approval[]
  windows: Record<string, UsageWindow>
  fsVersion: Record<string, number>
  reviews: Record<string, ReviewState>

  tabs: Tab[]
  activeTab: string | null
  dirtyTabs: Record<string, boolean>
  toast: { text: string; kind: 'error' | 'info' } | null

  sideTab: SideTab
  isMobile: boolean
  mobileView: MobileView
  quickOpen: boolean
  admin: AdminState | null
  adminLog: string[]
  restarting: boolean
  undone: Record<string, boolean>

  init: () => Promise<void>
  resync: () => Promise<void>
  setWorkspace: (ws: WorkspaceRef) => void
  openConversation: (id: string, refetch?: boolean) => Promise<void>
  closeChatTab: (id: string) => Promise<void>
  cycleChatTab: (step: number) => Promise<void>
  newConversation: (repo: string | null, mode?: Mode) => Promise<void>
  refreshConversations: (q?: string) => Promise<void>
  refreshTasks: () => Promise<void>
  openTab: (tab: Tab) => void
  closeTab: (id: string) => void
  setDirty: (id: string, dirty: boolean) => void
  loadReview: (ws: WorkspaceRef) => Promise<void>
  showError: (err: unknown) => void
  showInfo: (text: string) => void
}

export const useApp = create<State>((set, get) => ({
  repos: [],
  workspace: null,
  conversations: [],
  activeConv: null,
  convEvents: {},
  streaming: {},
  busyConvs: {},
  chatTabs: [],
  unseenConvs: {},
  convCache: {},
  tasks: [],
  taskEvents: {},
  approvals: [],
  windows: {},
  fsVersion: {},
  reviews: {},
  tabs: [],
  activeTab: null,
  dirtyTabs: {},
  toast: null,

  sideTab: 'files',
  isMobile: mobileQuery.matches,
  mobileView: 'chat',
  quickOpen: false,
  admin: null,
  adminLog: [],
  restarting: false,
  undone: {},

  async init() {
    const [repos, conversations, tasks] = await Promise.all([api.repos(), api.conversations(), api.tasks()])
    const busyConvs = Object.fromEntries(conversations.filter((c) => c.busy).map((c) => [c.id, true]))
    const saved = loadChatTabs()
    const ids = chatTabList.pruneChatTabs(saved.ids)
    const { gone, found } = await liveChatTabs(ids, conversations)
    const chatTabs = ids.filter((id) => !gone.includes(id))
    set((s) => ({
      repos, conversations, tasks, busyConvs, chatTabs,
      convCache: cacheConvs(s.convCache, found),
      workspace: repos[0] ? { repo: repos[0].name, task: null } : null,
    }))
    const first = chatTabs.includes(saved.active as string) ? (saved.active as string) : (chatTabs[0] ?? conversations[0]?.id)
    if (first) await get().openConversation(first)
  },

  async resync() {
    const [conversations, tasks] = await Promise.all([api.conversations(), api.tasks()])
    const busyConvs = Object.fromEntries(conversations.map((c) => [c.id, !!c.busy]))
    // Conversations deleted while disconnected lose their tabs, as a missed conversation_deleted would have done.
    const { gone, found } = await liveChatTabs(get().chatTabs, conversations)
    const before = get().activeConv
    const next = chatTabList.removeChatTabs({ ids: get().chatTabs, active: before }, gone)
    set((s) => ({ conversations, tasks, busyConvs, chatTabs: next.ids, activeConv: next.active, convCache: cacheConvs(s.convCache, found) }))
    // Events may have been missed while disconnected, so every loaded conversation is stale.
    loadedConvs.clear()
    if (next.active) await get().openConversation(next.active, true)
  },

  setWorkspace(ws) {
    set({ workspace: ws })
  },

  async openConversation(id, refetch = false) {
    set((s) => {
      const unseenConvs = { ...s.unseenConvs }
      delete unseenConvs[id]
      return { activeConv: id, chatTabs: chatTabList.addChatTab(s.chatTabs, id), unseenConvs }
    })
    if (loadedConvs.has(id) && !refetch) return
    const { events, ...conv } = await api.conversation(id)
    loadedConvs.add(id)
    set((s) => ({
      convCache: cacheConvs(s.convCache, [conv]),
      convEvents: { ...s.convEvents, [id]: events },
      busyConvs: { ...s.busyConvs, [id]: !!conv.busy },
    }))
  },

  async newConversation(repo, mode = 'agent') {
    const conv = await api.createConversation(repo, mode)
    loadedConvs.add(conv.id)
    set((s) => ({
      conversations: s.conversations.some((c) => c.id === conv.id) ? s.conversations : [conv, ...s.conversations],
      activeConv: conv.id,
      chatTabs: chatTabList.addChatTab(s.chatTabs, conv.id),
      convEvents: { ...s.convEvents, [conv.id]: [] },
    }))
  },

  async closeChatTab(id) {
    const s = get()
    const next = chatTabList.closeChatTab({ ids: s.chatTabs, active: s.activeConv }, id)
    set({ chatTabs: next.ids })
    if (next.active === s.activeConv) return
    if (next.active) await get().openConversation(next.active)
    else set({ activeConv: null })
  },

  async cycleChatTab(step) {
    const s = get()
    const next = chatTabList.cycleChatTab({ ids: s.chatTabs, active: s.activeConv }, step)
    if (next && next !== s.activeConv) await get().openConversation(next)
  },

  async refreshConversations(q) {
    set({ conversations: await api.conversations(q) })
  },

  async refreshTasks() {
    set({ tasks: await api.tasks() })
  },

  openTab(tab) {
    set((s) => {
      const existing = s.tabs.findIndex((t) => t.id === tab.id)
      const tabs = existing >= 0 ? s.tabs.map((t, i) => (i === existing ? tab : t)) : [...s.tabs, tab]
      // On a phone only one pane shows at a time, so opening something jumps to the editor.
      return { tabs, activeTab: tab.id, ...(s.isMobile ? { mobileView: 'editor' as const } : {}) }
    })
  },

  closeTab(id) {
    set((s) => {
      const index = s.tabs.findIndex((t) => t.id === id)
      const tabs = s.tabs.filter((t) => t.id !== id)
      const activeTab = s.activeTab === id ? (tabs[Math.max(0, index - 1)]?.id ?? null) : s.activeTab
      const dirtyTabs = { ...s.dirtyTabs }
      delete dirtyTabs[id]
      return { tabs, activeTab, dirtyTabs }
    })
  },

  setDirty(id, dirty) {
    set((s) => ({ dirtyTabs: { ...s.dirtyTabs, [id]: dirty } }))
  },

  async loadReview(ws) {
    const { review, running } = await api.review(ws)
    const key = wsKey(ws)
    set((s) => ({ reviews: { ...s.reviews, [key]: { review, running, progress: s.reviews[key]?.progress ?? [] } } }))
  },

  showError(err) {
    set({ toast: { text: err instanceof Error ? err.message : String(err), kind: 'error' } })
    setTimeout(() => set({ toast: null }), 6000)
  },

  showInfo(text) {
    set({ toast: { text, kind: 'info' } })
    setTimeout(() => set({ toast: null }), 4000)
  },
}))

useApp.subscribe((s, prev) => {
  // Covers init, refreshes and conversation_updated (which adds unlisted conversations to the list).
  if (s.conversations !== prev.conversations && s.conversations.some((c) => s.convCache[c.id] !== c)) {
    useApp.setState({ convCache: cacheConvs(s.convCache, s.conversations) })
  }
  if (s.chatTabs !== prev.chatTabs || s.activeConv !== prev.activeConv) {
    localStorage.setItem(CHAT_TABS_KEY, JSON.stringify({ ids: s.chatTabs, active: s.activeConv }))
  }
})

export const tabId = {
  file: (ws: WorkspaceRef, path: string) => `file:${wsKey(ws)}:${path}`,
  diff: (ws: WorkspaceRef, path: string, staged: boolean) => `diff:${wsKey(ws)}:${staged ? 'staged' : 'work'}:${path}`,
  task: (id: string) => `task:${id}`,
}
