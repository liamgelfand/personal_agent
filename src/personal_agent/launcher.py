"""Desktop launcher: start the server in the background if needed, then open the app in its own window.

The window is pywebview on Edge WebView2 (built into Windows 11), not a browser, so it has the app's
own icon, opens maximized, and pins to the taskbar as Personal Agent. Closing the window leaves the
server running, so agents keep working; launching again reuses it. Only one window runs at a time:
launching again brings the open one to the front.

    personal-agent-app                     open the app (starting the server if needed)
    personal-agent-app --tab               open in a normal browser tab instead
    personal-agent-app --install-shortcut  add Desktop and Start menu shortcuts (Windows)
    personal-agent-app --stop              stop the background server
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from .config import DATA_DIR, PROJECT_ROOT, load_config

APP_NAME = "Personal Agent"
# Ties the window and the shortcuts together, so the taskbar groups and pins them as one app with the
# shortcut's icon (rather than as pythonw.exe with Python's).
APP_USER_MODEL_ID = "LiamGelfand.PersonalAgent"
MUTEX_NAME = "Local\\personal-agent-window"
LOG_PATH = DATA_DIR / "logs" / "server.log"
# Cookies and localStorage persist here between launches.
WEBVIEW_DIR = DATA_DIR / "webview"
ICON_PNG = PROJECT_ROOT / "frontend" / "public" / "icon-512.png"
ICON_ICO = DATA_DIR / "icon.ico"


def _base_url() -> str:
    return f"http://127.0.0.1:{load_config().port}"


def server_running() -> bool:
    try:
        with urllib.request.urlopen(f"{_base_url()}/api/admin", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def start_server() -> None:
    """Start the supervisor detached from this process, with output going to a log file."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # On Windows: the console python with a hidden console (CREATE_NO_WINDOW). The git and Claude
    # Code processes the server starts inherit that hidden console instead of each flashing a window.
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe" and (python.parent / "python.exe").exists():
        python = python.parent / "python.exe"
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    log = open(LOG_PATH, "ab")
    subprocess.Popen(
        [str(python), "-c", "from personal_agent import main; main()"],
        cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=flags, start_new_session=os.name != "nt",
    )


def wait_for_server(timeout: float = 60) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_running():
            return True
        time.sleep(0.5)
    return False


def open_in_browser(url: str) -> None:
    """Open a link in the default browser."""
    webbrowser.open(url)


def stop_server() -> bool:
    request = urllib.request.Request(
        f"{_base_url()}/api/admin/shutdown", method="POST", headers={"Origin": _base_url()}
    )
    try:
        urllib.request.urlopen(request, timeout=5)
        return True
    except OSError:
        return False


# --- the app window ---------------------------------------------------------------------------

def _single_instance():
    """A named mutex held while the window is open, or None when another copy already holds it."""
    if os.name != "nt":
        return object()
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if handle and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle  # released by Windows when this process exits


def focus_existing_window() -> bool:
    """Restore and bring the open app window to the front. Matches only WinForms windows (what
    pywebview creates), so an Explorer window on a folder named "Personal Agent" isn't picked."""
    if os.name != "nt":
        return False
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        title, cls = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        user32.GetClassNameW(hwnd, cls, 256)
        if title.value == APP_NAME and cls.value.startswith("WindowsForms"):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        return False
    if user32.IsIconic(found[0]):
        user32.ShowWindow(found[0], 9)  # SW_RESTORE
    user32.SetForegroundWindow(found[0])
    return True


def _tag_window(hwnd: int) -> None:
    """Give the window itself the app's AppUserModelID too, so the taskbar matches it to the shortcut."""
    import pythoncom
    from win32com.propsys import propsys, pscon

    store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_USER_MODEL_ID, pythoncom.VT_LPWSTR))


def run_window(url: str) -> None:
    import webview

    if os.name == "nt":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    if not ICON_ICO.exists():
        _write_ico(ICON_PNG, ICON_ICO)
    # Links that open a new window (target=_blank, window.open) go to the default browser.
    window = webview.create_window(APP_NAME, url, maximized=True, min_size=(800, 560), text_select=True,
                                   background_color="#0b0c0e")
    if os.name == "nt":
        window.events.shown += lambda: _tag_window(window.native.Handle.ToInt64())
    webview.start(private_mode=False, storage_path=str(WEBVIEW_DIR), icon=str(ICON_ICO))


# --- shortcuts --------------------------------------------------------------------------------

def _write_ico(png: Path, ico: Path) -> None:
    """Wrap a PNG in an .ico container (Windows accepts PNG-compressed icon images)."""
    import struct

    data = png.read_bytes()
    ico.parent.mkdir(parents=True, exist_ok=True)
    # Header (reserved, type=1 icon, 1 image) + one directory entry (0 = 256 px) + the PNG.
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(data), 6 + 16)
    ico.write_bytes(header + entry + data)


def install_shortcuts() -> list[str]:
    if os.name != "nt":
        raise SystemExit("Shortcuts are only set up automatically on Windows.")
    import pythoncom
    from win32com.client import Dispatch
    from win32com.propsys import propsys, pscon
    from win32com.shell import shellcon

    target = shutil.which("personal-agent-app") or str(Path(sys.executable).parent / "personal-agent-app.exe")
    _write_ico(ICON_PNG, ICON_ICO)
    shell = Dispatch("WScript.Shell")
    paths = []
    for folder in ("Desktop", "Programs"):
        path = str(Path(shell.SpecialFolders(folder)) / f"{APP_NAME}.lnk")
        link = shell.CreateShortcut(path)
        link.TargetPath = target
        link.WorkingDirectory = str(PROJECT_ROOT)
        link.IconLocation = f"{ICON_ICO},0"
        link.Description = f"Open {APP_NAME}"
        link.Save()
        # The same AppUserModelID as the window: the taskbar then shows and pins the window as this shortcut.
        store = propsys.SHGetPropertyStoreFromParsingName(path, None, shellcon.GPS_READWRITE, propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_USER_MODEL_ID, pythoncom.VT_LPWSTR))
        store.Commit()
        paths.append(path)
    return paths


def _message(text: str) -> None:
    """Show an error even when started without a console (from a shortcut)."""
    if os.name == "nt" and Path(sys.executable).name.lower().startswith("pythonw"):
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)
    else:
        print(text, file=sys.stderr)


def main() -> None:
    args = set(sys.argv[1:])
    if "--install-shortcut" in args:
        for path in install_shortcuts():
            print(f"Created {path}")
        return
    if "--stop" in args:
        print("Stopped." if stop_server() else "The server was not running.")
        return
    tab = "--tab" in args
    if not tab and _single_instance() is None:
        if focus_existing_window():
            return
    if not server_running():
        start_server()
        if not wait_for_server():
            _message(f"The server did not start. See {LOG_PATH}")
            sys.exit(1)
    if tab:
        open_in_browser(_base_url())
    else:
        run_window(_base_url())


if __name__ == "__main__":
    main()
