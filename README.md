# Personal Agent

A local web IDE with a team of Claude agents for the repositories you delegate to it.
Built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview). By default it
runs on your Claude subscription login (Pro or Max), so it costs nothing beyond your plan.

- **Orchestrator** chats with you, answers questions, makes small edits, and starts tasks.
- **Tasks** run a fixed pipeline in an isolated git worktree: implementer, then reviewer, then
  fixes (up to two rounds). Changes stay uncommitted so you review the diff and commit yourself.
- **Reviewer** checks any uncommitted changes on request and pins its findings to diff lines.
- **Researcher** answers questions from the web and library docs.

Intended for personal use on your own machine. Anthropic does not allow offering claude.ai
login to other people's products; if you build something for others, use an API key.

## Requirements

- Python 3.11+, Node 20+, git
- [Claude Code](https://code.claude.com) installed and logged in (`claude`, then `/login`)
- Optional: the GitHub CLI (`gh`) for opening pull requests

## Setup

```sh
python -m pip install --user uv
python -m uv sync
cp config.example.yaml config.yaml   # list the repos agents may touch
cd frontend && npm install && npm run build && cd ..
```

## Run

```sh
python -m uv run personal-agent      # http://127.0.0.1:8000
```

The command runs a small supervisor, so the app can restart itself from the browser.

### Desktop shortcut (Windows)

```sh
python -m uv run python -m personal_agent.launcher --install-shortcut
```

This adds a "Personal Agent" shortcut to your Desktop and Start menu. It starts the server in
the background (no terminal; output goes to `data/logs/server.log`) if it isn't already running,
then opens the app in your default browser: as a standalone app window for Chrome, Edge, Brave,
and Vivaldi, or a normal tab otherwise. Opening it again reuses the running server. To stop the
server: `python -m uv run python -m personal_agent.launcher --stop`.
For UI development with hot reload, also run `cd frontend && npm run dev` and open
http://127.0.0.1:5173.

## Using it

- **Project and workspace** (top bar): pick a repo, then its main checkout or one of its task
  worktrees. Files, Search, Changes, and the editor follow the selected workspace.
- **Chats** are independent of the IDE selection. Link each one to a repo or to none. History
  lists past chats with search and a repo filter.
- **Modes**: **Chat** mode reads and discusses only (no file edits, no shell, no tasks).
  **Agent** mode can edit files, run commands, and start tasks.
- **@mentions**: type `@` in the message box to attach a file's contents.
- **Undo**: every Agent-mode turn that edited files can be undone from the transcript.
- **Quick open and search**: `Ctrl+P` jumps to a file; `Ctrl+Shift+F` searches the repo.
- **Changes**: side-by-side diffs, stage, unstage, discard, commit, and "Request review".
- **Tasks**: follow a task's log, open its worktree, stop it, merge it into your checkout,
  open a pull request, or discard it.
- **Approvals**: commits, pushes, package installs, and similar commands pause the agent until
  you allow or deny them.

## Configuration

See `config.example.yaml` for every option: repos and access levels, models per role, billing,
remote access, and notifications.

### Billing

`billing: subscription` (the default) removes `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`
from the server's environment at startup, so agent runs always use your claude.ai plan login.
The top bar shows which account is in use. Set `billing: api` to use an API key instead.

### Repo access

A guard runs before every tool call:

| | `access: read` | `access: write` | task worktree |
|---|---|---|---|
| Read / search files | yes | yes | yes |
| Edit / write files (agent or you) | blocked | yes | yes |
| Bash | read-only commands only | yes, except the blocklist | yes, except the blocklist |
| Start tasks | no | yes | n/a |
| Files outside delegated repos | blocked | blocked | blocked |

Always blocked: force-push, pushing to main/master, `git reset --hard`, `git clean -f`,
`gh repo delete`, and the PowerShell tool. Always needs approval: merging a PR.
With `auto_approve: false` also: `git commit`, `git push`, `gh pr/issue create|edit|comment`,
package installs, `curl | sh`, `docker`.
Bash in writable workspaces can still reach anything your user account can, so this is a
guardrail, not a sandbox. Only delegate repos you are comfortable with an agent changing.

### Phone access

1. Install [Tailscale](https://tailscale.com) on your computer and phone.
2. In `config.yaml`, set `remote: { enabled: true }` and restart.
3. Open the phone icon in the top bar for the address and the access token (stored in
   `data/remote_token`). Every device must sign in with it.
4. On iPhone, open the address in Safari, then Share > Add to Home Screen.

For push notifications (approval needed, task ready or failed), set `notifications.ntfy_topic`
to a long random name and subscribe to it in the [ntfy](https://ntfy.sh) app. Only short status
text is sent, never code.

### Letting agents improve this app

Add this project's own folder to `config.yaml` with `access: write` to let agents work on it.
Edits to its `config.yaml`, `.env`, and `src/personal_agent/guards.py` always need your
approval, so an agent cannot widen its own access unnoticed. When its source changes, a banner
offers to rebuild the UI and restart the server.

## Layout

```
agents/*.md                 specialist prompts (also usable by plain Claude Code: copy to ~/.claude/agents/)
src/personal_agent/
  server.py                 FastAPI routes, sign-in, and the WebSocket
  chat.py                   conversations: orchestrator turns, modes, @mentions, start_task tool
  tasks.py                  task pipeline in git worktrees, merge, pull requests
  review.py                 structured reviewer run
  runner.py                 one agent query -> UI events (shared by everything above)
  guards.py                 PreToolUse allow / deny / ask guard
  checkpoints.py            per-turn file snapshots for undo
  bus.py                    event fan-out to browsers, pending approvals
  workspace.py, gitops.py   file access, search, and git operations per workspace
  admin.py, watcher.py      self-update (rebuild / restart) and live file-change events
  remote.py, notify.py      remote sign-in and ntfy notifications
  store.py                  SQLite (data/agent.db)
frontend/                   React + Monaco IDE
tests/                      pytest
```

All local state (database, checkpoints, worktrees, access token) lives in `data/`, which is
gitignored along with `config.yaml` and `.env`.

## Tests

```sh
python -m uv run pytest
```
