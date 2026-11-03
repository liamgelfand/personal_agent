// Open chat tabs: pure list logic, kept free of imports so tests/test_chat_tabs.py can run it under Node.

export type ChatTabs = { ids: string[]; active: string | null }

export function addChatTab(ids: string[], id: string): string[] {
  return ids.includes(id) ? ids : [...ids, id]
}

// Closing the active tab activates its right neighbour, else its left one, like a browser.
export function closeChatTab({ ids, active }: ChatTabs, id: string): ChatTabs {
  const index = ids.indexOf(id)
  if (index < 0) return { ids, active }
  const next = ids.filter((x) => x !== id)
  if (active !== id) return { ids: next, active }
  return { ids: next, active: next[Math.min(index, next.length - 1)] ?? null }
}

export function removeChatTabs(tabs: ChatTabs, gone: Iterable<string>): ChatTabs {
  let next = tabs
  for (const id of gone) next = closeChatTab(next, id)
  return next
}

export function cycleChatTab({ ids, active }: ChatTabs, step: number): string | null {
  if (!ids.length) return null
  const index = active ? ids.indexOf(active) : -1
  return ids[(((index < 0 ? 0 : index + step) % ids.length) + ids.length) % ids.length]
}

// Cleans saved tab ids: drops non-strings, duplicates and, if given, ids not in `existing`.
export function pruneChatTabs(ids: unknown, existing?: Set<string>): string[] {
  if (!Array.isArray(ids)) return []
  return ids.filter((id, i): id is string => typeof id === 'string' && (!existing || existing.has(id)) && ids.indexOf(id) === i)
}
