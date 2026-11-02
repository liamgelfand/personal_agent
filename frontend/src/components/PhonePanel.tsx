import { Copy, Smartphone } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { notificationsSupported } from '../notify'
import { useApp } from '../store'
import type { RemoteInfo } from '../types'

// Phone access: remote URL and token, notifications, and home-screen install steps.
export function PhonePanel() {
  const [open, setOpen] = useState(false)
  const [info, setInfo] = useState<RemoteInfo | null>(null)
  const [permission, setPermission] = useState(notificationsSupported() ? Notification.permission : 'unsupported')
  const showError = useApp((s) => s.showError)
  const showInfo = useApp((s) => s.showInfo)
  const panel = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    api.remote().then(setInfo, showError)
    const close = (e: MouseEvent) => !panel.current?.contains(e.target as Node) && setOpen(false)
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  async function enableNotifications() {
    setPermission(await Notification.requestPermission())
  }

  const copy = (text: string) => navigator.clipboard?.writeText(text).then(() => showInfo('Copied'))

  return (
    <div className="popover-anchor" ref={panel}>
      <button className={`icon-btn ${open ? 'on' : ''}`} onClick={() => setOpen((v) => !v)} title="Phone access" aria-label="Phone access" aria-expanded={open}>
        <Smartphone size={15} />
      </button>
      {open && (
        <div className="popover phone-panel" role="dialog" aria-label="Phone access">
          <h3>Phone access</h3>
          {!info ? (
            <p className="faint">Loading</p>
          ) : info.enabled ? (
            <>
              <p className="muted">Open one of these on your phone (over Tailscale), then sign in with the token.</p>
              {info.urls.map((url) => (
                <div key={url} className="copy-row">
                  <span className="mono ellipsis">{url}</span>
                  <button className="icon-btn" onClick={() => copy(url)} aria-label="Copy address"><Copy size={13} /></button>
                </div>
              ))}
              {info.token && (
                <div className="copy-row">
                  <span className="mono ellipsis">Token: {info.token.slice(0, 6)}…</span>
                  <button className="icon-btn" onClick={() => copy(info.token!)} aria-label="Copy token"><Copy size={13} /></button>
                </div>
              )}
            </>
          ) : (
            <ol className="steps">
              <li>Install Tailscale on this computer and your phone, signed in to the same account.</li>
              <li>In <span className="mono">config.yaml</span>, set <span className="mono">remote: {'{'} enabled: true {'}'}</span>, then restart.</li>
              <li>Open this panel again for the address and access token.</li>
            </ol>
          )}

          <h4>Notifications</h4>
          <div className="setting-row">
            <span>This browser</span>
            {permission === 'granted' ? (
              <span className="status"><span className="dot ok" />On</span>
            ) : permission === 'unsupported' ? (
              <span className="faint small">Needs https or localhost</span>
            ) : (
              <button className="btn sm" onClick={enableNotifications} disabled={permission === 'denied'}>
                {permission === 'denied' ? 'Blocked' : 'Enable'}
              </button>
            )}
          </div>
          <div className="setting-row">
            <span>Phone push (ntfy)</span>
            {info?.ntfy ? (
              <button className="btn sm" onClick={() => api.testNotification().then(() => showInfo('Test sent'), showError)}>Send test</button>
            ) : (
              <span className="faint small">Set notifications.ntfy_topic</span>
            )}
          </div>

          <h4>Home screen</h4>
          <p className="muted small">iPhone: open the app in Safari, tap Share, then Add to Home Screen.</p>
        </div>
      )}
    </div>
  )
}
