import Editor, { DiffEditor } from '@monaco-editor/react'
import { ChevronDown, ChevronRight, Columns2, GitBranch, LoaderCircle, Rows2, X } from 'lucide-react'
import type { editor } from 'monaco-editor'
import * as monaco from 'monaco-editor'
import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { FileContent } from '../api'
import { EDITOR_FONT, monacoTheme } from '../monaco'
import { useApp } from '../store'
import { wsKey } from '../types'
import type { Finding, Tab, WorkspaceRef } from '../types'
import { FindingRow } from './ChangesPanel'
import { EventView } from './EventList'
import { ACTIVE_STATUSES, StatusDot } from './TasksPanel'

const EDITOR_OPTIONS: editor.IStandaloneEditorConstructionOptions = {
  ...EDITOR_FONT,
  minimap: { enabled: true, renderCharacters: false, scale: 1 },
  scrollBeyondLastLine: false,
  automaticLayout: true,
  glyphMargin: true,
  renderLineHighlight: 'line',
  padding: { top: 8 },
  scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10, useShadows: false },
  overviewRulerBorder: false,
  guides: { indentation: true },
}

function useWritable(ws: WorkspaceRef) {
  const access = useApp((s) => s.repos.find((r) => r.name === ws.repo)?.access)
  return !!ws.task || access === 'write'
}

function TabLabel({ tab, title }: { tab: Tab; title: (id: string) => string }) {
  if (tab.kind === 'task') {
    return (
      <>
        <span className="kind">Task</span>
        <span>{title(tab.taskId)}</span>
      </>
    )
  }
  return (
    <>
      <span>{tab.path.split('/').pop()}</span>
      {tab.kind === 'diff' && <span className="kind">{tab.staged ? 'Staged diff' : 'Diff'}</span>}
      {tab.ws.task && <span className="kind">Task</span>}
    </>
  )
}

export function EditorArea() {
  const tabs = useApp((s) => s.tabs)
  const activeTab = useApp((s) => s.activeTab)
  const dirty = useApp((s) => s.dirtyTabs)
  const tasks = useApp((s) => s.tasks)
  const closeTab = useApp((s) => s.closeTab)
  const title = (id: string) => tasks.find((t) => t.id === id)?.title ?? id

  const close = (tab: Tab) => {
    if (dirty[tab.id] && !confirm('Close without saving?')) return
    closeTab(tab.id)
  }

  return (
    <div className="editor-area">
      <div className="tabs no-scrollbar" role="tablist">
        {tabs.map((tab) => (
          <div
            key={tab.id}
            role="tab"
            aria-selected={tab.id === activeTab}
            className={`tab ${tab.id === activeTab ? 'active' : ''}`}
            onClick={() => useApp.setState({ activeTab: tab.id })}
            onMouseDown={(e) => e.button === 1 && (e.preventDefault(), close(tab))}
            title={tab.kind === 'task' ? title(tab.taskId) : tab.path}
          >
            <TabLabel tab={tab} title={title} />
            <button className="tab-close" aria-label="Close tab" onClick={(e) => (e.stopPropagation(), close(tab))}>
              {dirty[tab.id] ? <span className="dirty-dot" /> : <X size={13} />}
            </button>
          </div>
        ))}
      </div>
      <div className="tab-content">
        {tabs.length === 0 && (
          <div className="empty-editor">
            <div>
              <h2>No file open</h2>
              <p>Open a file from Files, or a diff from Changes.</p>
            </div>
          </div>
        )}
        {tabs.map((tab) => (
          <div key={tab.id} className="tab-pane" hidden={tab.id !== activeTab}>
            {tab.kind === 'file' && <FileEditor tab={tab} />}
            {tab.kind === 'diff' && <DiffView tab={tab} />}
            {tab.kind === 'task' && <TaskLog taskId={tab.taskId} />}
          </div>
        ))}
      </div>
    </div>
  )
}

function Loading() {
  return (
    <div className="pad working">
      <LoaderCircle size={13} className="spin" /> Loading
    </div>
  )
}

function FileEditor({ tab }: { tab: Extract<Tab, { kind: 'file' }> }) {
  const key = wsKey(tab.ws)
  const version = useApp((s) => s.fsVersion[key] ?? 0)
  const isDirty = useApp((s) => !!s.dirtyTabs[tab.id])
  const setDirty = useApp((s) => s.setDirty)
  const showError = useApp((s) => s.showError)
  const writable = useWritable(tab.ws)
  const [file, setFile] = useState<FileContent | null>(null)
  const [error, setError] = useState<string | null>(null)
  const saved = useRef('')
  const current = useRef('')
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null)

  const reveal = (ed: editor.IStandaloneCodeEditor | null, line?: number) => {
    if (!ed || !line) return
    ed.revealLineInCenter(line)
    ed.setPosition({ lineNumber: line, column: 1 })
    ed.focus()
  }

  // Search results reopen the same tab with a new line; jump to it.
  useEffect(() => reveal(editorRef.current, tab.line), [tab.line])

  useEffect(() => {
    if (isDirty) return // don't clobber unsaved edits when the agent touches the file
    api.file(tab.ws, tab.path).then(
      (f) => {
        saved.current = current.current = f.content
        setFile(f)
        setError(null)
      },
      (e) => setError(e.message),
    )
  }, [key, tab.path, version]) // eslint-disable-line react-hooks/exhaustive-deps

  async function save() {
    try {
      await api.saveFile(tab.ws, tab.path, current.current)
      saved.current = current.current
      setDirty(tab.id, false)
    } catch (e) {
      showError(e)
    }
  }
  const saveRef = useRef(save)
  saveRef.current = save

  if (error) return <p className="pad error-text">{error}</p>
  if (!file) return <Loading />
  if (file.binary) return <p className="pad faint">Binary file, {file.size.toLocaleString()} bytes.</p>
  if (file.too_large) return <p className="pad faint">File is too large to open ({file.size.toLocaleString()} bytes).</p>

  return (
    <div className="editor-wrap">
      {!writable && <span className="tag editor-note">Read only</span>}
      <Editor
        path={`${key}/${tab.path}`}
        value={file.content}
        theme={monacoTheme()}
        options={{ ...EDITOR_OPTIONS, readOnly: !writable }}
        loading={<Loading />}
        onChange={(value) => {
          current.current = value ?? ''
          setDirty(tab.id, current.current !== saved.current)
        }}
        onMount={(ed) => {
          editorRef.current = ed
          ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current())
          reveal(ed, tab.line)
        }}
      />
    </div>
  )
}

function findingDecorations(findings: Finding[]): editor.IModelDeltaDecoration[] {
  return findings.map((f) => ({
    range: new monaco.Range(f.line, 1, f.line, 1),
    options: {
      isWholeLine: true,
      className: `finding-line sev-${f.severity}`,
      glyphMarginClassName: `finding-glyph sev-${f.severity}`,
      hoverMessage: { value: `**${f.title}**\n\n${f.detail}` },
      glyphMarginHoverMessage: { value: `**${f.title}**\n\n${f.detail}` },
    },
  }))
}

function DiffView({ tab }: { tab: Extract<Tab, { kind: 'diff' }> }) {
  const key = wsKey(tab.ws)
  const version = useApp((s) => s.fsVersion[key] ?? 0)
  const review = useApp((s) => s.reviews[key]?.review)
  const [sides, setSides] = useState<{ original: string; modified: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [inline, setInline] = useState(false)
  const diffEditor = useRef<editor.IStandaloneDiffEditor | null>(null)
  const decorations = useRef<editor.IEditorDecorationsCollection | null>(null)
  const findings = review?.findings.filter((f) => f.file === tab.path) ?? []

  useEffect(() => {
    api.diff(tab.ws, tab.path, tab.staged).then(setSides, (e) => setError(e.message))
  }, [key, tab.path, tab.staged, version]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const modified = diffEditor.current?.getModifiedEditor()
    if (!modified) return
    decorations.current?.clear()
    decorations.current = modified.createDecorationsCollection(findingDecorations(findings))
  }, [sides, review]) // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <p className="pad error-text">{error}</p>
  if (!sides) return <Loading />

  return (
    <div className="diff-wrap">
      <div className="pane-header">
        <span className="mono ellipsis">{tab.path}</span>
        <span className="faint">{tab.staged ? 'HEAD to staged' : 'Last commit to working tree'}</span>
        <div className="spacer" />
        <div className="segmented" role="group" aria-label="Diff layout">
          <button className={!inline ? 'on' : ''} onClick={() => setInline(false)} title="Side by side" aria-label="Side by side">
            <Columns2 size={13} />
          </button>
          <button className={inline ? 'on' : ''} onClick={() => setInline(true)} title="Inline" aria-label="Inline">
            <Rows2 size={13} />
          </button>
        </div>
      </div>
      {findings.length > 0 && (
        <div className="diff-findings">
          {findings.map((f, i) => (
            <FindingRow
              key={i}
              finding={f}
              showDetail
              onClick={() => diffEditor.current?.getModifiedEditor().revealLineInCenter(f.line)}
            />
          ))}
        </div>
      )}
      <div className="diff-editor">
        <DiffEditor
          original={sides.original}
          modified={sides.modified}
          originalModelPath={`diff-orig/${key}/${tab.staged}/${tab.path}`}
          modifiedModelPath={`diff-mod/${key}/${tab.staged}/${tab.path}`}
          theme={monacoTheme()}
          loading={<Loading />}
          options={{
            ...EDITOR_OPTIONS,
            readOnly: true,
            renderSideBySide: !inline,
            useInlineViewWhenSpaceIsLimited: false,
            originalEditable: false,
            renderOverviewRuler: false,
          }}
          onMount={(ed) => {
            diffEditor.current = ed
            decorations.current = ed.getModifiedEditor().createDecorationsCollection(findingDecorations(findings))
            if (tab.line) setTimeout(() => ed.getModifiedEditor().revealLineInCenter(tab.line!), 300)
          }}
        />
      </div>
    </div>
  )
}

function TaskLog({ taskId }: { taskId: string }) {
  const task = useApp((s) => s.tasks.find((t) => t.id === taskId))
  const events = useApp((s) => s.taskEvents[taskId])
  const setWorkspace = useApp((s) => s.setWorkspace)
  const [showSpec, setShowSpec] = useState(false)

  useEffect(() => {
    api.task(taskId).then((t) => useApp.setState((s) => ({ taskEvents: { ...s.taskEvents, [taskId]: t.events } })))
  }, [taskId])

  if (!task) return <p className="pad faint">Task not found.</p>
  const Chevron = showSpec ? ChevronDown : ChevronRight
  return (
    <div className="task-log">
      <div className="task-log-head">
        <h2>{task.title}</h2>
        <div className="task-meta">
          <StatusDot status={task.status} />
          <GitBranch size={12} />
          <span className="mono">{task.branch}</span>
          {task.review_rounds > 0 && <span>· {task.review_rounds} review round{task.review_rounds > 1 ? 's' : ''}</span>}
        </div>
        <div>
          <button className="spec-toggle" onClick={() => setShowSpec((v) => !v)} aria-expanded={showSpec}>
            <Chevron size={12} style={{ verticalAlign: -2 }} /> Specification
          </button>
          {showSpec && <pre className="spec">{task.spec}</pre>}
        </div>
        {task.status !== 'discarded' && (
          <button className="btn sm" onClick={() => setWorkspace({ repo: task.repo, task: task.id })}>
            Open workspace to review the diff
          </button>
        )}
      </div>
      <div className="transcript">
        {(events ?? []).map((e, i) => (
          <EventView key={i} event={e} />
        ))}
        {ACTIVE_STATUSES.has(task.status) && (
          <div className="working">
            <LoaderCircle size={13} className="spin" /> {task.status}
          </div>
        )}
      </div>
    </div>
  )
}
