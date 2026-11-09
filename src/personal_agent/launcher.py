"""Desktop launcher: start the server in the background if needed, then open the app.

Opens in your default browser. When that browser is Chromium-based (Chrome, Edge, Brave,
Vivaldi) it uses app mode, a standalone window without tabs or an address bar.

    personal-agent-app                     open the app (starting the server if needed)
    personal-agent-app --tab               open in a normal browser tab instead
    personal-agent-app --install-shortcut  add Desktop and Start menu shortcuts (Windows)
    personal-agent-app --stop              stop the background server
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from .config import DATA_DIR, PROJECT_ROOT, load_config

APP_MODE_BROWSERS = {"chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe"}
CHROME_PATHS = [
    Path(os.environ.get(var, "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
]
LOG_PATH = DATA_DIR / "logs" / "server.log"
BROWSER_PROFILE = DATA_DIR / "browser"
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


def default_browser() -> Path | None:
    """The default browser's executable on Windows, read from the http URL association."""
    if os.name != "nt":
        return None
    import shlex
    import winreg

    try:
        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            prog_id = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as k:
            command = winreg.QueryValueEx(k, "")[0]
        return Path(shlex.split(command, posix=False)[0].strip('"'))
    except (OSError, IndexError, ValueError):
        return None


def web_app_id(manifest_id: str) -> str:
    """Chromium's id for a web app: the first 128 bits of SHA-256 of its manifest id, in hex digits a-p."""
    digest = hashlib.sha256(manifest_id.encode()).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def installed_app_id(url: str) -> str | None:
    """The id of this app if it was installed from the app window's menu (Install Personal Agent).

    The profile also holds apps Chrome preinstalls (Google Drive, Docs, Gmail, YouTube...), so look for
    this app's own id. Its manifest has no "id", so Chrome derives it from start_url ("/").
    """
    app_id = web_app_id(f"{url.rstrip('/')}/")
    resources = BROWSER_PROFILE / "Default" / "Web Applications" / "Manifest Resources"
    return app_id if (resources / app_id).is_dir() else None


def open_app(url: str, tab: bool = False) -> None:
    browser = default_browser()
    if browser is None or not browser.exists():
        browser = next((p for p in CHROME_PATHS if p.exists()), None)  # fall back to Chrome
    if not tab and browser and browser.name.lower() in APP_MODE_BROWSERS:
        # A dedicated profile makes this its own browser process: a window handed to an already running
        # browser ignores --start-maximized, and only an installed app (see installed_app_id) pins with
        # its own icon instead of the browser's.
        app_id = installed_app_id(url)
        target = f"--app-id={app_id}" if app_id else f"--app={url}"
        subprocess.Popen([str(browser), f"--user-data-dir={BROWSER_PROFILE}", target, "--start-maximized"])
    elif browser and browser.exists():
        subprocess.Popen([str(browser), url])
    else:
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
    target = shutil.which("personal-agent-app") or str(Path(sys.executable).parent / "personal-agent-app.exe")
    _write_ico(ICON_PNG, ICON_ICO)
    script = f"""
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @($shell.SpecialFolders('Desktop'), $shell.SpecialFolders('Programs'))) {{
  $link = $shell.CreateShortcut((Join-Path $folder 'Personal Agent.lnk'))
  $link.TargetPath = '{target}'
  $link.WorkingDirectory = '{PROJECT_ROOT}'
  $link.IconLocation = '{ICON_ICO}'
  $link.Description = 'Open Personal Agent'
  $link.Save()
  Write-Output $link.FullName
}}"""
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line.strip()]


def _message(text: str) -> None:
    """Show an error even when started without a console (from a shortcut)."""
    if os.name == "nt" and Path(sys.executable).name.lower().startswith("pythonw"):
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "Personal Agent", 0x10)
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
    if not server_running():
        start_server()
        if not wait_for_server():
            _message(f"The server did not start. See {LOG_PATH}")
            sys.exit(1)
    open_app(_base_url(), tab="--tab" in args)


if __name__ == "__main__":
    main()
