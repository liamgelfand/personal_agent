"""Integrated terminal: the user's own shell in a pseudo-terminal (ConPTY on Windows), streamed to the IDE.

The shell runs in a workspace the server resolves (a writable repo, a task worktree, or the scratch
dir); a path never comes from the client. See the /api/terminal/ws route in server.py for who may open one.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from .config import SCRATCH_DIR, Access, Config
from .store import Store
from .workspace import get_workspace

MAX_TERMINALS = 8
READ_SIZE = 65536


class TerminalError(Exception):
    pass


def resolve_cwd(config: Config, store: Store, repo: str | None, task: str | None = None) -> Path:
    """The shell's working directory: a configured repo or task worktree, or the scratch dir for no repo.

    Raises WorkspaceError for an unknown repo or task, TerminalError for a read-only repo: a shell
    could edit anything there, which would break the read-only guarantee.
    """
    if not repo:
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        return SCRATCH_DIR.resolve()
    ws = get_workspace(config, store, repo, task)
    if ws.access is not Access.WRITE:
        raise TerminalError(f"'{repo}' is read-only, so it has no terminal - set access: write in config.yaml")
    if not ws.path.is_dir():
        raise TerminalError(f"{ws.path} no longer exists")
    return ws.path


def shell_command(setting: str | list[str] | None) -> list[str]:
    if isinstance(setting, str):
        return [setting]
    if setting:
        return list(setting)
    if os.name == "nt":
        return [shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe", "-NoLogo"]
    return [os.environ.get("SHELL") or "/bin/bash"]


def _env() -> dict[str, str]:
    return {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}


def _clamp(cols: int, rows: int) -> tuple[int, int]:
    return max(2, min(int(cols), 1000)), max(1, min(int(rows), 500))


class _WindowsPty:
    def __init__(self, argv: list[str], cwd: Path, cols: int, rows: int):
        from winpty import PtyProcess

        self.proc = PtyProcess.spawn(argv, cwd=str(cwd), env=_env(), dimensions=(rows, cols))
        self.pid = self.proc.pid

    def read(self) -> bytes:
        try:
            return self.proc.read(READ_SIZE).encode()
        except (EOFError, OSError):
            return b""

    def write(self, data: bytes, binary: bool = False) -> None:
        # ConPTY takes text. Binary input (e.g. legacy mouse reports) is one character per byte, as xterm.js means it.
        self.proc.write(data.decode("latin-1") if binary else data.decode("utf-8", errors="replace"))

    def resize(self, cols: int, rows: int) -> None:
        self.proc.setwinsize(rows, cols)

    def release(self) -> None:
        pass

    def exit_code(self) -> int | None:
        return self.proc.exitstatus

    def kill(self) -> None:
        # The shell's whole process tree: closing the console alone leaves programs started from it running.
        if self.proc.isalive():
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.pid)], capture_output=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.proc.close(force=True)
        except OSError:
            pass


class _PosixPty:
    def __init__(self, argv: list[str], cwd: Path, cols: int, rows: int):
        import pty

        env = _env()
        pid, fd = pty.fork()  # the child leads a new session with the pty as its controlling terminal
        if pid == 0:
            try:
                os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.status: int | None = None
        # kill() wakes the reader through this pipe rather than closing the pty under it: a background job
        # that ignores SIGHUP can keep the pty open, and a closed fd number may be reused by another file.
        self._wake_r, self._wake_w = os.pipe()
        self.resize(cols, rows)

    def read(self) -> bytes:
        import select

        try:
            ready, _, _ = select.select([self.fd, self._wake_r], [], [])
            if self._wake_r in ready:
                return b""
            return os.read(self.fd, READ_SIZE)
        except OSError:  # EIO once the shell has exited
            return b""

    def release(self) -> None:
        """Close the pty and the wake pipe, once the reader has stopped reading (Session._kill)."""
        for fd in (self.fd, self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def write(self, data: bytes, binary: bool = False) -> None:
        while data:
            data = data[os.write(self.fd, data):]

    def resize(self, cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _reap(self) -> bool:
        if self.status is None:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return True
            if pid == 0:
                return False
            self.status = os.waitstatus_to_exitcode(status)
        return True

    def exit_code(self) -> int | None:
        self._reap()
        return self.status

    def kill(self) -> None:
        # Hang up the session (as closing a terminal window does), then kill what is left of its process group.
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.killpg(self.pid, sig)
            except OSError:
                pass
            deadline = time.monotonic() + 1
            while not self._reap() and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.status is not None:
                break
        try:
            os.write(self._wake_w, b"x")  # the reader stops and closes the fds (release)
        except OSError:
            pass


class Session:
    """One shell. Output is read on its own thread (pty reads block) and handed to the event loop."""

    def __init__(self, argv: list[str], cwd: Path, cols: int = 80, rows: int = 24):
        cols, rows = _clamp(cols, rows)
        backend = _WindowsPty if os.name == "nt" else _PosixPty
        try:
            self.pty = backend(argv, cwd, cols, rows)
        except ImportError as exc:
            raise TerminalError("the terminal needs pywinpty - run uv sync") from exc
        except Exception as exc:  # the shell could not start, e.g. a wrong terminal.shell
            raise TerminalError(f"could not start {argv[0]}: {exc}") from exc
        self.cwd = cwd
        self.closed = False
        self._loop = asyncio.get_running_loop()
        self._output: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True, name=f"terminal-{self.pty.pid}")
        self._reader.start()

    def _pump(self) -> None:
        while True:
            data = self.pty.read()
            try:
                self._loop.call_soon_threadsafe(self._output.put_nowait, data or None)
            except RuntimeError:  # the event loop is gone (server shutting down)
                return
            if not data:
                return

    async def read(self) -> bytes | None:
        """The next chunk of output (whatever else is already waiting, joined), or None once the shell exits."""
        data = await self._output.get()
        if data is None:
            return None
        while not self._output.empty():
            more = self._output.get_nowait()
            if more is None:
                self._output.put_nowait(None)
                break
            data += more
        return data

    def write(self, data: bytes, binary: bool = False) -> None:
        """Send input: UTF-8 text, or with `binary`, raw bytes (xterm.js onBinary)."""
        if not self.closed:
            try:
                self.pty.write(data, binary)
            except (EOFError, OSError):
                pass  # the shell exited; read() reports it

    def resize(self, cols: int, rows: int) -> None:
        if not self.closed:
            try:
                self.pty.resize(*_clamp(cols, rows))
            except OSError:
                pass

    def exit_code(self) -> int | None:
        return self.pty.exit_code()

    async def close(self) -> None:
        """Kill the shell and the programs it started. Safe to call more than once."""
        if self.closed:
            return
        self.closed = True  # writes and resizes stop here, before the pty's fds are closed
        await asyncio.to_thread(self._kill)

    def _kill(self) -> None:
        self.pty.kill()
        # Close the pty only once the reader is done with it; if it is somehow still blocked, leak the fds
        # rather than let it read from a reused fd number.
        self._reader.join(timeout=5)
        if not self._reader.is_alive():
            self.pty.release()


class Terminals:
    """The open shells, by the id the client gave each connection, capped at `limit`."""

    def __init__(self, limit: int = MAX_TERMINALS):
        self.limit = limit
        self.sessions: dict[str, Session] = {}

    async def open(self, key: str, argv: list[str], cwd: Path, cols: int = 80, rows: int = 24) -> Session:
        # Refused, never replaced: a late-arriving stale connection must not kill the live shell.
        if key in self.sessions:
            raise TerminalError("this terminal id is already in use")
        if len(self.sessions) >= self.limit:
            raise TerminalError(f"at most {self.limit} terminals can be open at once - close one first")
        session = Session(argv, cwd, cols, rows)
        self.sessions[key] = session
        return session

    async def close(self, key: str, session: Session) -> None:
        if self.sessions.get(key) is session:
            del self.sessions[key]
        await session.close()

    async def close_all(self) -> None:
        sessions, self.sessions = list(self.sessions.values()), {}
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)
