// Links in agent output. The app runs as a browser app window, so following a link there would
// replace the app; web links open in the default browser instead (see /api/open-url).
import type { ComponentProps, MouseEvent } from 'react'
import type { Components, ExtraProps } from 'react-markdown'
import { api } from '../api'

function isWebUrl(href: string) {
  try {
    return ['http:', 'https:'].includes(new URL(href).protocol)
  } catch {
    return false // relative links and the like: nothing sensible to open
  }
}

// Only the app window on the server's own computer needs the server's help. Anywhere else (a phone
// over remote access) the plain target=_blank link works, and a window.open after an async round
// trip would be blocked as a pop-up.
const ON_THIS_COMPUTER = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)

// Click handler for a web link: open it in the default browser instead of the app window.
export function openExternal(e: MouseEvent, url: string) {
  if (!ON_THIS_COMPUTER) return
  e.preventDefault()
  api.openUrl(url).catch(() => window.open(url, '_blank', 'noopener,noreferrer'))
}

export function ExternalLink({ href, title, children }: ComponentProps<'a'> & ExtraProps) {
  if (!href || !isWebUrl(href)) return <span>{children}</span>
  // Keep the real href, so hovering shows it and "Copy link" works.
  return (
    <a
      href={href}
      title={title}
      target="_blank"
      rel="noreferrer"
      onClick={(e) => openExternal(e, href)}
      onAuxClick={(e) => e.button === 1 && openExternal(e, href)}
    >
      {children}
    </a>
  )
}

export const markdownComponents: Components = { a: ExternalLink }
