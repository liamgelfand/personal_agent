import { LoaderCircle, RotateCw } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useApp } from '../store'

// Shown when this app's own source changed (usually an agent working on personal_agent):
// one button rebuilds the UI and/or restarts the server, whichever changed, without leaving the browser.
export function AdminBar() {
  const admin = useApp((s) => s.admin)
  const restarting = useApp((s) => s.restarting)
  const log = useApp((s) => s.adminLog)
  const showError = useApp((s) => s.showError)
  const [built, setBuilt] = useState(false)
  const wasBuilding = useRef(false)
  const applying = useRef(false)

  // A build that just finished cleanly means the page can reload into the new UI, or, when applying
  // all changes, the server restarts next (the page reloads when it is back).
  useEffect(() => {
    if (admin?.building) wasBuilding.current = true
    else if (wasBuilding.current) {
      wasBuilding.current = false
      const finishing = applying.current
      applying.current = false
      if (admin && !admin.pending.ui) {
        if (finishing && admin.pending.server) void restart()
        else if (finishing) location.reload()
        else setBuilt(true)
      }
    }
  }, [admin])

  if (!admin) return null
  const { server, ui } = admin.pending
  if (!server && !ui && !admin.building && !restarting && !built) return null

  async function restart() {
    try {
      await api.restart()
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      if (message.includes('agents are running') && confirm(`${message}\n\nRestart anyway? Running agents will be stopped.`)) {
        await api.restart(true).catch(showError)
      } else if (!message.includes('agents are running')) {
        showError(e)
      }
    }
  }

  function apply() {
    if (ui) {
      applying.current = true
      api.rebuild().catch((e) => {
        applying.current = false
        showError(e)
      })
    } else void restart()
  }

  let text: string
  if (restarting) text = 'Restarting the server. The page reloads when it is back.'
  else if (admin.building) text = log[log.length - 1] ?? 'Building the UI'
  else if (built && !server) text = 'The UI was rebuilt.'
  else text = `This app's ${server && ui ? 'server and UI code' : server ? 'server code' : 'UI code'} changed.`

  return (
    <div className="admin-bar" role="status">
      {restarting || admin.building ? <LoaderCircle size={13} className="spin" /> : <RotateCw size={13} />}
      <span className="ellipsis">{text}</span>
      <div className="spacer" />
      {!restarting && !admin.building && (
        <>
          {(ui || server) && (
            <button
              className="btn sm"
              onClick={apply}
              disabled={server && !admin.supervised}
              title={server && !admin.supervised ? 'Start the server with `uv run personal-agent` to enable restarts' : ''}
            >
              {ui && server ? 'Rebuild UI and restart server' : server ? 'Restart server' : 'Rebuild UI'}
            </button>
          )}
          {built && !server && <button className="btn sm primary" onClick={() => location.reload()}>Reload page</button>}
        </>
      )}
    </div>
  )
}
