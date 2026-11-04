// The integrated terminal: a bottom dock of shells under the editor (see src/personal_agent/terminal.py).
// Every terminal stays mounted while the dock is hidden or another tab is shown, so its shell keeps running.
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import { Terminal } from '@xterm/xterm'
import type { ITheme } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { ChevronDown, Plus, SquareTerminal, X } from 'lucide-react'
import { useEffect, useRef } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import { EDITOR_FONT, prefersDark } from '../monaco'
import { MAX_TERMINALS, useApp } from '../store'
import type { TerminalRef } from '../store'
import { openUrl } from './ExternalLink'

const MIN_HEIGHT = 80

// Matched to the app's surfaces and status colors (see index.css tokens).
const DARK: ITheme = {
  background: '#111214', foreground: '#e8e9eb', cursor: '#e8e9eb', cursorAccent: '#111214',
  selectionBackground: '#5b95ff40', scrollbarSliderBackground: '#30333866', scrollbarSliderHoverBackground: '#303338',
  black: '#17181b', red: '#ef5b5b', green: '#3fb97f', yellow: '#d9a23a', blue: '#5b95ff', magenta: '#c084fc',
  cyan: '#4fc1d6', white: '#a1a3a9', brightBlack: '#6b6e75', brightRed: '#ff7b7b', brightGreen: '#5fd39a',
  brightYellow: '#f0c060', brightBlue: '#82b0ff', brightMagenta: '#d4a5ff', brightCyan: '#7dd8e8', brightWhite: '#e8e9eb',
}
const LIGHT: ITheme = {
  background: '#ffffff', foreground: '#17181b', cursor: '#17181b', cursorAccent: '#ffffff',
  selectionBackground: '#2160e033', scrollbarSliderBackground: '#d4d4d899', scrollbarSliderHoverBackground: '#d4d4d8',
  black: '#17181b', red: '#c93434', green: '#1a7f4b', yellow: '#a86b00', blue: '#2160e0', magenta: '#8a3fcf',
  cyan: '#137f91', white: '#8a8c93', brightBlack: '#55575e', brightRed: '#e04848', brightGreen: '#239a5c',
  brightYellow: '#c27f00', brightBlue: '#3b78f0', brightMagenta: '#a258e6', brightCyan: '#1a98ad', brightWhite: '#b4b6bc',
}
const darkQuery = window.matchMedia('(prefers-color-scheme: dark)')
const theme = () => (prefersDark() ? DARK : LIGHT)

// Each connection gets its own id, so a superseded socket (React StrictMode mounts twice in dev) that
// reaches the server late is refused instead of taking over or killing the live shell.
let connectionSeq = 0

function socketUrl({ id: tab, repo, task }: TerminalRef, cols: number, rows: number) {
  const id = `${tab}.${++connectionSeq}`
  const params = new URLSearchParams({ id, repo: repo ?? '', task: task ?? '', cols: String(cols), rows: String(rows) })
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/api/terminal/ws?${params}`
}

function TerminalView({ term: ref, visible }: { term: TerminalRef; visible: boolean }) {
  const box = useRef<HTMLDivElement>(null)
  const controls = useRef<{ fit: () => void; focus: () => void } | null>(null)

  useEffect(() => {
    const el = box.current!
    const term = new Terminal({
      fontFamily: EDITOR_FONT.fontFamily, fontSize: EDITOR_FONT.fontSize, theme: theme(),
      cursorBlink: true, scrollback: 5000,
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.loadAddon(new WebLinksAddon((_e, uri) => openUrl(uri)))
    term.open(el)
    // Fitting a hidden terminal would shrink it to nothing; it is fitted again when shown.
    const fitIfShown = () => el.offsetWidth > 0 && el.offsetHeight > 0 && fit.fit()
    fitIfShown()

    const socket = new WebSocket(socketUrl(ref, term.cols, term.rows))
    socket.binaryType = 'arraybuffer'
    let ended = false
    const send = (data: string | Uint8Array<ArrayBuffer>) => socket.readyState === WebSocket.OPEN && socket.send(data)
    const sendSize = () => send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    socket.onopen = sendSize // in case it was fitted again while connecting
    socket.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(e.data))
        return
      }
      const msg = JSON.parse(e.data)
      ended = true
      if (msg.type === 'exit') term.write(`\r\n\x1b[2m[Process exited with code ${msg.code ?? '?'}]\x1b[0m\r\n`)
      else if (msg.type === 'error') term.write(`\x1b[31m${msg.message}\x1b[0m\r\n`)
    }
    socket.onclose = () => !ended && term.write('\r\n\x1b[2m[Disconnected]\x1b[0m\r\n')

    const encoder = new TextEncoder()
    const subscriptions = [
      term.onData((data) => send(encoder.encode(data))),
      // Raw bytes (one per character), kept apart from UTF-8 text so the server doesn't decode them as such.
      term.onBinary((data) => send(JSON.stringify({ type: 'binary', data }))),
      term.onResize(sendSize),
    ]
    const observer = new ResizeObserver(fitIfShown)
    observer.observe(el)
    const onScheme = () => (term.options.theme = theme())
    darkQuery.addEventListener('change', onScheme)
    controls.current = { fit: fitIfShown, focus: () => term.focus() }

    return () => {
      controls.current = null
      darkQuery.removeEventListener('change', onScheme)
      observer.disconnect()
      subscriptions.forEach((s) => s.dispose())
      socket.onopen = socket.onmessage = socket.onclose = null
      socket.close() // the server then ends the shell
      term.dispose()
    }
    // A terminal's id, repo and task never change, so this runs once per terminal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ref.id])

  useEffect(() => {
    if (!visible) return
    const frame = requestAnimationFrame(() => {
      controls.current?.fit()
      controls.current?.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [visible])

  return <div ref={box} className="terminal-view" hidden={!visible} />
}

export function TerminalPanel() {
  const open = useApp((s) => s.terminalOpen)
  const height = useApp((s) => s.terminalHeight)
  const terminals = useApp((s) => s.terminals)
  const active = useApp((s) => s.activeTerminal)
  const { newTerminal, closeTerminal, toggleTerminal } = useApp.getState()
  const dock = useRef<HTMLDivElement>(null)

  const startResize = (e: ReactPointerEvent) => {
    e.preventDefault()
    const startY = e.clientY
    const startHeight = dock.current?.offsetHeight ?? height
    const maxHeight = (dock.current?.parentElement?.clientHeight ?? 600) - 120
    const move = (ev: PointerEvent) => {
      const next = Math.round(startHeight + startY - ev.clientY)
      useApp.setState({ terminalHeight: Math.max(MIN_HEIGHT, Math.min(next, maxHeight)) })
    }
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      document.body.classList.remove('resizing-rows')
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    document.body.classList.add('resizing-rows')
  }

  return (
    <div ref={dock} className="terminal-dock" hidden={!open} style={{ height }}>
      <div className="terminal-resize" role="separator" aria-orientation="horizontal" aria-label="Resize terminal" onPointerDown={startResize} />
      <div className="tabs no-scrollbar terminal-tabs" role="tablist">
        {terminals.map((t) => (
          <div
            key={t.id}
            role="tab"
            aria-selected={t.id === active}
            className={`tab ${t.id === active ? 'active' : ''}`}
            onClick={() => useApp.setState({ activeTerminal: t.id })}
            onMouseDown={(e) => e.button === 1 && (e.preventDefault(), closeTerminal(t.id))}
            title={t.repo ? `${t.title}${t.task ? ' (task worktree)' : ''}` : 'Scratch folder (no repo)'}
          >
            <SquareTerminal size={13} />
            <span>{t.title}</span>
            <button className="tab-close" aria-label="Close terminal" onClick={(e) => (e.stopPropagation(), closeTerminal(t.id))}>
              <X size={13} />
            </button>
          </div>
        ))}
        <div className="terminal-actions">
          <button
            className="icon-btn"
            onClick={newTerminal}
            disabled={terminals.length >= MAX_TERMINALS}
            title={terminals.length >= MAX_TERMINALS ? `At most ${MAX_TERMINALS} terminals` : 'New terminal'}
            aria-label="New terminal"
          >
            <Plus size={14} />
          </button>
          <div className="spacer" />
          <button className="icon-btn" onClick={toggleTerminal} title="Hide terminal (Ctrl+`)" aria-label="Hide terminal">
            <ChevronDown size={14} />
          </button>
        </div>
      </div>
      <div className="terminal-body">
        {terminals.map((t) => (
          <TerminalView key={t.id} term={t} visible={open && t.id === active} />
        ))}
      </div>
    </div>
  )
}
