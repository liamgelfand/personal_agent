import { ChevronDown, ChevronRight, ChevronsDownUp, FilePlus, FolderPlus, RefreshCw } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent, MouseEvent, ReactNode } from 'react'
import { api } from '../api'
import { tabId, useApp } from '../store'
import { wsKey } from '../types'
import type { WorkspaceRef } from '../types'

type Node = { name: string; path: string; children: Map<string, Node> | null }
type Entry = { path: string; isDir: boolean }
// The inline name box: a new entry inside `parent` ('' is the root), or renaming `entry`.
type Editing = { kind: 'new'; parent: string; isDir: boolean } | { kind: 'rename'; entry: Entry }
type Menu = { x: number; y: number; entry: Entry | null }

function buildTree(files: string[], dirs: string[] = []): Node {
  const root: Node = { name: '', path: '', children: new Map() }
  const add = (path: string, isDir: boolean) => {
    let node = root
    const parts = path.split('/')
    parts.forEach((part, i) => {
      const isFile = !isDir && i === parts.length - 1
      let child = node.children!.get(part)
      if (!child) {
        child = { name: part, path: parts.slice(0, i + 1).join('/'), children: isFile ? null : new Map() }
        node.children!.set(part, child)
      }
      node = child
    })
  }
  for (const file of files) add(file, false)
  for (const dir of dirs) add(dir, true)
  return root
}

function find(root: Node, path: string): Node | null {
  if (!path) return root
  let node: Node | undefined = root
  for (const part of path.split('/')) {
    node = node?.children?.get(part)
    if (!node) return null
  }
  return node
}

function sorted(node: Node): Node[] {
  return [...(node.children?.values() ?? [])].sort((a, b) =>
    !!a.children === !!b.children ? a.name.localeCompare(b.name) : a.children ? -1 : 1,
  )
}

const parentOf = (path: string) => path.slice(0, Math.max(0, path.lastIndexOf('/')))
const join = (parent: string, name: string) => (parent ? `${parent}/${name}` : name)

// Why `name` can't be used in `parent`, or null. `self` is the entry being renamed, which may keep its name.
function nameError(root: Node, parent: string, name: string, self?: string): string | null {
  if (!name) return 'A name is required.'
  if (/[\\/]/.test(name)) return 'A name cannot contain / or \\.'
  if (name === '.' || name === '..') return `"${name}" is not a valid name.`
  const lower = join(parent, name).toLowerCase() // the filesystem may ignore case
  const clash = [...(find(root, parent)?.children?.values() ?? [])].some(
    (c) => c.path !== self && c.path.toLowerCase() === lower,
  )
  return clash ? `"${name}" already exists here.` : null
}

function NameInput({ initial, depth, isDir, validate, onSubmit, onCancel }: {
  initial: string
  depth: number
  isDir: boolean
  validate: (name: string) => string | null
  onSubmit: (name: string) => Promise<void>
  onCancel: () => void
}) {
  const [value, setValue] = useState(initial)
  const [error, setError] = useState<string | null>(null)
  const busy = useRef(false)
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const el = input.current!
    el.focus()
    const dot = isDir ? -1 : initial.lastIndexOf('.')
    el.setSelectionRange(0, dot > 0 ? dot : initial.length) // the name without its extension, like VS Code
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  async function submit() {
    const name = value.trim()
    if (name === initial && initial) return onCancel()
    const problem = validate(name)
    setError(problem)
    if (problem) return
    busy.current = true
    try {
      await onSubmit(name)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      input.current?.focus()
    } finally {
      busy.current = false
    }
  }

  const shown = error ?? (value.trim() ? validate(value.trim()) : null)
  return (
    <div className="tree-edit" style={{ paddingLeft: 6 + depth * 14 + (isDir ? 0 : 18) }}>
      {isDir && <ChevronRight size={14} className="chev" />}
      <input
        ref={input}
        value={value}
        aria-label={isDir ? 'Folder name' : 'File name'}
        aria-invalid={!!shown}
        className={shown ? 'invalid' : ''}
        onChange={(e) => {
          setValue(e.target.value)
          setError(null)
        }}
        onKeyDown={(e) => {
          e.stopPropagation()
          if (e.key === 'Enter') submit()
          else if (e.key === 'Escape') onCancel()
        }}
        onBlur={() => !busy.current && onCancel()}
      />
      {shown && <div className="tree-edit-error" role="alert">{shown}</div>}
    </div>
  )
}

export function FileTree({ ws }: { ws: WorkspaceRef }) {
  const key = wsKey(ws)
  const version = useApp((s) => s.fsVersion[key] ?? 0)
  const activeTab = useApp((s) => s.activeTab)
  const openTab = useApp((s) => s.openTab)
  const moveTabs = useApp((s) => s.moveTabs)
  const showError = useApp((s) => s.showError)
  const [files, setFiles] = useState<string[]>([])
  const [dirs, setDirs] = useState<string[]>([])
  const [writable, setWritable] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [filter, setFilter] = useState('')
  const [reload, setReload] = useState(0)
  const [selected, setSelected] = useState<Entry | null>(null)
  const [editing, setEditing] = useState<Editing | null>(null)
  const [menu, setMenu] = useState<Menu | null>(null)
  const treeRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    api.tree(ws).then(
      (t) => !cancelled && (setFiles(t.files), setDirs(t.dirs), setWritable(t.access === 'write'), setError(null)),
      (e) => !cancelled && setError(String(e.message ?? e)),
    )
    return () => {
      cancelled = true
    }
  }, [key, version, reload]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!menu) return
    const close = () => setMenu(null)
    const onKey = (e: globalThis.KeyboardEvent) => e.key === 'Escape' && close()
    window.addEventListener('pointerdown', close)
    window.addEventListener('blur', close)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('pointerdown', close)
      window.removeEventListener('blur', close)
      window.removeEventListener('keydown', onKey)
    }
  }, [menu])

  const shown = useMemo(
    () => (filter ? files.filter((f) => f.toLowerCase().includes(filter.toLowerCase())) : files),
    [files, filter],
  )
  const tree = useMemo(() => buildTree(shown, filter ? [] : dirs), [shown, dirs, filter])
  const full = useMemo(() => buildTree(files, dirs), [files, dirs])

  const refresh = () => setReload((n) => n + 1)
  const focusTree = () => treeRef.current?.focus()

  // Where a new entry goes: the folder itself, or the folder holding a file.
  const targetDir = (entry: Entry | null) => (!entry ? '' : entry.isDir ? entry.path : parentOf(entry.path))

  function startNew(isDir: boolean, entry: Entry | null = selected) {
    if (!writable) return
    const parent = find(full, targetDir(entry))?.children ? targetDir(entry) : '' // gone since it was selected
    setMenu(null)
    setFilter('')
    setOpen((o) => {
      const next = { ...o }
      for (let p = parent; p; p = parentOf(p)) next[p] = true
      return next
    })
    setEditing({ kind: 'new', parent, isDir })
  }

  function startRename(entry: Entry | null = selected) {
    if (!writable || !entry) return
    setMenu(null)
    setEditing({ kind: 'rename', entry })
  }

  // Open file tabs at or under `path` with unsaved edits, which a rename or delete would discard.
  function unsavedUnder(path: string) {
    const { tabs, dirtyTabs } = useApp.getState()
    return tabs.filter(
      (t) => t.kind === 'file' && dirtyTabs[t.id] && wsKey(t.ws) === key && (t.path === path || t.path.startsWith(`${path}/`)),
    ).length
  }

  const unsavedNote = (count: number) =>
    count ? `\n\n${count === 1 ? 'An open file has' : `${count} open files have`} unsaved changes, which will be lost.` : ''

  async function remove(entry: Entry | null = selected) {
    if (!writable || !entry) return
    setMenu(null)
    const name = entry.path.split('/').pop()
    const note = unsavedNote(unsavedUnder(entry.path))
    if (!confirm(`Delete "${name}"${entry.isDir ? ' and everything in it' : ''}? This cannot be undone.${note}`)) return
    try {
      await api.deleteEntry(ws, entry.path)
      moveTabs(ws, entry.path, null)
      if (selected?.path === entry.path) setSelected(null)
    } catch (e) {
      showError(e)
    }
    refresh()
  }

  async function create(parent: string, isDir: boolean, name: string) {
    const path = join(parent, name)
    await api.createEntry(ws, path, isDir)
    setEditing(null)
    setSelected({ path, isDir })
    if (isDir) setOpen((o) => ({ ...o, [path]: true }))
    else openTab({ id: tabId.file(ws, path), kind: 'file', ws, path })
    refresh()
  }

  async function rename(entry: Entry, name: string) {
    const path = join(parentOf(entry.path), name)
    const unsaved = unsavedUnder(entry.path)
    if (unsaved && !confirm(`Rename "${entry.path.split('/').pop()}" to "${name}"?${unsavedNote(unsaved)}`)) {
      cancelEdit()
      return
    }
    await api.renameEntry(ws, entry.path, path)
    setEditing(null)
    moveTabs(ws, entry.path, path)
    setSelected({ path, isDir: entry.isDir })
    if (entry.isDir) {
      setOpen((o) => {
        const next: Record<string, boolean> = {}
        for (const [p, v] of Object.entries(o)) {
          next[p === entry.path || p.startsWith(`${entry.path}/`) ? path + p.slice(entry.path.length) : p] = v
        }
        return next
      })
    }
    refresh()
    focusTree()
  }

  function cancelEdit() {
    setEditing(null)
    focusTree()
  }

  function onKeyDown(e: KeyboardEvent) {
    if (editing || e.target instanceof HTMLInputElement) return
    if (e.key === 'F2') {
      e.preventDefault()
      startRename()
    } else if (e.key === 'Delete') {
      e.preventDefault()
      remove()
    }
  }

  function onContextMenu(e: MouseEvent, entry: Entry | null) {
    if (e.target instanceof HTMLInputElement) return // keep the browser's own menu in the filter and name boxes
    e.preventDefault()
    e.stopPropagation()
    if (entry) setSelected(entry)
    setMenu({ x: e.clientX, y: e.clientY, entry })
  }

  const newRow = (parent: string, depth: number) =>
    editing?.kind === 'new' && editing.parent === parent && (
      <NameInput
        key={`new:${parent}`}
        initial=""
        depth={depth}
        isDir={editing.isDir}
        validate={(name) => nameError(full, parent, name)}
        onSubmit={(name) => create(parent, editing.isDir, name)}
        onCancel={cancelEdit}
      />
    )

  const renameRow = (child: Node, depth: number) =>
    editing?.kind === 'rename' && editing.entry.path === child.path && (
      <NameInput
        key={`rename:${child.path}`}
        initial={child.name}
        depth={depth}
        isDir={!!child.children}
        validate={(name) => nameError(full, parentOf(child.path), name, child.path)}
        onSubmit={(name) => rename(editing.entry, name)}
        onCancel={cancelEdit}
      />
    )

  const render = (node: Node, depth: number): ReactNode[] => [
    newRow(node.path, depth),
    ...sorted(node).map((child) => {
      const indent = { paddingLeft: 6 + depth * 14 }
      const isSelected = selected?.path === child.path
      if (child.children) {
        const entry = { path: child.path, isDir: true }
        const isOpen = !!filter || !!open[child.path]
        const Chevron = isOpen ? ChevronDown : ChevronRight
        return (
          <div key={child.path}>
            {renameRow(child, depth) || (
              <button
                className={`tree-row dir ${isSelected ? 'selected' : ''}`}
                style={indent}
                aria-expanded={isOpen}
                onClick={() => {
                  setSelected(entry)
                  setOpen((o) => ({ ...o, [child.path]: !o[child.path] }))
                }}
                onContextMenu={(e) => onContextMenu(e, entry)}
              >
                <Chevron size={14} className="chev" />
                <span className="label">{child.name}</span>
              </button>
            )}
            {isOpen && render(child, depth + 1)}
          </div>
        )
      }
      const entry = { path: child.path, isDir: false }
      const id = tabId.file(ws, child.path)
      return (
        renameRow(child, depth) || (
          <button
            key={child.path}
            className={`tree-row ${activeTab === id ? 'active' : ''} ${isSelected ? 'selected' : ''}`}
            style={{ paddingLeft: indent.paddingLeft + 18 }}
            onClick={() => {
              setSelected(entry)
              openTab({ id, kind: 'file', ws, path: child.path })
            }}
            onContextMenu={(e) => onContextMenu(e, entry)}
            title={child.path}
          >
            <span className="label">{child.name}</span>
          </button>
        )
      )
    }),
  ]

  const readOnlyTitle = writable ? undefined : 'This repo is read-only'
  return (
    <div className="panel-body explorer" onContextMenu={(e) => onContextMenu(e, null)}>
      <div className="explorer-bar">
        <input className="filter" placeholder="Filter files" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <button className="icon-btn" title={readOnlyTitle ?? 'New file'} aria-label="New file" disabled={!writable} onClick={() => startNew(false)}>
          <FilePlus size={15} />
        </button>
        <button className="icon-btn" title={readOnlyTitle ?? 'New folder'} aria-label="New folder" disabled={!writable} onClick={() => startNew(true)}>
          <FolderPlus size={15} />
        </button>
        <button className="icon-btn" title="Refresh" aria-label="Refresh" onClick={refresh}>
          <RefreshCw size={14} />
        </button>
        <button className="icon-btn" title="Collapse folders" aria-label="Collapse folders" onClick={() => setOpen({})}>
          <ChevronsDownUp size={15} />
        </button>
      </div>
      {error && <p className="pad error-text">{error}</p>}
      <div
        className="tree"
        ref={treeRef}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        onClick={(e) => e.target === e.currentTarget && setSelected(null)}
      >
        {render(tree, 0)}
      </div>
      {!error && files.length === 0 && dirs.length === 0 && !editing && <p className="pad faint">No files</p>}
      {menu && (
        <div
          className="context-menu"
          role="menu"
          style={{ left: Math.min(menu.x, window.innerWidth - 180), top: Math.min(menu.y, window.innerHeight - 140) }}
          onPointerDown={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.preventDefault()}
        >
          <button role="menuitem" disabled={!writable} onClick={() => startNew(false, menu.entry)}>New File</button>
          <button role="menuitem" disabled={!writable} onClick={() => startNew(true, menu.entry)}>New Folder</button>
          {menu.entry && (
            <>
              <div className="context-sep" />
              <button role="menuitem" disabled={!writable} onClick={() => startRename(menu.entry)}>
                Rename<span className="faint">F2</span>
              </button>
              <button role="menuitem" className="danger" disabled={!writable} onClick={() => remove(menu.entry)}>
                Delete<span className="faint">Del</span>
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
