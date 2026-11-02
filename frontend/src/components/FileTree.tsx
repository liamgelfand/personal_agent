import { ChevronDown, ChevronRight } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { tabId, useApp } from '../store'
import { wsKey } from '../types'
import type { WorkspaceRef } from '../types'

type Node = { name: string; path: string; children: Map<string, Node> | null }

function buildTree(files: string[]): Node {
  const root: Node = { name: '', path: '', children: new Map() }
  for (const file of files) {
    let node = root
    const parts = file.split('/')
    parts.forEach((part, i) => {
      const isFile = i === parts.length - 1
      let child = node.children!.get(part)
      if (!child) {
        child = { name: part, path: parts.slice(0, i + 1).join('/'), children: isFile ? null : new Map() }
        node.children!.set(part, child)
      }
      node = child
    })
  }
  return root
}

function sorted(node: Node): Node[] {
  return [...(node.children?.values() ?? [])].sort((a, b) =>
    !!a.children === !!b.children ? a.name.localeCompare(b.name) : a.children ? -1 : 1,
  )
}

export function FileTree({ ws }: { ws: WorkspaceRef }) {
  const key = wsKey(ws)
  const version = useApp((s) => s.fsVersion[key] ?? 0)
  const activeTab = useApp((s) => s.activeTab)
  const openTab = useApp((s) => s.openTab)
  const [files, setFiles] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [filter, setFilter] = useState('')

  useEffect(() => {
    let cancelled = false
    api.tree(ws).then(
      (t) => !cancelled && (setFiles(t.files), setError(null)),
      (e) => !cancelled && setError(String(e.message ?? e)),
    )
    return () => {
      cancelled = true
    }
  }, [key, version]) // eslint-disable-line react-hooks/exhaustive-deps

  const shown = useMemo(
    () => (filter ? files.filter((f) => f.toLowerCase().includes(filter.toLowerCase())) : files),
    [files, filter],
  )
  const tree = useMemo(() => buildTree(shown), [shown])

  const render = (node: Node, depth: number) =>
    sorted(node).map((child) => {
      const indent = { paddingLeft: 6 + depth * 14 }
      if (child.children) {
        const isOpen = !!filter || !!open[child.path]
        const Chevron = isOpen ? ChevronDown : ChevronRight
        return (
          <div key={child.path}>
            <button
              className="tree-row dir"
              style={indent}
              aria-expanded={isOpen}
              onClick={() => setOpen((o) => ({ ...o, [child.path]: !o[child.path] }))}
            >
              <Chevron size={14} className="chev" />
              <span className="label">{child.name}</span>
            </button>
            {isOpen && render(child, depth + 1)}
          </div>
        )
      }
      const id = tabId.file(ws, child.path)
      return (
        <button
          key={child.path}
          className={`tree-row ${activeTab === id ? 'active' : ''}`}
          style={{ paddingLeft: indent.paddingLeft + 18 }}
          onClick={() => openTab({ id, kind: 'file', ws, path: child.path })}
          title={child.path}
        >
          <span className="label">{child.name}</span>
        </button>
      )
    })

  return (
    <div className="panel-body">
      <input className="filter" placeholder="Filter files" value={filter} onChange={(e) => setFilter(e.target.value)} />
      {error && <p className="pad error-text">{error}</p>}
      <div className="tree">{render(tree, 0)}</div>
      {!error && files.length === 0 && <p className="pad faint">No files</p>}
    </div>
  )
}
