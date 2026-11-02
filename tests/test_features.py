import subprocess
from pathlib import Path

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
