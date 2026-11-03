import { LoaderCircle, Plus, X } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { useApp } from '../store'

// Browser-style tabs over the open conversations. Closing a tab never deletes the conversation.
export function ChatTabs({ onSelect, onNew }: { onSelect: () => void; onNew: () => void }) {
  const chatTabs = useApp((s) => s.chatTabs)
  const activeConv = useApp((s) => s.activeConv)
  const cache = useApp((s) => s.convCache)
  const busy = useApp((s) => s.busyConvs)
  const unseen = useApp((s) => s.unseenConvs)
  const openConversation = useApp((s) => s.openConversation)
  const closeChatTab = useApp((s) => s.closeChatTab)
  const showError = useApp((s) => s.showError)
  const bar = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bar.current?.querySelector('.chat-tab.active')?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [activeConv, chatTabs])

  if (chatTabs.length === 0) return null

  const select = (id: string) => openConversation(id).then(onSelect, showError)
  const close = (id: string) => closeChatTab(id).catch(showError)

  return (
    <div className="chat-tabs" role="tablist" aria-label="Open chats" ref={bar}>
      {chatTabs.map((id) => {
        const title = cache[id]?.title ?? 'Chat'
        const active = id === activeConv
        return (
          <div
            key={id}
            role="tab"
            aria-selected={active}
            className={`chat-tab ${active ? 'active' : ''}`}
            title={title}
            onClick={() => select(id)}
            onMouseDown={(e) => e.button === 1 && (e.preventDefault(), close(id))}
          >
            {busy[id] && <LoaderCircle size={12} className="spin faint" aria-label="Working" />}
            {unseen[id] && <span className="dot live" aria-label="New activity" />}
            <span className="ellipsis">{title}</span>
            <button className="tab-close" aria-label="Close tab" onClick={(e) => (e.stopPropagation(), close(id))}>
              <X size={12} />
            </button>
          </div>
        )
      })}
      <button className="icon-btn chat-tab-new" onClick={onNew} title="New chat" aria-label="New chat in a new tab">
        <Plus size={14} />
      </button>
    </div>
  )
}
