import { Search } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { fuzzyFilter } from '../fuzzy'
import { tabId, useApp } from '../store'

// Ctrl+P: jump to any file in the current workspace.
export function QuickOpen() {
  const open = useApp((s) => s.quickOpen)
  const workspace = useApp((s) => s.workspace)
  const openTab = useApp((s) => s.openTab)
  const [files, setFiles] = useState<string[]>([])
  const [query, setQuery] = useState('')
  const [index, setIndex] = useState(0)
  const list = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open || !workspace) return
    setQuery('')
    setIndex(0)
    api.tree(workspace).then((t) => setFiles(t.files), () => setFiles([]))
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  const results = useMemo(() => fuzzyFilter(query, files, 50), [query, files])

  useEffect(() => {
    list.current?.children[index]?.scrollIntoView({ block: 'nearest' })
  }, [index])

  if (!open || !workspace) return null
  const close = () => useApp.setState({ quickOpen: false })
  const choose = (path: string | undefined) => {
    if (!path) return
    openTab({ id: tabId.file(workspace, path), kind: 'file', ws: workspace, path })
    close()
  }

  return (
    <div className="overlay" onMouseDown={close}>
      <div className="palette" role="dialog" aria-label="Go to file" onMouseDown={(e) => e.stopPropagation()}>
        <div className="palette-input">
          <Search size={15} />
          <input
            autoFocus
            placeholder={`Go to file in ${workspace.repo}${workspace.task ? ' (task)' : ''}`}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value)
              setIndex(0)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Escape') close()
              else if (e.key === 'ArrowDown') (e.preventDefault(), setIndex((i) => Math.min(i + 1, results.length - 1)))
              else if (e.key === 'ArrowUp') (e.preventDefault(), setIndex((i) => Math.max(i - 1, 0)))
              else if (e.key === 'Enter') choose(results[index])
            }}
          />
        </div>
        <div className="palette-list" ref={list} role="listbox">
          {results.map((path, i) => {
            const slash = path.lastIndexOf('/')
            return (
              <div
                key={path}
                role="option"
                aria-selected={i === index}
                className={`palette-item ${i === index ? 'active' : ''}`}
                onMouseEnter={() => setIndex(i)}
                onMouseDown={() => choose(path)}
              >
                <span>{path.slice(slash + 1)}</span>
                <span className="faint ellipsis">{slash > 0 ? path.slice(0, slash) : ''}</span>
              </div>
            )
          })}
          {results.length === 0 && <div className="pad faint">No matching files</div>}
        </div>
      </div>
    </div>
  )
}
