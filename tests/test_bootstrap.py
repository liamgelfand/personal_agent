import os
import subprocess
import sys
from pathlib import Path

import pytest

from personal_agent import bootstrap, gitops
from personal_agent.config import Access, Config, Repo
from personal_agent.store import Store


def sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with a frontend package whose node_modules is installed but not git-ignored."""
    root = tmp_path / "repo"
    (root / "frontend").mkdir(parents=True)
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "config", "user.email", "t@example.com")
    sh(root, "git", "config", "user.name", "t")
    (root / "frontend" / "package.json").write_text('{"scripts": {"typecheck": "tsc"}}\n', newline="")
    sh(root, "git", "add", ".")
    sh(root, "git", "commit", "-q", "-m", "init")
    (root / "frontend" / "node_modules" / "lib").mkdir(parents=True)
    (root / "frontend" / "node_modules" / "lib" / "index.js").write_text("x\n", newline="")
    return root


# node_modules links

async def test_link_dependencies_and_removal_keeps_main_node_modules(repo, tmp_path):
    wt = tmp_path / "wt"
    await gitops.add_worktree(repo, wt, "agent/x")
    assert await gitops.link_dependencies(repo, wt) == ["frontend/node_modules"]
    assert gitops.is_link(wt / "frontend" / "node_modules")
    assert (wt / "frontend" / "node_modules" / "lib" / "index.js").read_text() == "x\n"
    assert gitops.dependency_links(wt) == ["frontend/node_modules"]
    assert (await gitops.status(wt))["files"] == []  # excluded, so never committed
    assert await gitops.link_dependencies(repo, wt) == []  # already there

    await gitops.remove_worktree(repo, wt, "agent/x")
    assert not wt.exists()
    assert (repo / "frontend" / "node_modules" / "lib" / "index.js").read_text() == "x\n"


async def test_link_dependencies_keeps_status_clean_when_already_ignored(repo, tmp_path):
    (repo / ".gitignore").write_text("node_modules/\n", newline="")
    sh(repo, "git", "add", ".")
    sh(repo, "git", "commit", "-q", "-m", "ignore")
    wt = tmp_path / "wt"
    await gitops.add_worktree(repo, wt, "agent/x")
    assert await gitops.link_dependencies(repo, wt) == ["frontend/node_modules"]
    if os.name == "nt":
        # Git sees a junction as a folder, so `node_modules/` already covers it. A POSIX symlink is
        # not a folder to git, so there the exclude entry is (rightly) added.
        exclude = repo / ".git" / "info" / "exclude"
        assert "/frontend/node_modules" not in (exclude.read_text() if exclude.exists() else "")
    assert (await gitops.status(wt))["files"] == []
    await gitops.remove_worktree(repo, wt, "agent/x")
    assert (repo / "frontend" / "node_modules" / "lib" / "index.js").exists()


async def test_link_dependencies_skips_when_absent_or_present(repo, tmp_path):
    wt = tmp_path / "wt"
    await gitops.add_worktree(repo, wt, "agent/x")
    (wt / "frontend" / "node_modules").mkdir()  # the worktree already has its own
    assert await gitops.link_dependencies(repo, wt) == []
    assert not gitops.is_link(wt / "frontend" / "node_modules")

    import shutil

    shutil.rmtree(repo / "frontend" / "node_modules")
    wt2 = tmp_path / "wt2"
    await gitops.add_worktree(repo, wt2, "agent/y")
    assert gitops.dependency_dirs(repo) == []
    assert await gitops.link_dependencies(repo, wt2) == []
    assert not (wt2 / "frontend" / "node_modules").exists()


@pytest.mark.parametrize("command", [
    "npm install", "cd frontend && npm i -D vitest", "npm --prefix frontend install left-pad", "npm ci",
    "pnpm add zod", "yarn", "yarn add zod", "npm uninstall react", "rm -rf frontend/node_modules",
    "npx rimraf node_modules",
])
def test_dependency_write_commands_are_detected(command):
    assert bootstrap.DEPENDENCY_WRITE.search(command)


@pytest.mark.parametrize("command", [
    "cd frontend && npm run typecheck", "npm run build", "npm test -- --grep add", "npx tsc --noEmit",
    "ls frontend/node_modules", "rm -rf dist; ls node_modules", "git diff",
])
def test_ordinary_commands_keep_the_link(command):
    assert not bootstrap.DEPENDENCY_WRITE.search(command)


async def test_install_command_swaps_the_link_for_a_real_folder(repo, tmp_path):
    wt = tmp_path / "wt"
    await gitops.add_worktree(repo, wt, "agent/x")
    await gitops.link_dependencies(repo, wt)
    seen = []

    async def on_unlink(links, command):
        seen.append((links, command))

    hook = bootstrap.dependency_link_hook(wt, on_unlink).hooks[0]
    assert await hook({"tool_name": "Bash", "tool_input": {"command": "npm run typecheck"}}, "1", None) == {}
    assert gitops.is_link(wt / "frontend" / "node_modules") and seen == []

    assert await hook({"tool_name": "Bash", "tool_input": {"command": "cd frontend && npm install zod"}}, "2", None) == {}
    assert not (wt / "frontend" / "node_modules").exists()
    assert seen == [(["frontend/node_modules"], "cd frontend && npm install zod")]
    assert (repo / "frontend" / "node_modules" / "lib" / "index.js").read_text() == "x\n"


# setup commands

async def test_run_setup_reports_failures_and_keeps_going(tmp_path):
    failures = await bootstrap.run_setup(["exit 3", "echo made> made.txt"], tmp_path)
    assert len(failures) == 1 and "`exit 3` exited with 3" in failures[0]
    assert (tmp_path / "made.txt").exists()


async def test_cancelled_setup_kills_the_command_and_its_children(tmp_path):
    import asyncio

    beat = tmp_path / "beat.txt"
    # The shell starts Python, which keeps appending to a file: it must stop once the setup is cancelled.
    script = f"import time\nwhile True:\n    open({str(beat)!r}, 'a').write('x')\n    time.sleep(0.05)\n"
    (tmp_path / "beat.py").write_text(script)
    command = f'"{Path(sys.executable).as_posix()}" "{(tmp_path / "beat.py").as_posix()}"'
    job = asyncio.create_task(bootstrap.run_setup([command], tmp_path))
    for _ in range(200):
        if beat.exists():
            break
        await asyncio.sleep(0.05)
    assert beat.exists()
    job.cancel()
    # Killing only the shell leaves the child holding the output pipe, and the cancel never finishes.
    done, _ = await asyncio.wait({job}, timeout=15)
    assert done, "setup was not killed with its children"
    with pytest.raises(asyncio.CancelledError):
        job.result()
    await asyncio.sleep(0.3)
    size = beat.stat().st_size
    await asyncio.sleep(0.5)
    assert beat.stat().st_size == size


# environment section

def test_environment_section_lists_links_and_detected_commands(repo, tmp_path):
    wt = tmp_path / "wt"
    sh(repo, "git", "worktree", "add", "-q", "-b", "agent/x", str(wt))
    (wt / "tests").mkdir()
    (wt / "src").mkdir()
    python = repo / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("")
    (wt / "frontend" / "node_modules").mkdir()  # a real folder is not reported as a link

    text = bootstrap.environment_section(Repo(name="r", path=repo, access=Access.WRITE), wt, "implementer")
    assert f"PYTHONPATH={(wt / 'src').as_posix()} {python.as_posix()} -m pytest tests" in text
    assert "`cd frontend && npm run typecheck`" in text
    assert "Linked from" not in text
    assert "Do not build the full frontend bundle" in text and "full suite once at the end" in text

    review = bootstrap.environment_section(
        Repo(name="r", path=repo, access=Access.WRITE, verify=["make test"], setup=["make deps"]), wt, "reviewer",
    )
    assert "`make test`" in review and "pytest" not in review and "`make deps`" in review
    assert "Do not rerun the full test suite" in review


# config override

def test_config_path_env_override(tmp_path):
    config = tmp_path / "other.yaml"
    config.write_text("repos: []\nport: 9123\nmax_review_rounds: 4\n")
    code = "from personal_agent.config import CONFIG_PATH, load_config; c = load_config(); print(CONFIG_PATH, c.port, c.max_review_rounds)"
    env = {**os.environ, "PERSONAL_AGENT_CONFIG": str(config), "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout
    assert out.split() == [str(config), "9123", "4"]
    assert Config().max_review_rounds == 2


# review loop and phase timing

def _manager(tmp_path, monkeypatch, reviews: list[dict], **config):
    import asyncio

    from personal_agent import tasks as tasks_mod
    from personal_agent.bus import Bus

    calls = {"implement": [], "review": 0}

    async def fake_run_agent(options, prompt, store, bus, kind, ref):
        calls["implement"].append(prompt)
        yield {"type": "session", "session_id": "s"}
        yield {"type": "result", "is_error": False, "errors": None, "session_id": "s", "num_turns": 7, "cost_usd": 0.5}

    async def fake_review(ws, config, store, bus, stats=None):
        review = reviews[min(calls["review"], len(reviews) - 1)]
        calls["review"] += 1
        stats.update(turns=3, cost_usd=0.25)
        store.save_review(ws.key, review)
        return review

    monkeypatch.setattr(tasks_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(tasks_mod, "review_workspace", fake_review)
    store = Store(tmp_path / "t.db")
    repo = Repo(name="r", path=tmp_path, access=Access.WRITE, **config.pop("repo", {}))
    manager = tasks_mod.TaskManager(Config(repos=[repo], **config), store, Bus())
    wt = tmp_path / "wt"
    wt.mkdir()
    store.create_task(id="t", conversation_id=None, repo="r", title="t", spec="the spec",
                      branch="agent/t", base_sha="abc", worktree=str(wt))

    async def run() -> None:
        await asyncio.wait_for(manager._pipeline("t"), 10)

    return manager, store, calls, run


def finding(severity: str) -> dict:
    return {"file": "app.py", "line": 1, "severity": severity, "title": f"a {severity}", "detail": "d"}


async def test_non_blocking_findings_end_the_review_loop(tmp_path, monkeypatch):
    review = {"verdict": "approve", "summary": "fine", "findings": [finding("should_fix"), finding("nit")]}
    manager, store, calls, run = _manager(tmp_path, monkeypatch, [review])
    await run()
    assert calls["review"] == 1 and len(calls["implement"]) == 1
    task = store.task("t")
    assert task["status"] == "ready" and task["review_rounds"] == 1


async def test_review_loop_respects_max_review_rounds(tmp_path, monkeypatch):
    blocking = {"verdict": "changes_requested", "summary": "bad", "findings": [finding("blocking"), finding("nit")]}
    manager, store, calls, run = _manager(tmp_path, monkeypatch, [blocking])
    await run()
    assert calls["review"] == 2 and len(calls["implement"]) == 2  # implement, review, fix, review
    assert "a blocking" in calls["implement"][1] and "a nit" not in calls["implement"][1]
    task = store.task("t")
    assert task["status"] == "ready" and task["review_rounds"] == 2
    warnings = [e["message"] for e in store.events(task_id="t") if e["type"] == "warning"]
    assert len(warnings) == 1 and "Review limit reached (2 rounds)" in warnings[0] and "a blocking" in warnings[0]


async def test_blocking_then_clean_review_stops_before_the_cap(tmp_path, monkeypatch):
    blocking = {"verdict": "changes_requested", "summary": "bad", "findings": [finding("blocking")]}
    clean = {"verdict": "approve", "summary": "ok", "findings": [finding("should_fix")]}
    manager, store, calls, run = _manager(tmp_path, monkeypatch, [blocking, clean], max_review_rounds=5)
    await run()
    assert calls["review"] == 2 and len(calls["implement"]) == 2
    assert not any(e["type"] == "warning" for e in store.events(task_id="t"))


async def test_phase_timing_events_and_summary(tmp_path, monkeypatch):
    blocking = {"verdict": "changes_requested", "summary": "bad", "findings": [finding("blocking")]}
    clean = {"verdict": "approve", "summary": "ok", "findings": []}
    manager, store, calls, run = _manager(tmp_path, monkeypatch, [blocking, clean])
    await run()
    events = store.events(task_id="t")
    phases = [e for e in events if e["type"] == "phase"]
    assert [(e["phase"], e["round"]) for e in phases] == [
        ("bootstrap", None), ("implement", None), ("review", 1), ("fix", 1), ("review", 2),
    ]
    assert all(e["ended_at"] >= e["started_at"] and e["seconds"] >= 0 for e in phases)
    assert (phases[1]["turns"], phases[1]["cost_usd"]) == (7, 0.5)
    assert (phases[2]["turns"], phases[2]["cost_usd"]) == (3, 0.25)
    assert "7 turns" in phases[1]["text"] and "$0.50" in phases[1]["text"]
    summary = "bootstrap 0s, implement 0s, review 0s, fix 0s, review 0s"
    assert events[-1] == {"type": "timing", "agent": "pipeline", "text": f"Finished: {summary}", "summary": summary}
    assert store.task("t")["timing"] == summary


async def test_failing_setup_command_does_not_fail_the_task(tmp_path, monkeypatch):
    clean = {"verdict": "approve", "summary": "ok", "findings": []}
    manager, store, calls, run = _manager(tmp_path, monkeypatch, [clean], repo={"setup": ["exit 2", "echo hi"]})
    await run()
    assert store.task("t")["status"] == "ready"
    warnings = [e["message"] for e in store.events(task_id="t") if e["type"] == "warning"]
    assert len(warnings) == 1 and "Setup command failed" in warnings[0] and "exit 2" in warnings[0]


def test_duration_formatting():
    from personal_agent.tasks import _duration

    assert [_duration(s) for s in (4.4, 59.6, 61, 552)] == ["4s", "1m 0s", "1m 1s", "9m 12s"]
    assert [_duration(s, short=True) for s in (4.4, 552, 570)] == ["4s", "9m", "10m"]
