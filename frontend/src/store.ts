import { create } from 'zustand'
import { api } from './api'
import type {
  AdminState, AgentEvent, Approval, Conversation, MobileView, Mode, Repo, Review, SideTab, Tab, Task, UsageWindow,
  WorkspaceRef,
} from './types'
import { wsKey } from './types'

const mobileQuery = window.matchMedia('(max-width: 820px)')
mobileQuery.addEventListener('change', (e) => useApp.setState({ isMobile: e.matches }))

type ReviewState = { review: Review | null; running: boolean; progress: string[] }

type State = {
  repos: Repo[]
  workspace: WorkspaceRef | null

  conversations: Conversation[]
  activeConv: string | null
  convEvents: Record<string, AgentEvent[]>
  streaming: Record<string, string>
  busyConvs: Record<string, boolean>

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
  setWorkspace: (ws: WorkspaceRef) => void
  openConversation: (id: string) => Promise<void>
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
    set({ repos, conversations, tasks, busyConvs, workspace: repos[0] ? { repo: repos[0].name, task: null } : null })
    if (conversations[0]) await get().openConversation(conversations[0].id)
  },

  setWorkspace(ws) {
    set({ workspace: ws })
  },

  async openConversation(id) {
    set({ activeConv: id })
    const conv = await api.conversation(id)
    set((s) => ({
      convEvents: { ...s.convEvents, [id]: conv.events },
      busyConvs: { ...s.busyConvs, [id]: !!conv.busy },
    }))
  },

  async newConversation(repo, mode = 'agent') {
    const conv = await api.createConversation(repo, mode)
    set((s) => ({
      conversations: [conv, ...s.conversations],
      activeConv: conv.id,
      convEvents: { ...s.convEvents, [conv.id]: [] },
    }))
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

export const tabId = {
  file: (ws: WorkspaceRef, path: string) => `file:${wsKey(ws)}:${path}`,
  diff: (ws: WorkspaceRef, path: string, staged: boolean) => `diff:${wsKey(ws)}:${staged ? 'staged' : 'work'}:${path}`,
  task: (id: string) => `task:${id}`,
}
