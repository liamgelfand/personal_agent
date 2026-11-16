import subprocess
from pathlib import Path

import pytest

from personal_agent import gitops
from personal_agent.config import Access, Repo
from personal_agent.store import Store
from personal_agent.workspace import (
    Workspace, WorkspaceError, create_entry, delete_entry, list_entries, list_files, read_file, rename_entry, write_file,
)


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


async def test_list_entries_reports_empty_dirs(repo):
    (repo / "empty" / "nested").mkdir(parents=True)
    (repo / "full").mkdir()
    (repo / "full" / "f.txt").write_text("x", newline="")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "pkg").mkdir()
    (repo / "node_modules" / "pkg" / "x.js").write_text("x", newline="")
    (repo / "dist").mkdir()  # a fresh empty folder with a skipped name still shows; skipped dirs aren't walked into
    assert await list_entries(ws(repo)) == ([".gitignore", "a.py", "full/f.txt"], ["dist", "empty/nested"], False)


def test_create_rename_delete_entries(repo):
    w = ws(repo)
    create_entry(w, "src/new.py", is_dir=False)
    assert (repo / "src" / "new.py").read_bytes() == b""
    create_entry(w, "docs", is_dir=True)
    assert (repo / "docs").is_dir()
    rename_entry(w, "src/new.py", "docs/moved.py")
    assert (repo / "docs" / "moved.py").is_file() and not (repo / "src" / "new.py").exists()
    rename_entry(w, "a.py", "A.py")  # case-only rename, even where the filesystem ignores case
    assert "A.py" in [p.name for p in repo.iterdir()]
    delete_entry(w, "docs")
    delete_entry(w, "A.py")
    assert not (repo / "docs").exists() and not (repo / "A.py").exists()


def test_entries_refuse_overwriting_existing_paths(repo):
    w = ws(repo)
    (repo / "sub").mkdir()
    (repo / "sub" / "keep.txt").write_text("keep", newline="")
    for rel, is_dir in (("a.py", False), ("a.py", True), ("sub", True), ("sub", False)):
        with pytest.raises(WorkspaceError, match="already exists"):
            create_entry(w, rel, is_dir)
    with pytest.raises(WorkspaceError, match="already exists"):
        rename_entry(w, "a.py", "sub/keep.txt")
    with pytest.raises(WorkspaceError, match="into itself"):
        rename_entry(w, "sub", "sub/inner")
    assert (repo / "a.py").read_text() == "one\n" and (repo / "sub" / "keep.txt").read_text() == "keep"


def test_entries_are_confined_to_the_workspace(repo, tmp_path):
    w = ws(repo)
    (tmp_path / "outside.txt").write_text("x", newline="")
    for rel in ("../outside.txt", "../evil", "sub/../../evil", str(tmp_path / "evil"), "", ".", ".."):
        with pytest.raises(WorkspaceError):
            create_entry(w, rel, is_dir=False)
        with pytest.raises(WorkspaceError):
            delete_entry(w, rel)
        with pytest.raises(WorkspaceError):
            rename_entry(w, "a.py", rel)
    with pytest.raises(WorkspaceError):
        rename_entry(w, "../outside.txt", "stolen.txt")
    # Windows ignores case and trailing dots/spaces, so these all name the real .git there.
    for rel in (".git", ".git/config", ".git/hooks/x", ".GIT", ".Git.", ".git ", ".GIT/config", "sub/.GIT."):
        with pytest.raises(WorkspaceError, match="git metadata"):
            delete_entry(w, rel)
        with pytest.raises(WorkspaceError, match="git metadata"):
            rename_entry(w, rel, "moved")
        with pytest.raises(WorkspaceError, match="git metadata"):
            create_entry(w, rel, is_dir=True)
    with pytest.raises(WorkspaceError, match="git metadata"):
        write_file(w, ".GIT/config", "x")
    with pytest.raises(WorkspaceError, match="git metadata"):
        rename_entry(w, "a.py", ".git/a.py")
    assert (tmp_path / "outside.txt").exists() and not (tmp_path / "evil").exists() and (repo / ".git").is_dir()


def test_failed_rename_removes_the_folders_it_made(repo, monkeypatch):
    def fail(self, target):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(WorkspaceError, match="could not rename"):
        rename_entry(ws(repo), "a.py", "new/deeper/a.py")
    assert not (repo / "new").exists() and (repo / "a.py").exists()


def test_entries_respect_read_only_access(repo):
    r = ws(repo, Access.READ)
    with pytest.raises(WorkspaceError, match="read-only"):
        create_entry(r, "new.py", is_dir=False)
    with pytest.raises(WorkspaceError, match="read-only"):
        rename_entry(r, "a.py", "b.py")
    with pytest.raises(WorkspaceError, match="read-only"):
        delete_entry(r, "a.py")
    assert sorted(p.name for p in repo.iterdir()) == [".git", ".gitignore", "a.py"]


def test_fs_entry_endpoints(repo, monkeypatch):
    from fastapi.testclient import TestClient

    from personal_agent import server
    from personal_agent.config import Config

    monkeypatch.setattr(server, "config", Config(repos=[
        Repo(name="r", path=repo.resolve(), access=Access.WRITE),
        Repo(name="ro", path=repo.resolve(), access=Access.READ),
    ]))
    published = []

    async def publish(event):
        published.append(event)

    monkeypatch.setattr(server.bus, "publish", publish)
    client = TestClient(server.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))

    assert client.post("/api/fs/entry?repo=r", json={"path": "pkg", "dir": True}).status_code == 200
    assert client.post("/api/fs/entry?repo=r", json={"path": "pkg/mod.py"}).status_code == 200
    assert (repo / "pkg" / "mod.py").is_file()
    assert client.post("/api/fs/rename?repo=r", json={"path": "pkg/mod.py", "new_path": "pkg/main.py"}).status_code == 200
    assert (repo / "pkg" / "main.py").is_file()
    assert published[-1] == {"type": "fs_changed", "repo": "r", "task": None}

    existing = client.post("/api/fs/entry?repo=r", json={"path": "a.py"})
    assert existing.status_code == 400 and "already exists" in existing.json()["detail"]
    clash = client.post("/api/fs/rename?repo=r", json={"path": "pkg/main.py", "new_path": "a.py"})
    assert clash.status_code == 400 and (repo / "a.py").read_text() == "one\n"
    for bad in (client.post("/api/fs/entry?repo=r", json={"path": "../escape.txt"}),
                client.post("/api/fs/rename?repo=r", json={"path": "a.py", "new_path": "../escape.txt"}),
                client.delete("/api/fs/entry", params={"repo": "r", "path": "../repo"})):
        assert bad.status_code == 400 and "outside the workspace" in bad.json()["detail"]
    assert not (repo.parent / "escape.txt").exists() and repo.exists()
    read_only = client.post("/api/fs/entry?repo=ro", json={"path": "x.py"})
    assert read_only.status_code == 400 and "read-only" in read_only.json()["detail"]

    tree = client.get("/api/fs/tree?repo=r").json()
    assert tree["files"] == [".gitignore", "a.py", "pkg/main.py"] and tree["dirs"] == []
    assert client.delete("/api/fs/entry", params={"repo": "r", "path": "pkg"}).status_code == 200
    assert not (repo / "pkg").exists()


def test_store_conversations(tmp_path):
    store = Store(tmp_path / "t.db")
    conv = store.create_conversation(None)
    linked = store.create_conversation("r")
    store.add_event({"type": "user", "text": "find the parser bug"}, conversation_id=conv["id"])
    assert [c["id"] for c in store.conversations(repo="")] == [conv["id"]]
    assert [c["id"] for c in store.conversations(repo="r")] == [linked["id"]]
    assert [c["id"] for c in store.conversations(query="parser")] == [conv["id"]]
    assert store.events(conversation_id=conv["id"]) == [{"type": "user", "text": "find the parser bug"}]
