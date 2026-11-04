import asyncio
import os
from pathlib import Path

import pytest

from personal_agent import terminal
from personal_agent.config import SCRATCH_DIR, Access, Config, Repo
from personal_agent.store import Store
from personal_agent.terminal import Terminals, TerminalError
from personal_agent.workspace import WorkspaceError

HOST = "127.0.0.1:8000"
HEADERS = {"Host": HOST, "Origin": f"http://{HOST}"}


@pytest.fixture
def repos(tmp_path):
    (tmp_path / "rw").mkdir()
    (tmp_path / "ro").mkdir()
    return [Repo(name="rw", path=tmp_path / "rw", access=Access.WRITE),
            Repo(name="ro", path=tmp_path / "ro", access=Access.READ)]


# where a shell may start

def test_resolve_cwd_uses_configured_repos_only(tmp_path, repos):
    config, store = Config(repos=repos), Store(tmp_path / "t.db")
    assert terminal.resolve_cwd(config, store, "rw") == repos[0].path
    assert terminal.resolve_cwd(config, store, None) == SCRATCH_DIR.resolve()
    for name in ("nope", str(tmp_path / "rw"), "../rw"):  # names only, never paths
        with pytest.raises(WorkspaceError):
            terminal.resolve_cwd(config, store, name)
    with pytest.raises(WorkspaceError):  # a task the store doesn't know
        terminal.resolve_cwd(config, store, "rw", "task-1")


def test_resolve_cwd_refuses_read_only_repos(tmp_path, repos):
    with pytest.raises(TerminalError, match="read-only"):
        terminal.resolve_cwd(Config(repos=repos), Store(tmp_path / "t.db"), "ro")


def test_shell_command_setting():
    assert terminal.shell_command("C:/Program Files/Git/bin/bash.exe") == ["C:/Program Files/Git/bin/bash.exe"]
    assert terminal.shell_command(["bash", "--login"]) == ["bash", "--login"]
    default = terminal.shell_command(None)
    if os.name == "nt":
        assert Path(default[0]).stem.lower() in ("pwsh", "powershell")
    else:
        assert default == [os.environ.get("SHELL") or "/bin/bash"]


async def test_terminal_limit(monkeypatch, tmp_path):
    closed = []

    class FakeSession:
        def __init__(self, argv, cwd, cols, rows):
            pass

        async def close(self):
            closed.append(self)

    monkeypatch.setattr(terminal, "Session", FakeSession)
    terms = Terminals(limit=2)
    first = await terms.open("a", ["sh"], tmp_path)
    await terms.open("b", ["sh"], tmp_path)
    with pytest.raises(TerminalError, match="at most 2"):
        await terms.open("c", ["sh"], tmp_path)
    with pytest.raises(TerminalError, match="already in use"):  # a stale connection can't take over a shell
        await terms.open("a", ["sh"], tmp_path)
    assert closed == [] and terms.sessions["a"] is first
    await terms.close_all()
    assert terms.sessions == {} and len(closed) == 2


async def test_close_releases_the_pty_only_after_the_reader_stops(monkeypatch, tmp_path):
    import threading

    events = []

    class FakePty:
        pid = 1

        def __init__(self, argv, cwd, cols, rows):
            self.woken = threading.Event()

        def read(self):
            self.woken.wait(5)  # blocks like a pty read until kill() wakes it
            events.append("reader done")
            return b""

        def kill(self):
            events.append("kill")
            self.woken.set()

        def release(self):
            events.append("release")

    monkeypatch.setattr(terminal, "_WindowsPty", FakePty)
    monkeypatch.setattr(terminal, "_PosixPty", FakePty)
    session = terminal.Session(["sh"], tmp_path)
    await session.close()
    await session.close()  # a second close does nothing
    assert events == ["kill", "reader done", "release"]
    session.write(b"late")  # after close: ignored, never reaches a closed fd


def test_windows_binary_input_keeps_every_byte():
    sent = []
    pty = object.__new__(terminal._WindowsPty)
    pty.proc = type("Proc", (), {"write": lambda self, s: sent.append(s)})()
    pty.write(b"\x1b[M \xc8\xa0", binary=True)  # an X10 mouse report with coordinates above 95
    pty.write("h\u00e9llo".encode())
    assert sent == ["\x1b[M \xc8\xa0", "h\u00e9llo"]


# who may open one

def test_terminal_client_allowed(monkeypatch):
    from personal_agent import server

    monkeypatch.setattr(server.config.remote, "enabled", False)
    monkeypatch.setattr(server.config.terminal, "allow_remote", False)
    assert server.terminal_client_allowed("127.0.0.1") and server.terminal_client_allowed("::1")
    assert not server.terminal_client_allowed("100.64.0.5")
    assert not server.terminal_client_allowed(None)
    monkeypatch.setattr(server.config.remote, "enabled", True)
    assert not server.terminal_client_allowed("100.64.0.5")  # remote access alone isn't enough
    monkeypatch.setattr(server.config.terminal, "allow_remote", True)
    assert server.terminal_client_allowed("100.64.0.5")
    monkeypatch.setattr(server.config.remote, "enabled", False)
    assert not server.terminal_client_allowed("100.64.0.5")


@pytest.fixture
def ws_client(monkeypatch, repos):
    """A TestClient for the terminal socket, with shells faked so refusals are tested without one."""
    from fastapi.testclient import TestClient

    from personal_agent import server

    monkeypatch.setattr(server.config, "repos", repos)
    monkeypatch.setattr(server.config.remote, "enabled", False)
    started = []

    async def fake_open(key, argv, cwd, cols=80, rows=24):
        started.append(cwd)
        raise TerminalError("fake: no shell in this test")

    monkeypatch.setattr(server.terminals, "open", fake_open)

    def connect(query: str, client=("127.0.0.1", 50000), headers=HEADERS):
        return TestClient(server.app, client=client).websocket_connect(f"/api/terminal/ws?{query}", headers=headers)

    connect.started = started
    return connect


def refusal(session) -> str:
    with session as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        return msg["message"]


def test_terminal_socket_forwards_text_binary_and_resize(monkeypatch, repos):
    from fastapi.testclient import TestClient

    from personal_agent import server

    monkeypatch.setattr(server.config, "repos", repos)
    monkeypatch.setattr(server.config.remote, "enabled", False)
    calls = []

    class FakeSession:
        async def read(self):
            await asyncio.Event().wait()  # no output; the client hangs up

        def write(self, data, binary=False):
            calls.append(("write", data, binary))

        def resize(self, cols, rows):
            calls.append(("resize", cols, rows))

        async def close(self):
            calls.append(("close",))

    async def fake_open(key, argv, cwd, cols=80, rows=24):
        return FakeSession()

    monkeypatch.setattr(server.terminals, "open", fake_open)
    client = TestClient(server.app, client=("127.0.0.1", 50000))
    with client.websocket_connect("/api/terminal/ws?repo=rw&id=t1", headers=HEADERS) as ws:
        ws.send_bytes("dir \u00e9\r".encode())
        ws.send_text(r'{"type": "binary", "data": "\u001b[M \u00c8\u00a0"}')
        ws.send_text('{"type": "resize", "cols": 120, "rows": 40}')
        ws.send_text("not json")  # ignored
    assert calls == [("write", "dir \u00e9\r".encode(), False), ("write", b"\x1b[M \xc8\xa0", True),
                     ("resize", 120, 40), ("close",)]


def test_terminal_socket_rejects_other_origins(ws_client):
    from starlette.websockets import WebSocketDisconnect

    for headers in ({"Host": HOST, "Origin": "https://evil.example"}, {"Host": HOST},
                    {"Host": "evil.example:8000", "Origin": "http://evil.example:8000"}):  # DNS rebinding
        with pytest.raises(WebSocketDisconnect) as exc:
            with ws_client("repo=rw&id=t1", headers=headers) as ws:
                ws.receive_json()
        assert exc.value.code == 1008
    assert ws_client.started == []


def test_terminal_socket_refusals(ws_client, repos):
    assert "this computer only" in refusal(ws_client("repo=rw&id=t1", client=("100.64.0.5", 50000)))
    assert "read-only" in refusal(ws_client("repo=ro&id=t1"))
    assert "unknown repo" in refusal(ws_client("repo=nope&id=t1"))
    assert "missing terminal id" in refusal(ws_client("repo=rw"))
    assert ws_client.started == []
    assert "fake" in refusal(ws_client("repo=rw&id=t1"))  # allowed: reaches the shell, in the repo
    assert ws_client.started == [repos[0].path]


# a real shell

def _has_pty() -> bool:
    if os.name != "nt":
        return True
    try:
        import winpty  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _has_pty(), reason="no pseudo-terminal support (pywinpty not installed)")
async def test_shell_round_trip(tmp_path):
    if os.name == "nt":
        argv, command = terminal.shell_command(None), b"Write-Output ('hel' + 'lo-pa')\r"
    else:
        argv, command = ["/bin/sh"], b"echo hel'lo'-pa\r"
    session = terminal.Session(argv, tmp_path, 100, 30)
    output = b""

    async def until(text: bytes) -> None:
        nonlocal output
        while text not in output:
            data = await session.read()
            assert data is not None, output
            output += data

    try:
        session.resize(120, 40)
        session.write(command)
        await asyncio.wait_for(until(b"hello-pa"), 30)  # only the command's output, not its echo, says this
    finally:
        await session.close()

    async def drain() -> None:
        while await session.read() is not None:
            pass

    await asyncio.wait_for(drain(), 10)  # closing ends the output stream...
    alive = session.pty.proc.isalive() if os.name == "nt" else session.exit_code() is None
    assert not alive  # ...and the shell
