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


async def test_restored_checkpoints_are_reported(tmp_path, repo):
    cps = Checkpoints(Store(tmp_path / "t.db"))
    cp = Checkpoint(repo)
    await run_edit(cp, "1", repo / "app.py", "changed\n")
    saved = cps.save(cp, "conv")
    assert cps.restored_ids([saved["id"]]) == set()
    cps.restore(saved["id"], [repo.resolve()])
    assert cps.restored_ids([saved["id"], "other"]) == {saved["id"]}


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
