// Browser notifications for events that need you while this tab is in the background.
// Browsers only allow them on https or localhost; on a phone over plain http, use ntfy instead.

export const notificationsSupported = () => 'Notification' in window && window.isSecureContext

export function notify(title: string, body: string, tag?: string) {
  if (document.visibilityState === 'visible' || !notificationsSupported()) return
  if (Notification.permission !== 'granted') return
  new Notification(title, { body, tag, icon: '/icon-192.png' })
}
