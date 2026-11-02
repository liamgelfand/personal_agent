import type {
  AdminState, AgentEvent, AuthStatus, Conversation, GitStatus, Mode, RemoteInfo, Repo, Review, SearchMatch, Task,
  WorkspaceRef,
} from './types'

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.json().then((d) => d.detail).catch(() => res.statusText)
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return res.json()
}

function wsQuery(ws: WorkspaceRef, extra: Record<string, string> = {}) {
  const params = new URLSearchParams({ repo: ws.repo, ...extra })
  if (ws.task) params.set('task', ws.task)
  return params.toString()
}

export type FileContent = { path: string; binary: boolean; too_large: boolean; size: number; content: string }

export const api = {
  repos: () => request<Repo[]>('GET', '/api/repos'),
  auth: () => request<AuthStatus>('GET', '/api/auth'),

  conversations: (q?: string) =>
    request<Conversation[]>('GET', `/api/conversations${q ? `?q=${encodeURIComponent(q)}` : ''}`),
  conversation: (id: string) =>
    request<Conversation & { events: AgentEvent[] }>('GET', `/api/conversations/${id}`),
  createConversation: (repo: string | null, mode: Mode) => request<Conversation>('POST', '/api/conversations', { repo, mode }),
  updateConversation: (id: string, patch: { title?: string; repo?: string; unlink?: boolean; mode?: Mode }) =>
    request<Conversation>('PATCH', `/api/conversations/${id}`, patch),
  deleteConversation: (id: string) => request('DELETE', `/api/conversations/${id}`),
  openUrl: (url: string) => request('POST', '/api/open-url', { url }),

  tasks: () => request<Task[]>('GET', '/api/tasks'),
  task: (id: string) => request<Task & { events: AgentEvent[] }>('GET', `/api/tasks/${id}`),
  stopTask: (id: string) => request('POST', `/api/tasks/${id}/stop`),
  openPr: (id: string) => request<{ url: string }>('POST', `/api/tasks/${id}/pr`),
  discardTask: (id: string) => request('DELETE', `/api/tasks/${id}`),

  tree: (ws: WorkspaceRef) =>
    request<{ root: string; access: string; files: string[] }>('GET', `/api/fs/tree?${wsQuery(ws)}`),
  file: (ws: WorkspaceRef, path: string) => request<FileContent>('GET', `/api/fs/file?${wsQuery(ws, { path })}`),
  saveFile: (ws: WorkspaceRef, path: string, content: string) =>
    request('PUT', `/api/fs/file?${wsQuery(ws, { path })}`, { content }),

  status: (ws: WorkspaceRef) => request<GitStatus>('GET', `/api/git/status?${wsQuery(ws)}`),
  diff: (ws: WorkspaceRef, path: string, staged: boolean) =>
    request<{ original: string; modified: string }>(
      'GET', `/api/git/diff?${wsQuery(ws, { path, staged: String(staged) })}`,
    ),
  gitAction: (ws: WorkspaceRef, action: 'stage' | 'unstage' | 'discard', paths: string[]) =>
    request('POST', `/api/git/${action}?${wsQuery(ws)}`, { paths }),
  commit: (ws: WorkspaceRef, message: string) =>
    request<{ sha: string }>('POST', `/api/git-commit?${wsQuery(ws)}`, { message }),

  search: (ws: WorkspaceRef, q: string, regex: boolean, caseSensitive: boolean) =>
    request<{ matches: SearchMatch[]; truncated: boolean }>(
      'GET', `/api/fs/search?${wsQuery(ws, { q, regex: String(regex), case: String(caseSensitive) })}`,
    ),
  mergeTask: (id: string, message?: string) => request<{ sha: string }>('POST', `/api/tasks/${id}/merge`, { message }),
  restoreCheckpoint: (id: string) => request<{ restored: string[] }>('POST', `/api/checkpoints/${id}/restore`),

  admin: () => request<AdminState & { busy: Record<string, number> }>('GET', '/api/admin'),
  rebuild: () => request('POST', '/api/admin/rebuild'),
  restart: (force = false) => request('POST', `/api/admin/restart?force=${force}`),
  remote: () => request<RemoteInfo>('GET', '/api/remote'),
  testNotification: () => request('POST', '/api/notify/test'),

  review: (ws: WorkspaceRef) =>
    request<{ review: Review | null; running: boolean }>('GET', `/api/review?${wsQuery(ws)}`),
  startReview: (ws: WorkspaceRef) => request('POST', `/api/review?${wsQuery(ws)}`),
}
