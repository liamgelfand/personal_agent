import subprocess
from pathlib import Path

import pytest

from personal_agent import gitops
from personal_agent.config import Access, Repo
from personal_agent.store import Store
from personal_agent.workspace import Workspace, WorkspaceError, list_files, read_file, write_file


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
    (root / "a.py").write_text("one\n", newline="")
    (root / ".gitignore").write_text("ignored.txt\n", newline="")
    sh(root, "git", "add", ".")
    sh(root, "git", "commit", "-q", "-m", "init")
    return root


def ws(path: Path, access: Access = Access.WRITE) -> Workspace:
    return Workspace(Repo(name="r", path=path, access=access), None, path.resolve(), access)


async def test_status_and_diff_sides(repo):
    (repo / "a.py").write_text("two\n", newline="")
    (repo / "new.py").write_text("new\n", newline="")
    st = await gitops.status(repo)
    files = {f["path"]: f for f in st["files"]}
    assert st["branch"].startswith("main")
    assert files["a.py"]["unstaged"] and not files["a.py"]["staged"]
    assert files["new.py"]["untracked"]
    assert await gitops.diff_sides(repo, "a.py", staged=False) == {"original": "one\n", "modified": "two\n"}
    assert await gitops.diff_sides(repo, "new.py", staged=False) == {"original": "", "modified": "new\n"}


async def test_stage_unstage_commit(repo):
    (repo / "a.py").write_text("two\n", newline="")
    await gitops.stage(repo, ["a.py"])
    assert (await gitops.status(repo))["files"][0]["staged"]
    assert await gitops.diff_sides(repo, "a.py", staged=True) == {"original": "one\n", "modified": "two\n"}
    await gitops.unstage(repo, ["a.py"])
    assert not (await gitops.status(repo))["files"][0]["staged"]
    await gitops.stage(repo, ["a.py"])
    sha = await gitops.commit(repo, "change")
    assert sha and (await gitops.status(repo))["files"] == []


async def test_discard_restores_tracked_and_deletes_untracked(repo):
    (repo / "a.py").write_text("two\n", newline="")
    (repo / "new.py").write_text("new\n", newline="")
    await gitops.stage(repo, ["new.py"])
    await gitops.discard(repo, ["a.py", "new.py"])
    assert (repo / "a.py").read_text() == "one\n"
    assert not (repo / "new.py").exists()
    assert (await gitops.status(repo))["files"] == []


async def test_worktree_lifecycle(repo, tmp_path):
    wt = tmp_path / "wt"
    await gitops.add_worktree(repo, wt, "agent/x")
    assert (wt / "a.py").read_text() == "one\n"
    await gitops.remove_worktree(repo, wt, "agent/x")
    assert not wt.exists()
    assert "agent/x" not in await gitops.git(repo, "branch")


async def test_list_files_includes_ignored_but_skips_heavy_dirs(repo):
    (repo / "ignored.txt").write_text("x", newline="")
    (repo / "untracked.py").write_text("x", newline="")
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "index.js").write_text("x", newline="")
    (repo / "tools" / "build").mkdir(parents=True)
    (repo / "tools" / "build" / "release.py").write_text("x", newline="")
    (repo / "tools" / "build" / "out.o").write_text("x", newline="")
    sh(repo, "git", "add", "tools/build/release.py")
    files = [".gitignore", "a.py", "ignored.txt", "tools/build/release.py", "untracked.py"]
    assert await list_files(ws(repo)) == (files, False)


async def test_list_files_reports_truncation(repo, monkeypatch):
    from personal_agent import workspace

    monkeypatch.setattr(workspace, "MAX_LIST_FILES", 1)
    assert await list_files(ws(repo)) == ([".gitignore"], True)


async def test_list_files_cap_keeps_non_ignored_files_over_ignored(repo, monkeypatch):
    from personal_agent import workspace

    (repo / "ignored.txt").write_text("x", newline="")  # sorts before "untracked.py" but is dropped first
    (repo / "untracked.py").write_text("x", newline="")
    monkeypatch.setattr(workspace, "MAX_LIST_FILES", 3)
    assert await list_files(ws(repo)) == ([".gitignore", "a.py", "untracked.py"], True)


async def test_worktree_git_link_file_is_hidden_and_not_writable(repo, tmp_path):
    wt = tmp_path / "wt"
    await gitops.add_worktree(repo, wt, "agent/x")
    w = ws(wt)
    assert (wt / ".git").is_file()
    assert await list_files(w) == ([".gitignore", "a.py"], False)
    with pytest.raises(WorkspaceError):
        write_file(w, ".git", "gitdir: elsewhere\n")


def test_file_access_is_confined_and_respects_access(repo):
    w = ws(repo)
    assert read_file(w, "a.py")["content"] == "one\n"
    with pytest.raises(WorkspaceError):
        read_file(w, "../outside.txt")
    write_file(w, "sub/new.py", "x\n")
    assert (repo / "sub" / "new.py").read_text() == "x\n"
    with pytest.raises(WorkspaceError):
        write_file(ws(repo, Access.READ), "a.py", "nope")


def test_write_preserves_crlf(repo):
    (repo / "win.txt").write_bytes(b"a\r\nb\r\n")
    write_file(ws(repo), "win.txt", "a\nb\nc\n")
    assert (repo / "win.txt").read_bytes() == b"a\r\nb\r\nc\r\n"


def test_store_conversations(tmp_path):
    store = Store(tmp_path / "t.db")
    conv = store.create_conversation(None)
    linked = store.create_conversation("r")
    store.add_event({"type": "user", "text": "find the parser bug"}, conversation_id=conv["id"])
    assert [c["id"] for c in store.conversations(repo="")] == [conv["id"]]
    assert [c["id"] for c in store.conversations(repo="r")] == [linked["id"]]
    assert [c["id"] for c in store.conversations(query="parser")] == [conv["id"]]
    assert store.events(conversation_id=conv["id"]) == [{"type": "user", "text": "find the parser bug"}]
