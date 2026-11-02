"""FastAPI app: conversations, tasks, workspace files/git, admin, and one WebSocket for live events."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import gitops, launcher, remote
from .admin import Admin, AdminError
from .bus import Bus
from .chat import ChatManager
from .checkpoints import CheckpointError, Checkpoints
from .config import PROJECT_ROOT, load_config
from .notify import Notifier
from .review import review_workspace
from .store import Store
from .tasks import TaskManager
from .watcher import watch
from .workspace import WorkspaceError, get_workspace, list_files, read_file, search, write_file

# The Vite dev server proxies to us from another port on this machine.
DEV_ORIGINS = {"http://127.0.0.1:5173", "http://localhost:5173"}
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# Reachable without logging in when remote access is on: the login page and the home-screen app files.
PUBLIC_PATHS = {"/login", "/api/login", "/manifest.webmanifest", "/icon.svg", "/apple-touch-icon.png",
                "/icon-192.png", "/icon-512.png"}

config = load_config()

# Billing: with billing "subscription" (the default), strip API credentials from the environment
# so the Claude Code process the SDK spawns can only use your claude.ai plan login.
API_CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
if config.billing == "subscription":
    for var in API_CREDENTIAL_VARS:
        os.environ.pop(var, None)

store = Store()
notifier = Notifier(config.notifications)
bus = Bus(notifier)
admin = Admin(bus)
checkpoints = Checkpoints(store)
tasks = TaskManager(config, store, bus)
chats = ChatManager(config, store, bus, tasks, checkpoints)
reviews_running: set[str] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks.recover()
    if config.remote.enabled:
        print(f"Remote access on. Access token: {remote.token()}  (also in {remote.TOKEN_PATH})", flush=True)
    watcher = asyncio.create_task(watch(config, bus, admin))
    yield
    watcher.cancel()


app = FastAPI(title="personal-agent", lifespan=lifespan)


def same_origin(origin: str | None, host: str | None) -> bool:
    """CSRF defense: state-changing requests must come from a page this server served."""
    if not origin:
        return True  # not a browser cross-site request (curl, same-origin GET navigations)
    if origin in DEV_ORIGINS:
        return True
    return bool(host) and urlsplit(origin).netloc == host


def host_allowed(host: str | None, remote_enabled: bool) -> bool:
    """DNS-rebinding defense: a site that points its own name at 127.0.0.1 sends matching Origin and
    Host headers, so local-only mode must also insist the Host is a loopback name. With remote
    access on, every request needs the sign-in token instead."""
    if remote_enabled:
        return True
    hostname = urlsplit(f"//{host or ''}").hostname
    return hostname in LOOPBACK


def signed_in(cookies: dict[str, str]) -> bool:
    return not config.remote.enabled or remote.valid(cookies.get(remote.COOKIE))


@app.middleware("http")
async def guard_requests(request: Request, call_next):
    if not host_allowed(request.headers.get("host"), config.remote.enabled):
        return JSONResponse({"detail": "unexpected host"}, status_code=403)
    if request.method != "GET" and not same_origin(request.headers.get("origin"), request.headers.get("host")):
        return JSONResponse({"detail": "forbidden origin"}, status_code=403)
    path = request.url.path
    if path not in PUBLIC_PATHS and not signed_in(request.cookies):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "sign in required"}, status_code=401)
        return RedirectResponse("/login")
    return await call_next(request)


@app.exception_handler(WorkspaceError)
async def workspace_error(request: Request, exc: WorkspaceError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(gitops.GitError)
async def git_error(request: Request, exc: gitops.GitError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# sign-in (remote access)

class Login(BaseModel):
    token: str


@app.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return remote.LOGIN_PAGE


@app.post("/api/login")
def login(body: Login, request: Request) -> JSONResponse:
    if config.remote.enabled and not remote.valid(body.token):
        raise HTTPException(401, "invalid token")
    response = JSONResponse({"ok": True})
    if config.remote.enabled:
        response.set_cookie(
            remote.COOKIE, body.token, max_age=30 * 86400, httponly=True, samesite="strict",
            secure=request.url.scheme == "https",
        )
    return response


@app.get("/api/remote")
def remote_info(request: Request) -> dict:
    local = request.client is not None and request.client.host in LOOPBACK
    info = {
        "enabled": config.remote.enabled,
        "port": config.port,
        "urls": [f"http://{ip}:{config.port}" for ip in remote.addresses()] if config.remote.enabled else [],
        "ntfy": notifier.enabled,
    }
    if config.remote.enabled and local:
        info["token"] = remote.token()  # only shown on this computer
    return info


class OpenUrl(BaseModel):
    url: str


def web_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


@app.post("/api/open-url")
def open_url(body: OpenUrl, request: Request) -> dict:
    """Open a link from the app in the default browser, so the app window itself never navigates away."""
    if not web_url(body.url):
        raise HTTPException(400, "only http and https links can be opened")
    if request.client is None or request.client.host not in LOOPBACK:
        raise HTTPException(403, "links open on this computer only; open it on your device instead")
    launcher.open_app(body.url, tab=True)
    return {"ok": True}


@app.post("/api/notify/test")
async def test_notification() -> dict:
    if not notifier.enabled:
        raise HTTPException(400, "set notifications.ntfy_topic in config.yaml first")
    await notifier.send("Personal Agent", "Test notification. Phone alerts are working.")
    return {"ok": True}


# repos, auth & usage

@app.get("/api/repos")
def list_repos() -> list[dict]:
    return [{"name": r.name, "path": str(r.path), "access": r.access.value, "github": r.github} for r in config.repos]


@app.get("/api/auth")
async def auth_status() -> dict:
    """Which account agent runs bill to, as reported by Claude Code itself."""
    status: dict = {"billing": config.billing}
    claude = shutil.which("claude")
    if not claude:
        return {**status, "error": "claude CLI not found on PATH"}
    proc = await asyncio.create_subprocess_exec(
        claude, "auth", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    try:
        info = json.loads(out)
    except ValueError:
        return {**status, "error": out.decode(errors="replace").strip()[:200]}
    keep = ("loggedIn", "authMethod", "subscriptionType", "email", "orgName")
    return {**status, **{k: info.get(k) for k in keep},
            "api_key_in_env": any(os.environ.get(v) for v in API_CREDENTIAL_VARS)}


@app.get("/api/usage")
def usage() -> dict:
    now = time.time()
    return {
        "last_5h": store.usage_since(now - 5 * 3600),
        "last_7d": store.usage_since(now - 7 * 86400),
        "windows": bus.rate_limits,
    }


# admin: apply changes to this app

def _busy() -> dict:
    return {"chats": len(chats.busy()), "tasks": len(tasks.running), "reviews": len(reviews_running)}


@app.get("/api/admin")
def admin_status() -> dict:
    return {**admin.status(), "busy": _busy()}


@app.post("/api/admin/rebuild")
async def admin_rebuild() -> dict:
    if admin.building:
        raise HTTPException(409, "a build is already running")
    asyncio.create_task(admin.rebuild_ui())
    return {"started": True}


@app.post("/api/admin/restart")
async def admin_restart(force: bool = False) -> dict:
    busy = _busy()
    if any(busy.values()) and not force:
        raise HTTPException(409, f"agents are running ({busy}); wait or force the restart")
    try:
        await admin.restart()
    except AdminError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"restarting": True}


@app.post("/api/admin/shutdown")
async def admin_shutdown(force: bool = False) -> dict:
    busy = _busy()
    if any(busy.values()) and not force:
        raise HTTPException(409, f"agents are running ({busy}); wait or force the shutdown")
    await admin.shutdown()
    return {"stopping": True}


# conversations

Mode = Literal["agent", "chat"]


class NewConversation(BaseModel):
    repo: str | None = None
    mode: Mode = "agent"


class EditMessage(BaseModel):
    index: int  # which user message, counting from 0
    text: str


class ConversationPatch(BaseModel):
    title: str | None = None
    repo: str | None = None
    unlink: bool = False
    mode: Mode | None = None


def _check_repo(name: str | None) -> None:
    if name is not None:
        try:
            config.repo(name)
        except KeyError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/api/conversations")
def list_conversations(repo: str | None = None, q: str | None = None) -> list[dict]:
    busy = set(chats.busy())
    return [{**c, "busy": c["id"] in busy} for c in store.conversations(repo, q)]


@app.post("/api/conversations")
def create_conversation(body: NewConversation) -> dict:
    _check_repo(body.repo)
    return store.create_conversation(body.repo, body.mode)


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: str) -> dict:
    conv = store.conversation(conv_id) or _not_found("conversation")
    events = store.events(conversation_id=conv_id)
    # Mark turns that were undone, so the transcript doesn't offer Undo twice after a reload.
    restored = checkpoints.restored_ids([e["id"] for e in events if e["type"] == "checkpoint"])
    for e in events:
        if e["type"] == "checkpoint":
            e["restored"] = e["id"] in restored
    return {**conv, "busy": conv_id in chats.busy(), "events": events}


@app.patch("/api/conversations/{conv_id}")
async def patch_conversation(conv_id: str, body: ConversationPatch) -> dict:
    conv = store.conversation(conv_id) or _not_found("conversation")
    fields = {}
    if body.title:
        fields["title"] = body.title[:120]
    if body.mode and body.mode != conv["mode"]:
        if conv_id in chats.busy():
            raise HTTPException(409, "wait for the current turn to finish before switching modes")
        fields["mode"] = body.mode
    new_repo = None if body.unlink else body.repo
    if body.unlink or (body.repo is not None and body.repo != conv["repo"]):
        if conv_id in chats.busy():
            raise HTTPException(409, "wait for the current turn to finish before changing the repo")
        _check_repo(new_repo)
        # Claude sessions are tied to their working directory, so a repo change starts a new
        # session; the next turn gets a recap of the conversation instead.
        fields |= {"repo": new_repo, "session_id": None}
    conv = store.update_conversation(conv_id, **fields)
    await bus.publish({"type": "conversation_updated", "conversation": conv})
    return conv


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str) -> dict:
    chats.stop(conv_id)
    store.delete_conversation(conv_id)
    await bus.publish({"type": "conversation_deleted", "id": conv_id})
    return {"ok": True}


@app.post("/api/conversations/{conv_id}/edit")
async def edit_message(conv_id: str, body: EditMessage) -> dict:
    """Replace a user message: discard it and everything after (undoing file changes), then run the new text."""
    store.conversation(conv_id) or _not_found("conversation")
    if conv_id in chats.busy():
        raise HTTPException(409, "wait for the current turn to finish before editing a message")
    if not body.text.strip():
        raise HTTPException(400, "the message is empty")
    try:
        chats.edit(conv_id, body.index, body.text)
    except (IndexError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/checkpoints/{checkpoint_id}/restore")
async def restore_checkpoint(checkpoint_id: str) -> dict:
    try:
        files = checkpoints.restore(checkpoint_id, chats.checkpoint_roots())
    except CheckpointError as exc:
        raise HTTPException(400, str(exc)) from exc
    await bus.publish({"type": "checkpoint_restored", "id": checkpoint_id, "files": files})
    return {"restored": files}


# tasks

class MergeBody(BaseModel):
    message: str | None = None


@app.get("/api/tasks")
def list_tasks(repo: str | None = None) -> list[dict]:
    return store.tasks(repo)


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    task = store.task(task_id) or _not_found("task")
    return {**task, "events": store.events(task_id=task_id)}


@app.post("/api/tasks/{task_id}/stop")
def stop_task(task_id: str) -> dict:
    return {"stopped": tasks.stop(task_id)}


@app.post("/api/tasks/{task_id}/merge")
async def merge_task(task_id: str, body: MergeBody) -> dict:
    store.task(task_id) or _not_found("task")
    try:
        return {"sha": await tasks.merge(task_id, body.message)}
    except (RuntimeError, gitops.GitError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/tasks/{task_id}/pr")
async def open_pr(task_id: str) -> dict:
    store.task(task_id) or _not_found("task")
    try:
        return {"url": await tasks.open_pr(task_id)}
    except (RuntimeError, gitops.GitError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/tasks/{task_id}")
async def discard_task(task_id: str) -> dict:
    store.task(task_id) or _not_found("task")
    await tasks.discard(task_id)
    return {"ok": True}


# workspace files

class FileWrite(BaseModel):
    content: str


@app.get("/api/fs/tree")
async def tree(repo: str, task: str | None = None) -> dict:
    ws = get_workspace(config, store, repo, task)
    return {"root": str(ws.path), "access": ws.access.value, "files": await list_files(ws)}


@app.get("/api/fs/file")
def get_file(repo: str, path: str, task: str | None = None) -> dict:
    return read_file(get_workspace(config, store, repo, task), path)


@app.put("/api/fs/file")
def put_file(repo: str, path: str, body: FileWrite, task: str | None = None) -> dict:
    write_file(get_workspace(config, store, repo, task), path, body.content)
    return {"ok": True}


@app.get("/api/fs/search")
async def search_files(repo: str, q: str, regex: bool = False, case: bool = False, task: str | None = None) -> dict:
    try:
        return await search(get_workspace(config, store, repo, task), q, regex, case)
    except ValueError as exc:  # bad regular expression
        raise HTTPException(400, f"invalid pattern: {exc}") from exc


# workspace git

class Paths(BaseModel):
    paths: list[str]


class CommitBody(BaseModel):
    message: str


@app.get("/api/git/status")
async def git_status(repo: str, task: str | None = None) -> dict:
    ws = get_workspace(config, store, repo, task)
    if not await gitops.is_repo(ws.path):
        return {"branch": "", "files": [], "is_repo": False}
    return {**await gitops.status(ws.path), "is_repo": True, "access": ws.access.value}


@app.get("/api/git/diff")
async def git_diff(repo: str, path: str, staged: bool = False, task: str | None = None) -> dict:
    ws = get_workspace(config, store, repo, task)
    ws.resolve(path)
    return await gitops.diff_sides(ws.path, path, staged)


@app.post("/api/git/{action}")
async def git_action(action: str, repo: str, body: Paths, task: str | None = None) -> dict:
    ws = get_workspace(config, store, repo, task)
    ws.require_write()
    for p in body.paths:
        ws.resolve(p)
    ops = {"stage": gitops.stage, "unstage": gitops.unstage, "discard": gitops.discard}
    if action not in ops:
        raise HTTPException(404, f"unknown action {action}")
    await ops[action](ws.path, body.paths)
    return {"ok": True}


@app.post("/api/git-commit")
async def git_commit(repo: str, body: CommitBody, task: str | None = None) -> dict:
    ws = get_workspace(config, store, repo, task)
    ws.require_write()
    if not body.message.strip():
        raise HTTPException(400, "commit message is empty")
    return {"sha": await gitops.commit(ws.path, body.message.strip())}


@app.get("/api/review")
def get_review(repo: str, task: str | None = None) -> dict:
    ws = get_workspace(config, store, repo, task)
    return {"review": store.review(ws.key), "running": ws.key in reviews_running}


@app.post("/api/review")
async def start_review(repo: str, task: str | None = None) -> dict:
    ws = get_workspace(config, store, repo, task)
    if ws.key in reviews_running:
        raise HTTPException(409, "a review is already running")

    async def run() -> None:
        reviews_running.add(ws.key)
        try:
            await review_workspace(ws, config, store, bus)
        except Exception as exc:
            await bus.publish({"type": "review_failed", "workspace": ws.key, "message": str(exc)})
        finally:
            reviews_running.discard(ws.key)

    asyncio.create_task(run())
    return {"started": True}


# live events

@app.websocket("/api/ws")
async def events(ws: WebSocket) -> None:
    """Client sends {type: chat|stop|approve, ...}; server pushes every event to every client."""
    # Browsers don't apply CORS to WebSockets, so any open tab could otherwise drive the agent.
    host = ws.headers.get("host")
    if (not host_allowed(host, config.remote.enabled) or not same_origin(ws.headers.get("origin") or "missing", host)
            or not signed_in(ws.cookies)):
        await ws.close(code=1008)
        return
    await ws.accept()
    await ws.send_json({"type": "hello", **admin.status()})
    await bus.connect(ws)
    try:
        while True:
            try:
                msg = await ws.receive_json()
                kind = msg.get("type")
                if kind == "chat":
                    if not chats.start(msg["conversation_id"], str(msg["prompt"])):
                        await ws.send_json({"type": "error", "conversation_id": msg["conversation_id"],
                                            "message": "conversation is busy, missing, or the message is empty"})
                elif kind == "stop":
                    chats.stop(msg["conversation_id"])
                elif kind == "approve":
                    bus.resolve(msg["id"], bool(msg["allow"]))
            except (KeyError, TypeError, AttributeError, ValueError):
                # A malformed message is answered, not fatal to the connection.
                await ws.send_json({"type": "error", "message": "malformed message"})
    except WebSocketDisconnect:
        pass
    finally:
        bus.clients.discard(ws)


def _not_found(what: str):
    raise HTTPException(404, f"{what} not found")


DIST = PROJECT_ROOT / "frontend" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="ui")
