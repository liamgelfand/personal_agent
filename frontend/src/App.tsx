import { Check, CircleAlert, FileCode2, Folder, GitCompare, ListChecks, MessageSquare } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useEffect } from 'react'
import { Group, Panel, Separator } from 'react-resizable-panels'
import { AdminBar } from './components/AdminBar'
import { Approvals } from './components/Approvals'
import { ChangesPanel } from './components/ChangesPanel'
import { ChatPanel } from './components/ChatPanel'
import { EditorArea } from './components/EditorArea'
import { FileTree } from './components/FileTree'
import { QuickOpen } from './components/QuickOpen'
import { SearchPanel } from './components/SearchPanel'
import { ACTIVE_STATUSES, TasksPanel, useTaskNotices } from './components/TasksPanel'
import { TerminalPanel } from './components/TerminalPanel'
import { TopBar } from './components/TopBar'
import { connect } from './socket'
import { useApp } from './store'
import type { MobileView, SideTab } from './types'

const SIDE_TABS: { id: SideTab; label: string }[] = [
  { id: 'files', label: 'Files' },
  { id: 'search', label: 'Search' },
  { id: 'changes', label: 'Changes' },
  { id: 'tasks', label: 'Tasks' },
]

const MOBILE_TABS: { id: MobileView; label: string; icon: LucideIcon }[] = [
  { id: 'chat', label: 'Chat', icon: MessageSquare },
  { id: 'files', label: 'Files', icon: Folder },
  { id: 'editor', label: 'Editor', icon: FileCode2 },
  { id: 'changes', label: 'Changes', icon: GitCompare },
  { id: 'tasks', label: 'Tasks', icon: ListChecks },
]

function useShortcuts() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return
      // Ctrl+` shows or hides the terminal, also while it has focus (the phone layout has none).
      if (e.code === 'Backquote' && e.ctrlKey && !useApp.getState().isMobile) {
        e.preventDefault()
        useApp.getState().toggleTerminal()
        return
      }
      // Ctrl+Tab cycles chat tabs (where the browser lets it through). No Ctrl+W: browsers reserve it to close the window.
      if (e.key === 'Tab' && e.ctrlKey) {
        e.preventDefault()
        useApp.getState().cycleChatTab(e.shiftKey ? -1 : 1).catch(useApp.getState().showError)
        return
      }
      const key = e.key.toLowerCase()
      if (key === 'p' && !e.shiftKey) {
        e.preventDefault()
        useApp.setState({ quickOpen: true })
      } else if (key === 'f' && e.shiftKey) {
        e.preventDefault()
        useApp.setState({ sideTab: 'search', mobileView: 'files' })
      }
    }
    window.addEventListener('keydown', onKey, true) // capture, so it works while Monaco has focus
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])
}

function Sidebar({ tabs }: { tabs: SideTab[] }) {
  const workspace = useApp((s) => s.workspace)
  const side = useApp((s) => s.sideTab)
  const activeTasks = useApp(
    (s) => s.tasks.filter((t) => t.repo === s.workspace?.repo && ACTIVE_STATUSES.has(t.status)).length,
  )
  const notices = useTaskNotices().length
  const current = tabs.includes(side) ? side : tabs[0]
  const wsId = workspace ? `${workspace.repo}@${workspace.task}` : ''

  return (
    <div className="sidebar">
      {tabs.length > 1 && (
        <div className="side-tabs" role="tablist">
          {SIDE_TABS.filter((t) => tabs.includes(t.id)).map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={current === t.id}
              className={`side-tab ${current === t.id ? 'active' : ''}`}
              onClick={() => useApp.setState({ sideTab: t.id })}
            >
              {t.label}
              {t.id === 'tasks' && activeTasks > 0 && <span className="count">{activeTasks}</span>}
              {t.id === 'tasks' && notices > 0 && (
                <span className="badge" title={`${notices} finished task${notices === 1 ? '' : 's'} to look at`}>{notices}</span>
              )}
            </button>
          ))}
        </div>
      )}
      {!workspace ? (
        <p className="pad faint">Add repos to config.yaml.</p>
      ) : current === 'files' ? (
        <FileTree key={wsId} ws={workspace} />
      ) : current === 'search' ? (
        <SearchPanel key={wsId} ws={workspace} />
      ) : current === 'changes' ? (
        <ChangesPanel key={wsId} ws={workspace} />
      ) : (
        <TasksPanel repo={workspace.repo} />
      )}
    </div>
  )
}

function DesktopLayout() {
  return (
    <Group orientation="horizontal" className="main">
      <Panel defaultSize="19" minSize={200}>
        <Sidebar tabs={['files', 'search', 'changes', 'tasks']} />
      </Panel>
      <Separator className="separator" />
      <Panel defaultSize="51" minSize={320}>
        <div className="editor-column">
          <div className="editor-column-main">
            <EditorArea />
          </div>
          <TerminalPanel />
        </div>
      </Panel>
      <Separator className="separator" />
      <Panel defaultSize="30" minSize={320}>
        <ChatPanel />
      </Panel>
    </Group>
  )
}

// Phones get one pane at a time with a bottom tab bar.
function MobileLayout() {
  const view = useApp((s) => s.mobileView)
  const approvals = useApp((s) => s.approvals.length)
  const notices = useTaskNotices().length
  return (
    <>
      <div className="mobile-main">
        {view === 'chat' && <ChatPanel />}
        {view === 'files' && <Sidebar tabs={['files', 'search']} />}
        {view === 'changes' && <Sidebar tabs={['changes']} />}
        {view === 'tasks' && <Sidebar tabs={['tasks']} />}
        {view === 'editor' && <EditorArea />}
      </div>
      <nav className="mobile-nav" aria-label="Views">
        {MOBILE_TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            className={view === id ? 'active' : ''}
            aria-current={view === id ? 'page' : undefined}
            onClick={() => useApp.setState({ mobileView: id, ...(id === 'files' ? { sideTab: 'files' as const } : {}) })}
          >
            <Icon size={18} />
            <span>{label}</span>
            {id === 'chat' && approvals > 0 && <span className="nav-badge" />}
            {id === 'tasks' && notices > 0 && <span className="nav-badge count-badge">{notices}</span>}
          </button>
        ))}
      </nav>
    </>
  )
}

export default function App() {
  const toast = useApp((s) => s.toast)
  const isMobile = useApp((s) => s.isMobile)
  useShortcuts()

  useEffect(() => {
    useApp.getState().init().catch(useApp.getState().showError)
    connect()
  }, [])

  return (
    <div className={`ide ${isMobile ? 'mobile' : ''}`}>
      <TopBar />
      <AdminBar />
      {isMobile ? <MobileLayout /> : <DesktopLayout />}
      <QuickOpen />
      <Approvals />
      {toast && (
        <div className={`toast ${toast.kind}`} role="status">
          {toast.kind === 'error' ? <CircleAlert size={15} /> : <Check size={15} />}
          {toast.text}
        </div>
      )}
    </div>
  )
}
