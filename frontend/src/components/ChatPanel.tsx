import { ArrowUp, History as HistoryIcon, LoaderCircle, Search, Square, SquarePen } from 'lucide-react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { fuzzyFilter } from '../fuzzy'
import { send } from '../socket'
import { useApp } from '../store'
import type { Conversation, Mode } from '../types'
import { EventView } from './EventList'
import { markdownComponents } from './ExternalLink'

function ago(ts: number) {
  const s = Date.now() / 1000 - ts
  if (s < 60) return 'Just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function ChatPanel() {
  const [showHistory, setShowHistory] = useState(false)
  const activeConv = useApp((s) => s.activeConv)
  const conv = useApp((s) => s.conversations.find((c) => c.id === s.activeConv))
  const workspace = useApp((s) => s.workspace)
  const newConversation = useApp((s) => s.newConversation)
  const showError = useApp((s) => s.showError)
  const [editingTitle, setEditingTitle] = useState(false)

  async function startNew() {
    try {
      await newConversation(workspace?.repo ?? null)
      setShowHistory(false)
    } catch (e) {
      showError(e)
    }
  }

  async function rename(title: string) {
    setEditingTitle(false)
    if (conv && title.trim() && title !== conv.title) await api.updateConversation(conv.id, { title }).catch(showError)
  }

  return (
    <div className="chat-panel">
      <div className="chat-header">
        {showHistory ? (
          <span className="chat-title">History</span>
        ) : editingTitle && conv ? (
          <input
            className="title-input"
            defaultValue={conv.title}
            autoFocus
            onBlur={(e) => rename(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && rename(e.currentTarget.value)}
          />
        ) : (
          <span className="chat-title" title="Double-click to rename" onDoubleClick={() => setEditingTitle(true)}>
            {conv?.title ?? 'Chat'}
          </span>
        )}
        <button
          className={`icon-btn ${showHistory ? 'on' : ''}`}
          onClick={() => setShowHistory((v) => !v)}
          title="Chat history"
          aria-label="Chat history"
        >
          <HistoryIcon size={15} />
        </button>
        <button className="icon-btn" onClick={startNew} title="New chat" aria-label="New chat">
          <SquarePen size={15} />
        </button>
      </div>
      {showHistory ? (
        <History onPick={() => setShowHistory(false)} />
      ) : activeConv ? (
        <ConversationView key={activeConv} id={activeConv} />
      ) : (
        <div className="empty-chat">
          <span>No conversation selected</span>
          <button className="btn" onClick={startNew}>New chat</button>
        </div>
      )}
    </div>
  )
}

function History({ onPick }: { onPick: () => void }) {
  const conversations = useApp((s) => s.conversations)
  const repos = useApp((s) => s.repos)
  const activeConv = useApp((s) => s.activeConv)
  const busy = useApp((s) => s.busyConvs)
  const openConversation = useApp((s) => s.openConversation)
  const refresh = useApp((s) => s.refreshConversations)
  const showError = useApp((s) => s.showError)
  const [query, setQuery] = useState('')
  const [repoFilter, setRepoFilter] = useState<string>('*')

  useEffect(() => {
    const t = setTimeout(() => refresh(query || undefined).catch(showError), 250)
    return () => clearTimeout(t)
  }, [query]) // eslint-disable-line react-hooks/exhaustive-deps

  const shown = conversations.filter((c) =>
    repoFilter === '*' ? true : repoFilter === '' ? c.repo === null : c.repo === repoFilter,
  )

  async function remove(c: Conversation) {
    if (!confirm(`Delete "${c.title}"?`)) return
    await api.deleteConversation(c.id).catch(showError)
  }

  return (
    <div className="history">
      <div className="history-filters">
        <div className="search">
          <Search size={14} />
          <input placeholder="Search chats" value={query} onChange={(e) => setQuery(e.target.value)} autoFocus />
        </div>
        <select value={repoFilter} onChange={(e) => setRepoFilter(e.target.value)} aria-label="Filter by repo">
          <option value="*">All repos</option>
          <option value="">No repo</option>
          {repos.map((r) => (
            <option key={r.name} value={r.name}>{r.name}</option>
          ))}
        </select>
      </div>
      <div className="history-list">
        {shown.map((c) => (
          <div
            key={c.id}
            className={`history-item ${c.id === activeConv ? 'active' : ''}`}
            onClick={() => openConversation(c.id).then(onPick, showError)}
          >
            <div className="history-title">
              {busy[c.id] && <LoaderCircle size={13} className="spin faint" />}
              <span className="ellipsis">{c.title}</span>
            </div>
            <div className="history-meta">
              <span>{c.mode === 'chat' ? 'Chat' : 'Agent'}</span>
              <span>·</span>
              <span>{c.repo ?? 'No repo'}</span>
              <span>·</span>
              <span>{ago(c.updated_at)}</span>
              <button className="text-btn danger" onClick={(e) => (e.stopPropagation(), remove(c))}>Delete</button>
            </div>
          </div>
        ))}
        {shown.length === 0 && <p className="pad faint">No chats found.</p>}
      </div>
    </div>
  )
}

function ConversationView({ id }: { id: string }) {
  const conv = useApp((s) => s.conversations.find((c) => c.id === id))
  const events = useApp((s) => s.convEvents[id])
  const streaming = useApp((s) => s.streaming[id])
  const busy = useApp((s) => !!s.busyConvs[id])
  const repos = useApp((s) => s.repos)
  const showError = useApp((s) => s.showError)
  const [draft, setDraft] = useState('')
  const bottom = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLTextAreaElement>(null)
  // @mentions: the repo's files, and the "@query" being typed at the caret (if any).
  const [files, setFiles] = useState<string[]>([])
  const [mention, setMention] = useState<{ start: number; query: string } | null>(null)
  const [pick, setPick] = useState(0)
  const repo = conv?.repo ?? null

  useEffect(() => {
    setFiles([])
    if (repo) api.tree({ repo, task: null }).then((t) => setFiles(t.files), () => setFiles([]))
  }, [repo])

  const suggestions = useMemo(() => (mention ? fuzzyFilter(mention.query, files, 8) : []), [mention, files])

  // Block body on purpose: newer Chrome returns a Promise from scrollIntoView, and React
  // would treat any returned value as a cleanup function and crash calling it.
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [events, streaming])

  if (!conv) return null

  function submit() {
    const prompt = draft.trim()
    if (!prompt || busy) return
    if (!send({ type: 'chat', conversation_id: id, prompt })) return showError('Not connected to the server')
    setDraft('')
    setMention(null)
  }

  function trackMention(el: HTMLTextAreaElement) {
    const before = el.value.slice(0, el.selectionStart)
    const match = /(^|\s)@([\w.\-/]*)$/.exec(before)
    setMention(match && files.length ? { start: before.length - match[2].length - 1, query: match[2] } : null)
    setPick(0)
  }

  function insertMention(path: string) {
    if (!mention) return
    const end = mention.start + 1 + mention.query.length
    const next = `${draft.slice(0, mention.start)}@${path} ${draft.slice(end)}`
    setDraft(next)
    setMention(null)
    requestAnimationFrame(() => {
      const caret = mention.start + path.length + 2
      input.current?.focus()
      input.current?.setSelectionRange(caret, caret)
    })
  }

  async function setMode(mode: Mode) {
    if (mode === conv!.mode) return
    try {
      await api.updateConversation(id, { mode })
    } catch (e) {
      showError(e)
    }
  }

  async function link(value: string) {
    try {
      await api.updateConversation(id, value ? { repo: value } : { unlink: true })
    } catch (e) {
      showError(e)
    }
  }

  return (
    <>
      <div className="context-bar">
        <div className="segmented text" role="radiogroup" aria-label="Conversation mode">
          {(['chat', 'agent'] as const).map((m) => (
            <button
              key={m}
              role="radio"
              aria-checked={conv.mode === m}
              className={conv.mode === m ? 'on' : ''}
              disabled={busy}
              onClick={() => setMode(m)}
              title={m === 'chat' ? 'Discuss and read code. No changes to files or your machine.' : 'Can edit files, run commands, and start tasks.'}
            >
              {m === 'chat' ? 'Chat' : 'Agent'}
            </button>
          ))}
        </div>
        <select value={conv.repo ?? ''} disabled={busy} onChange={(e) => link(e.target.value)} aria-label="Linked repo">
          <option value="">No repo</option>
          {repos.map((r) => (
            <option key={r.name} value={r.name}>
              {r.name} ({r.access === 'write' ? 'read / write' : 'read only'})
            </option>
          ))}
        </select>
      </div>
      <div className="transcript no-scrollbar">
        {(events ?? []).length === 0 && !busy && (
          <p className="empty">
            {conv.mode === 'chat'
              ? `Chat mode: discuss ideas${conv.repo ? ` and ${conv.repo}'s code` : ''}. Nothing on your machine is changed. Switch to Agent to make changes.`
              : conv.repo
                ? `Agent mode: ask about ${conv.repo} or describe a change. Larger changes run as tasks you review before committing.`
                : 'Agent mode with no repo. Link a repo above to work on code.'}
          </p>
        )}
        {(events ?? []).map((e, i) => (
          <EventView key={i} event={e} />
        ))}
        {streaming && (
          <div className="msg-text streaming-caret">
            <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{streaming}</Markdown>
          </div>
        )}
        {busy && !streaming && (
          <div className="working">
            <LoaderCircle size={13} className="spin" /> Working
          </div>
        )}
        <div ref={bottom} />
      </div>
      <div className="composer-wrap">
        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          {suggestions.length > 0 && (
            <div className="mention-menu" role="listbox" aria-label="Mention a file">
              {suggestions.map((path, i) => {
                const slash = path.lastIndexOf('/')
                return (
                  <div
                    key={path}
                    role="option"
                    aria-selected={i === pick}
                    className={`palette-item ${i === pick ? 'active' : ''}`}
                    onMouseEnter={() => setPick(i)}
                    onMouseDown={(e) => (e.preventDefault(), insertMention(path))}
                  >
                    <span>{path.slice(slash + 1)}</span>
                    <span className="faint ellipsis">{slash > 0 ? path.slice(0, slash) : ''}</span>
                  </div>
                )
              })}
            </div>
          )}
          <textarea
            ref={input}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value)
              trackMention(e.target)
            }}
            onClick={(e) => trackMention(e.currentTarget)}
            onBlur={() => setMention(null)}
            onKeyDown={(e) => {
              if (suggestions.length) {
                if (e.key === 'ArrowDown') return (e.preventDefault(), setPick((i) => (i + 1) % suggestions.length))
                if (e.key === 'ArrowUp') return (e.preventDefault(), setPick((i) => (i - 1 + suggestions.length) % suggestions.length))
                if (e.key === 'Enter' || e.key === 'Tab') return (e.preventDefault(), insertMention(suggestions[pick]))
                if (e.key === 'Escape') return (e.preventDefault(), setMention(null))
              }
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
            placeholder={busy ? 'The agent is working' : conv.mode === 'chat' ? 'Discuss an idea' : 'Describe what to do'}
            rows={3}
          />
          <div className="composer-foot">
            <span className="faint small">
              {conv.mode === 'chat' ? 'Chat mode, read only' : 'Agent mode'}
              {conv.repo ? ' · @ to mention a file' : ''}
            </span>
            <div className="spacer" />
            {busy ? (
              <button
                type="button"
                className="send-btn stop"
                onClick={() => send({ type: 'stop', conversation_id: id })}
                title="Stop"
                aria-label="Stop"
              >
                <Square size={11} fill="currentColor" />
              </button>
            ) : (
              <button type="submit" className="send-btn" disabled={!draft.trim()} title="Send" aria-label="Send">
                <ArrowUp size={15} />
              </button>
            )}
          </div>
        </form>
      </div>
    </>
  )
}
