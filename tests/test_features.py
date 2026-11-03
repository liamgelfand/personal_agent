import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent import gitops
from personal_agent.admin import classify
from personal_agent.chat import attach_mentions
from personal_agent.checkpoints import Checkpoint, CheckpointError, Checkpoints
from personal_agent.config import PROJECT_ROOT, Access, Repo
from personal_agent.store import Store
from personal_agent.workspace import Workspace, search


def sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "config", "user.email", "t@example.com")
    sh(root, "git", "config", "user.name", "t")
    sh(root, "git", "config", "core.autocrlf", "false")
    (root / "app.py").write_text("def hello():\n    return 'Hello'\n", newline="")
    sh(root, "git", "add", ".")
    sh(root, "git", "commit", "-q", "-m", "init")
    return root


# checkpoints

async def run_edit(cp: Checkpoint, tool_use_id: str, path: Path, new: str | None, succeed: bool = True) -> None:
    hooks = cp.hooks()
    data = {"tool_name": "Write", "tool_input": {"file_path": str(path)}}
    await hooks["PreToolUse"][0].hooks[0](data, tool_use_id, None)
    if succeed:
        path.write_text(new, newline="")
        await hooks["PostToolUse"][0].hooks[0](data, tool_use_id, None)


async def test_checkpoint_restores_edits_and_removes_created_files(tmp_path, repo):
    cps = Checkpoints(Store(tmp_path / "t.db"))
    cp = Checkpoint(repo)
    await run_edit(cp, "1", repo / "app.py", "changed\n")
    await run_edit(cp, "2", repo / "app.py", "changed twice\n")  # original from the first edit is kept
    await run_edit(cp, "3", repo / "new.py", "x\n")
    saved = cps.save(cp, "conv")
    assert sorted(Path(f).name for f in saved["files"]) == ["app.py", "new.py"]

    cps.restore(saved["id"], [repo.resolve()])
    assert (repo / "app.py").read_text() == "def hello():\n    return 'Hello'\n"
    assert not (repo / "new.py").exists()
    with pytest.raises(CheckpointError):
        cps.restore(saved["id"], [repo.resolve()])


async def test_checkpoint_ignores_denied_edits(tmp_path, repo):
    cp = Checkpoint(repo)
    await run_edit(cp, "1", repo / "app.py", None, succeed=False)
    assert Checkpoints(Store(tmp_path / "t.db")).save(cp, "conv") is None


async def test_checkpoint_skips_paths_outside_allowed_roots(tmp_path, repo):
    cps = Checkpoints(Store(tmp_path / "t.db"))
    cp = Checkpoint(repo)
    await run_edit(cp, "1", repo / "app.py", "changed\n")
    saved = cps.save(cp, "conv")
    assert cps.restore(saved["id"], [tmp_path / "elsewhere"]) == []
    assert (repo / "app.py").read_text() == "changed\n"


# mentions

def test_attach_mentions_includes_files_in_repo_only(repo, tmp_path):
    (tmp_path / "secret.txt").write_text("nope")
    out = attach_mentions("look at @app.py and @../secret.txt and email a@b.com", repo.resolve())
    assert '<file path="app.py">' in out and "return 'Hello'" in out
    assert "secret" not in out.split("Files the user mentioned:")[1]
    assert "b.com" not in out.split("Files the user mentioned:")[1]
    assert attach_mentions("no mentions", repo) == "no mentions"


# search

async def test_search_finds_matches_with_line_numbers(repo):
    (repo / "notes.md").write_text("say hello\n", newline="")  # untracked files are searched too
    ws = Workspace(Repo(name="r", path=repo, access=Access.WRITE), None, repo.resolve(), Access.WRITE)
    result = await search(ws, "hello")
    assert {(m["path"], m["line"]) for m in result["matches"]} == {("app.py", 1), ("app.py", 2), ("notes.md", 1)}
    assert [m["path"] for m in (await search(ws, "hello", case=True))["matches"]] == ["app.py", "notes.md"]
    assert (await search(ws, "h.llo\\(", regex=True))["matches"][0]["line"] == 1


async def test_search_includes_ignored_files_but_skips_heavy_dirs(repo):
    (repo / ".gitignore").write_text("config.yaml\n", newline="")
    (repo / "config.yaml").write_text("needle: 1\n", newline="")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "lib.js").write_text("needle\n", newline="")
    ws = Workspace(Repo(name="r", path=repo, access=Access.WRITE), None, repo.resolve(), Access.WRITE)
    assert [m["path"] for m in (await search(ws, "needle"))["matches"]] == ["config.yaml"]
    # One regex engine for tracked and ignored files alike.
    assert {m["path"] for m in (await search(ws, r"\d|(?=d)", regex=True))["matches"]} == {"app.py", "config.yaml"}


async def test_search_rejects_bad_regex(repo):
    ws = Workspace(Repo(name="r", path=repo, access=Access.WRITE), None, repo.resolve(), Access.WRITE)
    with pytest.raises(ValueError):
        await search(ws, "foo(", regex=True)


async def test_search_skips_symlinks_outside_workspace(repo, tmp_path):
    import re

    from personal_agent.workspace import _search

    (tmp_path / "secret.txt").write_text("PRIVATE KEY\n", newline="")
    assert _search(repo, ["../secret.txt"], re.compile("PRIVATE"), True) == []  # same check, no symlink needed
    try:
        (repo / "leak").symlink_to(tmp_path / "secret.txt")
    except OSError:
        pytest.skip("symlinks not permitted")
    ws = Workspace(Repo(name="r", path=repo, access=Access.WRITE), None, repo.resolve(), Access.WRITE)
    assert (await search(ws, "PRIVATE KEY"))["matches"] == []


# merge

async def test_merge_task_branch(tmp_path, repo):
    from personal_agent.bus import Bus
    from personal_agent.config import Config
    from personal_agent.tasks import TaskManager

    store = Store(tmp_path / "t.db")
    config = Config(repos=[Repo(name="r", path=repo, access=Access.WRITE)])
    manager = TaskManager(config, store, Bus())
    wt = tmp_path / "wt"
    await gitops.add_worktree(repo, wt, "agent/x")
    store.create_task(id="t1", conversation_id=None, repo="r", title="Greet", spec="s", branch="agent/x",
                      base_sha=await gitops.head_sha(repo), worktree=str(wt))
    store.update_task("t1", status="ready")
    (wt / "app.py").write_text("def hello():\n    return 'Hi'\n", newline="")

    with pytest.raises(RuntimeError, match="commit message"):
        await manager.merge("t1", None)
    await manager.merge("t1", "Change greeting")
    assert "'Hi'" in (repo / "app.py").read_text()
    assert not wt.exists() and store.task("t1")["status"] == "merged"


async def test_failed_merge_leaves_checkout_unchanged(tmp_path, repo):
    from personal_agent.bus import Bus
    from personal_agent.config import Config
    from personal_agent.tasks import TaskManager

    store = Store(tmp_path / "t.db")
    manager = TaskManager(Config(repos=[Repo(name="r", path=repo, access=Access.WRITE)]), store, Bus())
    wt = tmp_path / "wt"
    await gitops.add_worktree(repo, wt, "agent/y")
    store.create_task(id="t2", conversation_id=None, repo="r", title="Conflict", spec="s", branch="agent/y",
                      base_sha=await gitops.head_sha(repo), worktree=str(wt))
    store.update_task("t2", status="ready")
    (wt / "app.py").write_text("task side\n", newline="")
    (repo / "app.py").write_text("main side\n", newline="")
    sh(repo, "git", "commit", "-qam", "main change")

    with pytest.raises(RuntimeError, match="aborted"):
        await manager.merge("t2", "Task change")
    assert (repo / "app.py").read_text() == "main side\n"
    assert (await gitops.status(repo))["files"] == []


# finishing a task: commit, push, PR

@pytest.fixture
def finishing(tmp_path, repo, monkeypatch):
    """A task worktree whose origin looks like GitHub but pushes to a local bare repo; gh is faked."""
    from personal_agent import tasks as tasks_mod
    from personal_agent.bus import Bus
    from personal_agent.config import Config

    bare = tmp_path / "origin.git"
    sh(tmp_path, "git", "init", "-q", "--bare", str(bare))
    sh(repo, "git", "remote", "add", "origin", "https://github.com/octo/demo.git")
    sh(repo, "git", "config", "remote.origin.pushurl", str(bare))
    sh(repo, "git", "push", "-q", "origin", "main")
    # As in a clone; without it default_branch would ask the (fake GitHub) origin over the network.
    sh(repo, "git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    gh_calls: list[tuple[str, ...]] = []
    gh_replies: dict[str, object] = {"list": "", "create": "https://github.com/octo/demo/pull/7"}

    async def fake_gh(cwd, *args):
        gh_calls.append(args)
        reply = gh_replies[args[1]]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(tasks_mod, "_gh", fake_gh)
    store = Store(tmp_path / "t.db")
    manager = tasks_mod.TaskManager(Config(repos=[Repo(name="r", path=repo, access=Access.WRITE)]), store, Bus())

    async def add(task_id: str, branch: str, spec: str = "Do it.\n\n## Verify\n- run tests", **fields):
        wt = tmp_path / "wt" / task_id
        await gitops.add_worktree(repo, wt, branch)
        store.create_task(id=task_id, conversation_id=None, repo="r", title="Change greeting", spec=spec,
                          branch=branch, base_sha=await gitops.head_sha(repo), worktree=str(wt))
        store.update_task(task_id, status="ready", **fields)
        return wt

    def pushed(branch: str) -> bool:
        return subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                              capture_output=True).returncode == 0

    return SimpleNamespace(manager=manager, store=store, add=add, pushed=pushed, gh_calls=gh_calls,
                           gh_replies=gh_replies, repo=repo)


async def test_finish_commits_pushes_and_opens_pr(finishing):
    f = finishing
    wt = await f.add("t1", "agent/greet")
    (wt / "app.py").write_text("def hello():\n    return 'Hi'\n", newline="")
    f.store.save_review("r@t1", {"verdict": "approve", "summary": "Looks good", "findings": []})

    task = await f.manager.finish("t1")
    assert (task["pr_url"], task["pr_state"], task["finish_error"]) == ("https://github.com/octo/demo/pull/7", "open", None)
    assert task["status"] == "ready" and f.pushed("agent/greet")
    assert (await gitops.status(wt))["files"] == []
    message = await gitops.git(wt, "log", "-1", "--format=%B")
    assert message.startswith("Change greeting\n\nTask t1\n")
    assert message.strip().endswith("Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>")
    create = next(c for c in f.gh_calls if c[1] == "create")
    assert create[create.index("--base") + 1] == "main" and create[create.index("--head") + 1] == "agent/greet"
    body = create[create.index("--body") + 1]
    assert "Do it." in body and "Looks good (0 findings)" in body and "How to test" in body
    assert body.endswith("🤖 Generated with [Claude Code](https://claude.com/claude-code)")
    finished = [e for e in f.store.events(task_id="t1") if e["type"] == "finished"]
    assert finished and finished[0]["url"] == task["pr_url"]


async def test_finish_falls_back_to_compare_url_when_gh_fails(finishing):
    f = finishing
    f.gh_replies["list"] = RuntimeError("To get started with GitHub CLI, please run: gh auth login")
    wt = await f.add("t2", "agent/greet-2", spec="No verify here")
    (wt / "app.py").write_text("changed\n", newline="")

    task = await f.manager.finish("t2")
    assert task["pr_url"] == (
        "https://github.com/octo/demo/compare/main...agent/greet-2?expand=1&title=Change%20greeting"
    )
    assert task["pr_state"] == "compare" and "gh auth login" in task["finish_error"]
    assert task["status"] == "ready" and f.pushed("agent/greet-2")
    assert any(e["type"] == "warning" and "gh auth login" in e["message"] for e in f.store.events(task_id="t2"))
    # The Open PR button retries gh, and without auth lands on the compare page again.
    calls = len(f.gh_calls)
    assert await f.manager.open_pr("t2") == task["pr_url"]
    assert len(f.gh_calls) == calls + 1


async def test_finish_does_not_open_duplicate_prs(finishing):
    f = finishing
    wt = await f.add("t3", "agent/done", pr_url="https://github.com/octo/demo/pull/1", pr_state="open")
    (wt / "app.py").write_text("changed\n", newline="")
    task = await f.manager.finish("t3")
    assert task["pr_url"] == "https://github.com/octo/demo/pull/1"
    assert f.gh_calls == [] and not f.pushed("agent/done")

    # A retried task whose PR gh already knows about reuses it.
    f.gh_replies["list"] = "https://github.com/octo/demo/pull/2"
    wt = await f.add("t4", "agent/again", pr_url="https://github.com/octo/demo/compare/x", pr_state="compare")
    (wt / "app.py").write_text("changed\n", newline="")
    task = await f.manager.finish("t4")
    assert (task["pr_url"], task["pr_state"]) == ("https://github.com/octo/demo/pull/2", "open")
    assert not any(c[1] == "create" for c in f.gh_calls)


async def test_finish_never_pushes_the_default_branch(finishing):
    f = finishing
    sh(f.repo, "git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/release")
    wt = await f.add("t5", "release")
    (wt / "app.py").write_text("changed\n", newline="")
    task = await f.manager.finish("t5")
    assert task["pr_url"] is None and "refusing to push the default branch" in task["finish_error"]
    assert task["status"] == "ready" and not f.pushed("release") and f.gh_calls == []
    with pytest.raises(RuntimeError, match="refusing"):
        await f.manager.open_pr("t5")


async def test_finish_refuses_secret_like_new_files(finishing):
    f = finishing
    wt = await f.add("t6", "agent/secret")
    (wt / "app.py").write_text("changed\n", newline="")
    (wt / "config").mkdir()
    (wt / "config" / ".env").write_text("TOKEN=x\n")
    task = await f.manager.finish("t6")
    assert task["pr_url"] is None and "config/.env" in task["finish_error"]
    assert not f.pushed("agent/secret") and (await gitops.head_sha(wt)) == task["base_sha"]  # nothing committed


async def test_finish_needs_an_origin_remote(finishing):
    f = finishing
    sh(f.repo, "git", "remote", "remove", "origin")
    wt = await f.add("t7", "agent/no-origin")
    (wt / "app.py").write_text("changed\n", newline="")
    task = await f.manager.finish("t7")
    assert "no 'origin' remote" in task["finish_error"]


async def test_concurrent_finish_runs_open_one_pr(finishing, monkeypatch):
    from personal_agent import tasks as tasks_mod

    f = finishing
    created = []

    async def slow_gh(cwd, *args):
        await asyncio.sleep(0.05)
        if args[1] == "list":
            return created[-1] if created else ""
        created.append("https://github.com/octo/demo/pull/9")
        return created[-1]

    monkeypatch.setattr(tasks_mod, "_gh", slow_gh)
    wt = await f.add("t8", "agent/twice")
    (wt / "app.py").write_text("changed\n", newline="")
    first, second = await asyncio.gather(f.manager.open_pr("t8"), f.manager.open_pr("t8"))
    assert first == second == "https://github.com/octo/demo/pull/9" and len(created) == 1
    task = f.store.task("t8")
    assert (task["pr_state"], task["finish_error"]) == ("open", None)


async def test_open_pr_with_open_findings_is_a_draft_listing_them(finishing):
    f = finishing
    wt = await f.add("t9", "agent/draft")
    (wt / "app.py").write_text("changed\n", newline="")
    f.store.save_review("r@t9", {"verdict": "changes_requested", "summary": "Not yet", "findings": [
        {"severity": "blocking", "file": "app.py", "line": 1, "title": "Breaks hello", "detail": "returns wrong"},
    ]})
    await f.manager.open_pr("t9")
    create = next(c for c in f.gh_calls if c[1] == "create")
    assert "--draft" in create
    body = create[create.index("--body") + 1]
    assert "still has open findings" in body and "[blocking] app.py:1 Breaks hello" in body


async def test_merge_and_discard_wait_for_a_running_finish(finishing, monkeypatch):
    from personal_agent import tasks as tasks_mod

    f = finishing
    order = []

    async def slow_gh(cwd, *args):
        await asyncio.sleep(0.1)
        order.append(f"gh {args[1]}")
        return "" if args[1] == "list" else "https://github.com/octo/demo/pull/3"

    monkeypatch.setattr(tasks_mod, "_gh", slow_gh)
    for task_id, branch in (("m", "agent/merge-me"), ("d", "agent/discard-me")):
        wt = await f.add(task_id, branch)
        (wt / "app.py").write_text("changed\n", newline="")
    finishing_m = asyncio.create_task(f.manager.open_pr("m"))
    finishing_d = asyncio.create_task(f.manager.open_pr("d"))
    await asyncio.sleep(0.01)
    await asyncio.gather(f.manager.merge("m", None), f.manager.discard("d"))
    await asyncio.gather(finishing_m, finishing_d)
    for task_id, status in (("m", "merged"), ("d", "discarded")):
        task = f.store.task(task_id)
        assert task["status"] == status and task["pr_url"] == "https://github.com/octo/demo/pull/3", task_id
    assert order.count("gh create") == 2


async def test_finish_after_merge_does_nothing(finishing):
    f = finishing
    wt = await f.add("t10", "agent/late")
    (wt / "app.py").write_text("changed\n", newline="")
    await f.manager.merge("t10", "Merge it")
    task = await f.manager.finish("t10")
    assert "was merged" in task["finish_error"] and f.gh_calls == [] and not f.pushed("agent/late")


async def test_default_branch_detection(tmp_path, repo):
    assert await gitops.default_branch(repo) == "main"  # no origin at all: fall back
    sh(repo, "git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert await gitops.default_branch(repo) == "trunk"

    # A repo that was git init'ed and pushed has no origin/HEAD: ask origin.
    local = tmp_path / "local"
    local.mkdir()
    bare = tmp_path / "remote.git"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "develop", str(bare))
    sh(local, "git", "init", "-q", "-b", "develop")
    sh(local, "git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    sh(local, "git", "remote", "add", "origin", str(bare))
    sh(local, "git", "push", "-q", "-u", "origin", "develop")
    assert await gitops.default_branch(local) == "develop"

    # Origin unreachable: a pushed master still beats the 'main' guess.
    sh(local, "git", "branch", "-q", "-m", "develop", "master")
    sh(local, "git", "push", "-q", "-u", "origin", "master")
    sh(local, "git", "update-ref", "-d", "refs/remotes/origin/develop")
    sh(local, "git", "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert await gitops.default_branch(local) == "master"


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/octo/demo.git", ("octo", "demo")),
    ("https://github.com/octo/demo", ("octo", "demo")),
    ("https://user:token@github.com/octo/demo.git/", ("octo", "demo")),
    ("git@github.com:octo/my.repo.git", ("octo", "my.repo")),
    ("ssh://git@github.com/octo/demo.git", ("octo", "demo")),
    ("ssh://git@github.com:22/octo/demo", ("octo", "demo")),
    ("https://gitlab.com/octo/demo.git", None),
    ("C:/repos/origin.git", None),
])
def test_github_repo_from_remote_url(url, expected):
    assert gitops.github_repo(url) == expected


# origin checks and self-change detection

def test_same_origin():
    from personal_agent.server import same_origin

    assert same_origin("http://127.0.0.1:8000", "127.0.0.1:8000")
    assert same_origin("http://100.64.0.5:8000", "100.64.0.5:8000")
    assert same_origin("http://localhost:5173", "localhost:5173")
    assert same_origin(None, "127.0.0.1:8000")
    assert not same_origin("http://evil.example", "127.0.0.1:8000")
    assert not same_origin("missing", "127.0.0.1:8000")


def test_classify_self_changes():
    assert classify(PROJECT_ROOT / "src" / "personal_agent" / "chat.py") == "server"
    assert classify(PROJECT_ROOT / "frontend" / "src" / "App.tsx") == "ui"
    assert classify(PROJECT_ROOT / "agents" / "reviewer.md") is None  # read on every run, no restart
    assert classify(PROJECT_ROOT / "README.md") is None


def test_host_allowed_blocks_dns_rebinding():
    from personal_agent.server import host_allowed

    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "localhost:5173"):
        assert host_allowed(host, remote_enabled=False), host
    for host in ("evil.example:8000", "100.64.0.5:8000", None, ""):
        assert not host_allowed(host, remote_enabled=False), host
    assert host_allowed("100.64.0.5:8000", remote_enabled=True)  # remote mode relies on the sign-in token


async def test_discard_renamed_file_restores_original(repo):
    sh(repo, "git", "mv", "app.py", "main.py")
    (repo / "main.py").write_text("changed\n", newline="")
    await gitops.discard(repo, ["main.py"])
    assert (repo / "app.py").read_text() == "def hello():\n    return 'Hello'\n"
    assert not (repo / "main.py").exists()
    assert (await gitops.status(repo))["files"] == []


async def test_discard_untracked_symlink_keeps_target(repo):
    import os

    try:
        os.symlink(repo / "app.py", repo / "link.py")
    except OSError:
        pytest.skip("symlinks need extra privileges on this system")
    (repo / "app.py").write_text("uncommitted edit\n", newline="")
    await gitops.discard(repo, ["link.py"])
    assert not (repo / "link.py").is_symlink()
    assert (repo / "app.py").read_text() == "uncommitted edit\n"


async def test_restored_checkpoints_are_reported(tmp_path, repo):
    cps = Checkpoints(Store(tmp_path / "t.db"))
    cp = Checkpoint(repo)
    await run_edit(cp, "1", repo / "app.py", "changed\n")
    saved = cps.save(cp, "conv")
    assert cps.restored_ids([saved["id"]]) == set()
    cps.restore(saved["id"], [repo.resolve()])
    assert cps.restored_ids([saved["id"], "other"]) == {saved["id"]}


# links open in the default browser

def test_open_url_accepts_web_links_only(monkeypatch):
    from fastapi import HTTPException

    from personal_agent import server

    local, phone = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")), SimpleNamespace(client=SimpleNamespace(host="100.64.0.5"))
    opened = []
    monkeypatch.setattr(server.launcher, "open_app", lambda url, tab=False: opened.append((url, tab)))
    assert server.open_url(server.OpenUrl(url="https://example.com/a?b=1"), local) == {"ok": True}
    assert server.open_url(server.OpenUrl(url="HTTP://example.com"), local)
    for url in ("javascript:alert(1)", "file:///C:/Windows/win.ini", "ms-settings:", "http:no-host", "/relative", ""):
        with pytest.raises(HTTPException) as exc:
            server.open_url(server.OpenUrl(url=url), local)
        assert exc.value.status_code == 400, url
    with pytest.raises(HTTPException) as exc:  # a phone must not open tabs on this computer
        server.open_url(server.OpenUrl(url="https://example.com"), phone)
    assert exc.value.status_code == 403
    assert opened == [("https://example.com/a?b=1", True), ("HTTP://example.com", True)]


def test_open_url_rejects_cross_origin_posts(monkeypatch):
    from fastapi.testclient import TestClient

    from personal_agent import server

    opened = []
    monkeypatch.setattr(server.launcher, "open_app", lambda url, tab=False: opened.append(url))
    client = TestClient(server.app, client=("127.0.0.1", 50000))
    host = "127.0.0.1:8000"  # an allowed host, so only the Origin decides
    response = client.post("/api/open-url", json={"url": "https://example.com"},
                           headers={"Host": host, "Origin": "https://evil.example"})
    assert response.status_code == 403 and response.json()["detail"] == "forbidden origin"
    assert opened == []
    response = client.post("/api/open-url", json={"url": "https://example.com"},
                           headers={"Host": host, "Origin": f"http://{host}"})
    assert response.status_code == 200 and opened == ["https://example.com"]


# editing a message rewinds the conversation

REJECTION = "Resume rejected by --resume-drops-turn: entries after the fork point belong to another turn"


class FakeAgent:
    """Stands in for run_agent: a session, a reply and a result per turn. No network."""

    def __init__(self):
        self.calls: list[tuple] = []  # (options, prompt, prompt_uuid) per chat turn
        self.writes: dict[int, tuple[Path, str]] = {}  # turn number -> file that turn writes
        self.reject_forks: str | None = None  # "raise" or "result": how the CLI refuses a fork
        self.hang = False  # stop after the first message, until cancelled

    async def __call__(self, options, prompt, store, bus, kind, ref, prompt_uuid=None):
        if kind == "title":
            yield {"type": "result", "result": None, "is_error": False}
            return
        self.calls.append((options, prompt, prompt_uuid))
        n = len(self.calls)
        if self.reject_forks and options.fork_session:
            if self.reject_forks == "raise":
                from claude_agent_sdk import ProcessError

                raise ProcessError(REJECTION, exit_code=1)
            yield {"type": "result", "is_error": True, "num_turns": 0, "cost_usd": 0.0, "errors": [REJECTION],
                   "session_id": "", "result": None}
            return
        if n in self.writes:
            await write_through_hooks(options, f"tool-{n}", *self.writes[n])
        yield {"type": "session", "session_id": f"s{n}" if options.fork_session else options.resume or f"s{n}"}
        if self.hang:
            await asyncio.Event().wait()
        yield {"type": "text", "agent": "main", "text": f"reply {n}"}
        yield {"type": "result", "is_error": False, "num_turns": 1, "cost_usd": 0.0, "errors": None,
               "session_id": f"s{n}" if options.fork_session else options.resume or f"s{n}"}


async def write_through_hooks(options, tool_use_id: str, path: Path, text: str) -> None:
    from personal_agent.checkpoints import FILE_TOOLS

    data = {"tool_name": "Write", "tool_input": {"file_path": str(path)}}
    for event in ("PreToolUse", "PostToolUse"):
        for matcher in options.hooks[event]:
            if matcher.matcher == FILE_TOOLS:
                await matcher.hooks[0](data, tool_use_id, None)
        if event == "PreToolUse":
            path.write_text(text, newline="")


@pytest.fixture
def chat(tmp_path, repo, monkeypatch):
    from personal_agent import chat as chat_module
    from personal_agent import checkpoints as checkpoints_module
    from personal_agent.bus import Bus
    from personal_agent.config import Config

    monkeypatch.setattr(checkpoints_module, "CHECKPOINT_DIR", tmp_path / "checkpoints")
    agent = FakeAgent()
    monkeypatch.setattr(chat_module, "run_agent", agent)
    store = Store(tmp_path / "t.db")
    config = Config(repos=[Repo(name="r", path=repo, access=Access.WRITE)])
    manager = chat_module.ChatManager(config, store, Bus(), None, Checkpoints(store))
    return manager, store, agent, store.create_conversation("r", "agent")["id"]


def transcript_entry(u: str) -> SimpleNamespace:
    """A transcript message: prompts are our UUID4s, "r..." a tool result, anything else the assistant."""
    if len(u) == 36:
        return SimpleNamespace(uuid=u, type="user", message={"role": "user", "content": "a prompt"})
    if u.startswith("r"):
        return SimpleNamespace(uuid=u, type="user", message={
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]})
    return SimpleNamespace(uuid=u, type="assistant", message={"role": "assistant", "content": []})


def fake_transcript(monkeypatch, store, conv_id: str, sessions: dict[str, list[str]] | None = None) -> list[str]:
    """Point get_session_messages at a transcript: by default every turn so far in one session
    (prompt, tool call, tool result, reply...), or the given entry UUIDs per session."""
    from personal_agent import chat as chat_module

    prompts = [e["prompt_uuid"] for e in store.events(conversation_id=conv_id) if e["type"] == "user"]
    entries = [u for i, p in enumerate(prompts, 1) for u in (p, f"t{i}", f"r{i}", f"a{i}")]
    monkeypatch.setattr(
        chat_module, "get_session_messages",
        lambda session_id, directory=None: [
            transcript_entry(u) for u in (entries if sessions is None else sessions.get(session_id, []))
        ],
    )
    return prompts


async def finish(manager, conv_id: str) -> None:
    await manager.running[conv_id]
    await asyncio.sleep(0)  # let the title task finish


async def say(manager, conv_id: str, text: str) -> None:
    assert manager.start(conv_id, text)
    await finish(manager, conv_id)


def transcript(store, conv_id: str) -> list[tuple[str, str]]:
    return [(e["type"], e.get("text") or e.get("message")) for e in store.events(conversation_id=conv_id)
            if e["type"] in ("user", "text", "warning")]


async def test_turns_record_session_and_prompt_uuid(chat):
    manager, store, agent, conv_id = chat
    await say(manager, conv_id, "first")
    await say(manager, conv_id, "second")
    first, second = [e for e in store.events(conversation_id=conv_id) if e["type"] == "user"]
    assert first["session_id"] == "s1" and second["session_id"] == "s1"
    assert agent.calls[1][0].resume == "s1"
    assert [first["prompt_uuid"], second["prompt_uuid"]] == [agent.calls[0][2], agent.calls[1][2]]


async def test_edit_truncates_and_forks_the_session(chat, monkeypatch):
    manager, store, agent, conv_id = chat
    for text in ("first", "second", "third"):
        await say(manager, conv_id, text)
    prompts = fake_transcript(monkeypatch, store, conv_id)

    manager.edit(conv_id, 1, "second, edited")
    assert transcript(store, conv_id) == [("user", "first"), ("text", "reply 1")]  # dropped right away
    assert store.conversation(conv_id)["session_id"] is None
    await finish(manager, conv_id)

    options, prompt, _ = agent.calls[-1]
    assert prompt == "second, edited"  # the forked session has the context, so no recap
    assert (options.resume, options.fork_session, options.resume_session_at) == ("s1", True, "a1")
    # Two turns are discarded ("second" and "third"), which the CLI's one-turn guard would refuse.
    assert options.resume_drops_turn is None and prompts[2] != prompts[1]
    assert transcript(store, conv_id) == [
        ("user", "first"), ("text", "reply 1"), ("user", "second, edited"), ("text", "reply 4")]
    assert store.conversation(conv_id)["session_id"] == "s4"  # the fork's new session


async def test_edit_of_the_last_message_guards_the_dropped_turn(chat, monkeypatch):
    manager, store, agent, conv_id = chat
    await say(manager, conv_id, "first")
    await say(manager, conv_id, "second")
    prompts = fake_transcript(monkeypatch, store, conv_id)
    manager.edit(conv_id, 1, "second, edited")
    await finish(manager, conv_id)
    options = agent.calls[-1][0]
    assert (options.resume_session_at, options.resume_drops_turn) == ("a1", prompts[1])


async def test_edit_forks_from_the_session_the_message_went_to(chat, monkeypatch):
    manager, store, agent, conv_id = chat
    await say(manager, conv_id, "first")
    await say(manager, conv_id, "second")
    first_prompt, second_prompt = fake_transcript(monkeypatch, store, conv_id)
    manager.edit(conv_id, 1, "second, edited")  # forks s1 into s3
    await finish(manager, conv_id)
    await say(manager, conv_id, "third")  # resumes s3
    edited, third = [e for e in store.events(conversation_id=conv_id) if e["type"] == "user"][1:]
    assert edited["session_id"] == third["session_id"] == "s3"
    # The fork copied the kept history under new UUIDs; only its transcript knows them.
    fake_transcript(monkeypatch, store, conv_id, {
        "s1": [first_prompt, "a1", second_prompt, "a2"],
        "s3": ["c1", "c2", edited["prompt_uuid"], "a3", third["prompt_uuid"], "a4"],
    })
    manager.edit(conv_id, 2, "third, edited")
    await finish(manager, conv_id)
    options = agent.calls[-1][0]
    assert (options.resume, options.resume_session_at, options.resume_drops_turn) == ("s3", "a3", third["prompt_uuid"])


async def test_second_edit_counts_turns_an_earlier_edit_dropped(chat, monkeypatch):
    manager, store, agent, conv_id = chat
    for text in ("A", "B", "C"):
        await say(manager, conv_id, text)
    a, b, c = fake_transcript(monkeypatch, store, conv_id)
    manager.edit(conv_id, 2, "C'")  # forks s1 into s4; C is gone from our history but not from s1
    await finish(manager, conv_id)
    edited = store.events(conversation_id=conv_id)[-3]
    assert edited["text"] == "C'" and edited["session_id"] == "s4"
    fake_transcript(monkeypatch, store, conv_id, {
        "s1": [a, "a1", b, "a2", c, "a3"],
        "s4": ["c1", "c2", "c3", "c4", edited["prompt_uuid"], "a4"],
    })
    manager.edit(conv_id, 1, "B'")  # B still points at s1, where C's turn follows it
    await finish(manager, conv_id)
    options = agent.calls[-1][0]
    # Guarding the drop of B's turn alone would be refused (C follows), so truncate unguarded.
    assert (options.resume, options.resume_session_at, options.resume_drops_turn) == ("s1", "a1", None)
    assert agent.calls[-1][1] == "B'"  # forked, not recapped
    assert not any(e["type"] == "warning" for e in store.events(conversation_id=conv_id))


@pytest.mark.parametrize("how", ["raise", "result"])
async def test_edit_falls_back_to_recap_when_fork_is_rejected(chat, monkeypatch, how):
    manager, store, agent, conv_id = chat
    await say(manager, conv_id, "first")
    await say(manager, conv_id, "second")
    fake_transcript(monkeypatch, store, conv_id)
    agent.reject_forks = how

    manager.edit(conv_id, 1, "again")
    await finish(manager, conv_id)
    rejected, (options, prompt, _) = agent.calls[-2][0], agent.calls[-1]
    assert rejected.fork_session and rejected.resume == "s1"  # tried the fork first
    assert not options.fork_session and options.resume is None  # then a fresh session, not a plain resume
    assert prompt.startswith("Earlier in this conversation:") and "User: first" in prompt and "second" not in prompt
    events = store.events(conversation_id=conv_id)
    assert [e["type"] for e in events][-4:] == ["user", "warning", "text", "result"]  # no error, no rejected result
    assert events[-4]["session_id"] == "s4" and store.conversation(conv_id)["session_id"] == "s4"


async def test_edit_first_message_without_fork_point_starts_fresh(chat, monkeypatch):
    manager, store, agent, conv_id = chat
    await say(manager, conv_id, "first")
    fake_transcript(monkeypatch, store, conv_id)  # the prompt is the session's first entry: nothing to fork at
    manager.edit(conv_id, 0, "first, edited")
    await finish(manager, conv_id)
    options, prompt, _ = agent.calls[-1]
    assert prompt == "first, edited" and options.resume is None and not options.fork_session
    assert transcript(store, conv_id) == [("user", "first, edited"), ("text", "reply 2")]


async def test_edit_falls_back_when_transcript_is_missing(chat, monkeypatch):
    manager, store, agent, conv_id = chat
    await say(manager, conv_id, "first")
    await say(manager, conv_id, "second")
    fake_transcript(monkeypatch, store, conv_id, {})  # e.g. the repo changed and the session lives elsewhere
    manager.edit(conv_id, 1, "again")
    await finish(manager, conv_id)
    options, prompt, _ = agent.calls[-1]
    assert options.resume is None and not options.fork_session
    assert prompt.startswith("Earlier in this conversation:") and "second" not in prompt


async def until(condition) -> None:
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("timed out")


async def test_stopped_first_turn_keeps_its_session(chat):
    manager, store, agent, conv_id = chat
    agent.hang = True
    assert manager.start(conv_id, "first")
    await until(lambda: store.conversation(conv_id)["session_id"] == "s1")
    manager.stop(conv_id)
    await finish(manager, conv_id)
    user, stopped = store.events(conversation_id=conv_id)
    assert stopped["type"] == "stopped" and user["session_id"] == "s1"  # from the session event, not a result
    agent.hang = False
    await say(manager, conv_id, "go on")
    assert agent.calls[-1][0].resume == "s1" and agent.calls[-1][1] == "go on"


async def test_edit_a_stopped_message(chat, monkeypatch):
    manager, store, agent, conv_id = chat
    await say(manager, conv_id, "first")
    agent.hang = True
    assert manager.start(conv_id, "second")
    await until(lambda: len(agent.calls) == 2)
    await asyncio.sleep(0)
    manager.stop(conv_id)
    await finish(manager, conv_id)
    assert [e["type"] for e in store.events(conversation_id=conv_id)][-2:] == ["user", "stopped"]

    agent.hang = False
    prompts = fake_transcript(monkeypatch, store, conv_id)
    manager.edit(conv_id, 1, "second, edited")
    await finish(manager, conv_id)
    options = agent.calls[-1][0]
    assert (options.resume, options.resume_session_at, options.resume_drops_turn) == ("s1", "a1", prompts[1])
    assert transcript(store, conv_id)[-2:] == [("user", "second, edited"), ("text", "reply 3")]


async def test_edit_rejects_busy_conversation(chat, monkeypatch):
    from fastapi import HTTPException

    from personal_agent import server

    manager, store, agent, conv_id = chat
    await say(manager, conv_id, "first")
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "chats", manager)
    manager.running[conv_id] = asyncio.get_running_loop().create_future()  # a turn in progress
    with pytest.raises(ValueError):
        manager.edit(conv_id, 0, "x")
    with pytest.raises(HTTPException) as exc:
        await server.edit_message(conv_id, server.EditMessage(index=0, text="x"))
    assert exc.value.status_code == 409
    manager.running.pop(conv_id)
    for index, text in ((5, "x"), (-1, "x"), (0, "  ")):
        with pytest.raises(HTTPException) as exc:
            await server.edit_message(conv_id, server.EditMessage(index=index, text=text))
        assert exc.value.status_code == 400
    assert transcript(store, conv_id) == [("user", "first"), ("text", "reply 1")]  # nothing was removed
    assert store.conversation(conv_id)["session_id"] == "s1"


async def test_refused_edit_touches_nothing(chat, repo):
    manager, store, agent, conv_id = chat
    agent.writes = {1: (repo / "app.py", "one\n")}
    await say(manager, conv_id, "change it")
    before = store.events(conversation_id=conv_id)
    store.update_conversation(conv_id, repo="gone")  # the repo was removed from config.yaml since
    with pytest.raises(ValueError, match="no longer configured"):
        manager.edit(conv_id, 0, "never mind")
    assert (repo / "app.py").read_text() == "one\n"  # not restored
    assert manager.checkpoints.restored_ids([e["id"] for e in before if e["type"] == "checkpoint"]) == set()
    assert store.events(conversation_id=conv_id) == before and store.conversation(conv_id)["session_id"] == "s1"
    assert conv_id not in manager.running


async def test_edit_undoes_discarded_turns_newest_first(chat, repo):
    manager, store, agent, conv_id = chat
    original = (repo / "app.py").read_text()
    agent.writes = {1: (repo / "kept.py", "kept\n"), 2: (repo / "app.py", "two\n"), 3: (repo / "app.py", "three\n")}
    for text in ("add a file", "change it", "change it again"):
        await say(manager, conv_id, text)
    assert (repo / "app.py").read_text() == "three\n"
    assert sum(e["type"] == "checkpoint" for e in store.events(conversation_id=conv_id)) == 3

    manager.edit(conv_id, 1, "do something else")
    assert (repo / "app.py").read_text() == original  # the oldest original wins, not the state after turn 2
    assert (repo / "kept.py").read_text() == "kept\n"  # the kept turn's change stays
    await finish(manager, conv_id)
    assert [e["type"] for e in store.events(conversation_id=conv_id)].count("checkpoint") == 1
    assert not any(e["type"] == "warning" for e in store.events(conversation_id=conv_id))


async def test_edit_skips_undone_turns_and_warns_on_failure(chat, repo, monkeypatch):
    manager, store, agent, conv_id = chat
    agent.writes = {1: (repo / "app.py", "one\n"), 2: (repo / "new.py", "x\n")}
    await say(manager, conv_id, "change it")
    await say(manager, conv_id, "add a file")
    first, second = [e["id"] for e in store.events(conversation_id=conv_id) if e["type"] == "checkpoint"]
    manager.checkpoints.restore(second, manager.checkpoint_roots())  # already undone by hand
    (repo / "new.py").write_text("recreated\n")

    real_restore = manager.checkpoints.restore

    def restore(checkpoint_id, roots):
        if checkpoint_id == first:
            raise OSError("file is locked")
        return real_restore(checkpoint_id, roots)

    monkeypatch.setattr(manager.checkpoints, "restore", restore)
    manager.edit(conv_id, 0, "never mind")
    await finish(manager, conv_id)
    assert (repo / "new.py").read_text() == "recreated\n"  # the undone turn is not restored again
    assert [t for t, _ in transcript(store, conv_id)] == ["user", "warning", "text"]
    assert "file is locked" in transcript(store, conv_id)[1][1]


# resuming tasks after a server restart

def _resume_manager(tmp_path, monkeypatch, fail_resume=False):
    import asyncio

    from personal_agent import tasks as tasks_mod
    from personal_agent.bus import Bus
    from personal_agent.config import Config

    runs: list[dict] = []

    async def fake_run_agent(options, prompt, store, bus, kind, ref):
        runs.append({"resume": options.resume, "prompt": prompt})
        if options.resume and fail_resume:
            raise RuntimeError("No conversation found")
        yield {"type": "session", "session_id": "new-session"}
        yield {"type": "result", "is_error": False, "errors": None, "session_id": "new-session"}

    async def fake_review(ws, config, store, bus):
        return {"verdict": "approve", "summary": "ok", "findings": []}

    monkeypatch.setattr(tasks_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(tasks_mod, "review_workspace", fake_review)
    store = Store(tmp_path / "t.db")
    manager = tasks_mod.TaskManager(Config(repos=[Repo(name="r", path=tmp_path, access=Access.WRITE)]), store, Bus())

    async def no_finish(task_id):
        return {}

    monkeypatch.setattr(manager, "_finish", no_finish)  # these worktrees are not git checkouts

    def add(task_id: str, worktree: bool = True, **fields) -> None:
        wt = tmp_path / "wt" / task_id
        if worktree:
            wt.mkdir(parents=True)
        store.create_task(id=task_id, conversation_id=None, repo="r", title=task_id, spec="the spec",
                          branch=f"agent/{task_id}", base_sha="abc", worktree=str(wt))
        store.update_task(task_id, **fields)

    async def settle() -> None:
        await asyncio.gather(*manager.running.values())

    return manager, store, runs, add, settle


async def test_recover_resumes_active_tasks_with_their_session(tmp_path, monkeypatch):
    manager, store, runs, add, settle = _resume_manager(tmp_path, monkeypatch)
    add("impl", status="implementing", phase="implementing", session_id="s-impl")
    add("rev", status="reviewing", phase="reviewing", review_rounds=2, session_id="s-rev")
    add("fix", status="fixing", phase="fixing", review_rounds=1, session_id="s-fix")
    manager.recover()
    assert set(manager.running) == {"impl", "rev", "fix"}
    await settle()

    assert [r["resume"] for r in runs].count("s-impl") == 1
    assert [r["resume"] for r in runs].count("s-fix") == 1
    assert "s-rev" not in [r["resume"] for r in runs]  # a review is rerun, not resumed
    assert all("server restarted" in r["prompt"] for r in runs)
    for task_id in ("impl", "rev", "fix"):
        task = store.task(task_id)
        assert task["status"] == "ready" and task["resume_attempts"] == 1
        assert any(e["type"] == "resumed" for e in store.events(task_id=task_id))
    assert store.task("rev")["review_rounds"] == 2  # the interrupted round, not round 1 again
    assert store.task("fix")["review_rounds"] == 2  # the fix is followed by the next review
    assert store.task("impl")["session_id"] == "new-session"


async def test_ready_task_is_finished_with_a_pr(tmp_path, monkeypatch):
    manager, store, runs, add, settle = _resume_manager(tmp_path, monkeypatch)
    seen = []

    async def finish(task_id):
        seen.append(store.task(task_id)["status"])  # already ready, so a restart now does not rerun the review
        return {"pr_url": "https://github.com/o/r/pull/1", "pr_state": "open", "finish_error": None}

    monkeypatch.setattr(manager, "_finish", finish)
    add("t", status="reviewing", phase="reviewing", review_rounds=1)
    manager.recover()
    await settle()
    task = store.task("t")
    assert seen == ["ready"] and task["status"] == "ready" and task["pr_url"] == "https://github.com/o/r/pull/1"


async def test_no_automatic_pr_while_the_final_review_has_open_findings(tmp_path, monkeypatch):
    from personal_agent import tasks as tasks_mod

    manager, store, runs, add, settle = _resume_manager(tmp_path, monkeypatch)
    finished = []

    async def finish(task_id):
        finished.append(task_id)
        return {}

    async def still_blocking(ws, config, store, bus):
        return {"verdict": "changes_requested", "summary": "no", "findings": [
            {"severity": "blocking", "file": "a.py", "line": 1, "title": "t", "detail": "d"},
        ]}

    monkeypatch.setattr(manager, "_finish", finish)
    monkeypatch.setattr(tasks_mod, "review_workspace", still_blocking)
    add("t", status="reviewing", phase="reviewing", review_rounds=tasks_mod.MAX_FIX_ROUNDS + 1)
    manager.recover()
    await settle()
    task = store.task("t")
    assert finished == [] and task["status"] == "ready" and task["pr_url"] is None
    assert "open findings" in task["finish_error"]


async def test_recover_skips_missing_worktrees_and_finished_tasks(tmp_path, monkeypatch):
    manager, store, runs, add, settle = _resume_manager(tmp_path, monkeypatch)
    add("gone", worktree=False, status="implementing", session_id="s")
    add("stopped", status="stopped", session_id="s")
    add("discarded", status="discarded", session_id="s")
    add("merged", status="merged", session_id="s")
    manager.recover()
    assert manager.running == {}
    assert store.task("gone")["status"] == "stopped"
    assert [store.task(t)["status"] for t in ("stopped", "discarded", "merged")] == ["stopped", "discarded", "merged"]


async def test_recover_gives_up_after_the_attempt_limit(tmp_path, monkeypatch):
    from personal_agent.tasks import MAX_RESUME_ATTEMPTS

    manager, store, runs, add, settle = _resume_manager(tmp_path, monkeypatch)
    add("loop", status="implementing", session_id="s", resume_attempts=MAX_RESUME_ATTEMPTS)
    manager.recover()
    assert manager.running == {} and runs == []
    task = store.task("loop")
    assert task["status"] == "failed" and "gave up resuming" in task["error"]


async def test_recover_restarts_phase_when_resume_fails_or_no_session(tmp_path, monkeypatch):
    manager, store, runs, add, settle = _resume_manager(tmp_path, monkeypatch, fail_resume=True)
    add("broken", status="implementing", phase="implementing", session_id="missing")
    add("early", status="implementing", phase="implementing")
    manager.recover()
    await settle()

    assert [r["resume"] for r in runs].count("missing") == 1
    fresh = [r for r in runs if r["resume"] is None]
    assert len(fresh) == 2 and all("the spec" in r["prompt"] and "keep what is good" in r["prompt"] for r in fresh)
    assert store.task("broken")["status"] == "ready" and store.task("early")["status"] == "ready"
    texts = [e.get("text", "") for e in store.events(task_id="broken") if e["type"] == "resumed"]
    assert any(t.startswith("Could not resume session, restarted phase") for t in texts)


async def test_stop_marks_resumed_task_stopped_but_shutdown_does_not(tmp_path, monkeypatch):
    import asyncio

    from personal_agent import tasks as tasks_mod

    manager, store, runs, add, settle = _resume_manager(tmp_path, monkeypatch)

    async def hanging_run_agent(options, prompt, store, bus, kind, ref):
        yield {"type": "session", "session_id": "live"}
        await asyncio.Event().wait()

    monkeypatch.setattr(tasks_mod, "run_agent", hanging_run_agent)
    add("a", status="implementing", session_id="s")
    add("b", status="implementing", session_id="s")
    manager.recover()
    await asyncio.sleep(0.05)
    assert store.task("a")["session_id"] == "live"  # saved before the run ends

    assert manager.stop("a")
    await asyncio.sleep(0)
    await manager.shutdown()
    assert manager.running == {}
    assert store.task("a")["status"] == "stopped"
    assert store.task("b")["status"] == "implementing"  # still resumable on the next start


async def test_failure_after_a_successful_resume_fails_the_task(tmp_path, monkeypatch):
    from personal_agent import tasks as tasks_mod

    manager, store, runs, add, settle = _resume_manager(tmp_path, monkeypatch)

    async def worked_then_failed(options, prompt, store, bus, kind, ref):
        runs.append({"resume": options.resume, "prompt": prompt})
        yield {"type": "session", "session_id": "s"}
        yield {"type": "tool", "agent": "main", "name": "Edit", "summary": "app.py", "subagent": None}
        yield {"type": "result", "is_error": True, "errors": ["error_max_turns"], "session_id": "s"}

    monkeypatch.setattr(tasks_mod, "run_agent", worked_then_failed)
    add("t", status="implementing", phase="implementing", session_id="s")
    manager.recover()
    await settle()
    assert len(runs) == 1  # no fresh rerun of the phase
    task = store.task("t")
    assert task["status"] == "failed" and "error_max_turns" in task["error"]
    assert not any(e.get("text", "").startswith("Could not resume") for e in store.events(task_id="t"))


async def test_agents_dying_with_the_server_leave_tasks_resumable(tmp_path, monkeypatch):
    import asyncio

    from personal_agent import tasks as tasks_mod

    manager, store, runs, add, settle = _resume_manager(tmp_path, monkeypatch)
    started = asyncio.Event()
    die = asyncio.Event()

    async def dies_on_shutdown(options, prompt, store, bus, kind, ref):
        yield {"type": "tool", "agent": "main", "name": "Bash", "summary": "pytest", "subagent": None}
        started.set()
        await die.wait()
        if "crash" in str(options.cwd):
            raise RuntimeError("Command failed with exit code 3221225786")  # the child got Ctrl+C
        # otherwise the stream just ends with no result

    async def interrupted_review(ws, config, store, bus):
        return {"verdict": "changes_requested", "summary": "Review was interrupted.", "findings": []}

    monkeypatch.setattr(tasks_mod, "run_agent", dies_on_shutdown)
    monkeypatch.setattr(tasks_mod, "review_workspace", interrupted_review)
    add("crash", status="implementing", phase="implementing")
    add("ends", status="implementing", phase="implementing")
    manager.recover()
    await started.wait()
    manager.shutting_down = True  # the signal handler runs before the children's exit is seen
    die.set()
    await settle()
    for task_id in ("crash", "ends"):
        assert store.task(task_id)["status"] == "implementing", task_id
        assert not any(e["type"] in ("error", "review") for e in store.events(task_id=task_id))


async def test_implementer_ending_without_a_result_is_an_error(tmp_path, monkeypatch):
    from personal_agent import tasks as tasks_mod

    manager, store, runs, add, settle = _resume_manager(tmp_path, monkeypatch)

    async def no_result(options, prompt, store, bus, kind, ref):
        yield {"type": "text", "agent": "main", "text": "working"}

    monkeypatch.setattr(tasks_mod, "run_agent", no_result)
    add("t", status="implementing", phase="implementing")
    manager.recover()
    await settle()
    task = store.task("t")
    assert task["status"] == "failed" and "without a result" in task["error"]


def test_shutdown_signal_flags_tasks_before_uvicorn_handles_it():
    import signal

    from personal_agent import server

    seen = []
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in saved:
            signal.signal(sig, lambda signum, frame: seen.append(signum))  # stands in for uvicorn's handler
        server.flag_shutdown_on_signals()
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert server.tasks.shutting_down and seen == [signal.SIGINT]
    finally:
        server.tasks.shutting_down = False
        for sig, handler in saved.items():
            signal.signal(sig, handler)


@pytest.mark.parametrize("endpoint", ["admin_shutdown", "admin_restart"])
async def test_admin_exit_ends_task_pipelines_first(monkeypatch, endpoint):
    import asyncio

    from personal_agent import server

    order = []

    async def pipeline():
        try:
            await asyncio.Event().wait()
        finally:
            order.append("pipeline ended")

    async def exit_soon():
        order.append("exit scheduled")

    monkeypatch.setattr(server.admin, "shutdown", exit_soon)
    monkeypatch.setattr(server.admin, "restart", exit_soon)
    monkeypatch.setenv("PA_SUPERVISED", "1")
    monkeypatch.setattr(server.tasks, "running", {"t": asyncio.create_task(pipeline())})
    try:
        await asyncio.sleep(0)
        await getattr(server, endpoint)(force=True)
        assert order == ["pipeline ended", "exit scheduled"]
    finally:
        server.tasks.shutting_down = False
