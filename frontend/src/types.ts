// Shapes returned by the Python server (src/personal_agent/*).

export type Access = 'read' | 'write'

export type Repo = { name: string; path: string; access: Access; github: string | null }

export type Mode = 'agent' | 'chat'

export type Conversation = {
  id: string
  title: string
  repo: string | null
  mode: Mode
  session_id: string | null
  created_at: number
  updated_at: number
  busy?: boolean
}

export type TaskStatus =
  | 'queued' | 'implementing' | 'reviewing' | 'fixing' | 'ready' | 'failed' | 'stopped' | 'discarded' | 'merged'

export type Task = {
  id: string
  conversation_id: string | null
  repo: string
  title: string
  spec: string
  branch: string
  base_sha: string
  worktree: string
  status: TaskStatus
  review_rounds: number
  pr_url: string | null
  error: string | null
  created_at: number
  updated_at: number
}

export type Finding = {
  file: string
  line: number
  severity: 'blocking' | 'should_fix' | 'nit'
  title: string
  detail: string
}

export type Review = {
  verdict: 'approve' | 'changes_requested'
  summary: string
  findings: Finding[]
  created_at?: number
}

// Persisted conversation/task events plus live-only ones.
export type AgentEvent =
  | { type: 'user'; text: string }
  | { type: 'text'; agent: string; text: string }
  | { type: 'tool'; agent: string; name: string; summary: string; subagent: string | null }
  | { type: 'result'; is_error: boolean; num_turns: number; cost_usd: number | null; errors: string[] | null }
  | { type: 'task_link'; task_id: string; title: string }
  | { type: 'review'; agent: string; review: Review }
  | { type: 'error'; message: string }
  | { type: 'warning'; message: string }
  | { type: 'stopped' }
  | { type: 'checkpoint'; id: string; files: string[]; restored?: boolean }

export type FileStatus = {
  path: string
  index: string
  worktree: string
  untracked: boolean
  staged: boolean
  unstaged: boolean
}

export type GitStatus = { branch: string; files: FileStatus[]; is_repo: boolean; access?: Access }

export type AuthStatus = {
  billing: 'subscription' | 'api'
  loggedIn?: boolean
  authMethod?: string
  subscriptionType?: string
  email?: string
  orgName?: string
  api_key_in_env?: boolean
  error?: string
}

export type UsageWindow = { utilization: number; resetsAt: number; status: string }

export type Approval = {
  id: string
  tool: string
  summary: string
  reason: string
  conversation_id?: string
  task_id?: string
  workspace?: string
}

// The IDE side: a repo's main checkout, or a task's worktree.
export type WorkspaceRef = { repo: string; task: string | null }

export const wsKey = (w: WorkspaceRef) => (w.task ? `${w.repo}@${w.task}` : w.repo)

export type SearchMatch = { path: string; line: number; text: string }

export type RemoteInfo = { enabled: boolean; port: number; urls: string[]; ntfy: boolean; token?: string }

export type AdminState = {
  boot_id: string
  pending: { server: boolean; ui: boolean }
  building: boolean
  supervised: boolean
}

export type MobileView = 'chat' | 'files' | 'changes' | 'tasks' | 'editor'
export type SideTab = 'files' | 'search' | 'changes' | 'tasks'

export type Tab =
  | { id: string; kind: 'file'; ws: WorkspaceRef; path: string; line?: number }
  | { id: string; kind: 'diff'; ws: WorkspaceRef; path: string; staged: boolean; line?: number }
  | { id: string; kind: 'task'; taskId: string }
