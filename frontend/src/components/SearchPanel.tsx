import { CaseSensitive, LoaderCircle, Regex, Search } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { tabId, useApp } from '../store'
import { wsKey } from '../types'
import type { SearchMatch, WorkspaceRef } from '../types'

export function SearchPanel({ ws }: { ws: WorkspaceRef }) {
  const openTab = useApp((s) => s.openTab)
  const version = useApp((s) => s.fsVersion[wsKey(ws)] ?? 0)
  const [query, setQuery] = useState('')
  const [regex, setRegex] = useState(false)
  const [caseSensitive, setCaseSensitive] = useState(false)
  const [result, setResult] = useState<{ matches: SearchMatch[]; truncated: boolean } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!query.trim()) {
      setResult(null)
      return
    }
    setLoading(true)
    const t = setTimeout(() => {
      api.search(ws, query, regex, caseSensitive).then(
        (r) => (setResult(r), setError(null)),
        (e) => (setResult(null), setError(e.message)),
      ).finally(() => setLoading(false))
    }, 250)
    return () => clearTimeout(t)
  }, [query, regex, caseSensitive, wsKey(ws), version]) // eslint-disable-line react-hooks/exhaustive-deps

  const grouped = useMemo(() => {
    const map = new Map<string, SearchMatch[]>()
    for (const m of result?.matches ?? []) map.set(m.path, [...(map.get(m.path) ?? []), m])
    return [...map.entries()]
  }, [result])

  return (
    <div className="panel-body">
      <div className="search-box">
        <div className="search">
          <Search size={14} />
          <input autoFocus placeholder="Search in files" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <div className="segmented" role="group" aria-label="Search options">
          <button className={caseSensitive ? 'on' : ''} onClick={() => setCaseSensitive((v) => !v)} title="Match case" aria-pressed={caseSensitive}>
            <CaseSensitive size={14} />
          </button>
          <button className={regex ? 'on' : ''} onClick={() => setRegex((v) => !v)} title="Regular expression" aria-pressed={regex}>
            <Regex size={14} />
          </button>
        </div>
      </div>
      {loading && <div className="pad working"><LoaderCircle size={13} className="spin" /> Searching</div>}
      {error && <p className="pad error-text small">{error}</p>}
      {result && !loading && (
        <p className="pad faint small">
          {result.matches.length}{result.truncated ? '+' : ''} results in {grouped.length} files
        </p>
      )}
      {grouped.map(([path, matches]) => (
        <section key={path} className="search-group">
          <div className="search-file" title={path}>
            <span>{path.split('/').pop()}</span>
            <span className="faint ellipsis">{path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : ''}</span>
            <span className="count">{matches.length}</span>
          </div>
          {matches.map((m) => (
            <button
              key={`${m.path}:${m.line}`}
              className="search-hit"
              onClick={() => openTab({ id: tabId.file(ws, m.path), kind: 'file', ws, path: m.path, line: m.line })}
            >
              <span className="faint num">{m.line}</span>
              <span className="ellipsis mono">{m.text.trim()}</span>
            </button>
          ))}
        </section>
      ))}
    </div>
  )
}
