"""Remote access: a shared-secret login for when the server listens beyond localhost."""

from __future__ import annotations

import hmac
import secrets
import socket

from .config import DATA_DIR

TOKEN_PATH = DATA_DIR / "remote_token"
COOKIE = "pa_session"

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Personal Agent - Sign in</title>
<style>
  :root { color-scheme: light dark; --bg:#f6f6f7; --surface:#fff; --border:#d4d4d8; --text:#17181b; --muted:#55575e; --primary:#17181b; --primary-text:#fff; --danger:#c93434; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0b0c0e; --surface:#111214; --border:#303338; --text:#e8e9eb; --muted:#a1a3a9; --primary:#e8e9eb; --primary-text:#0b0c0e; --danger:#ef5b5b; } }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; padding:16px; background:var(--bg); color:var(--text);
         font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif; }
  form { width:100%; max-width:360px; background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:24px; display:flex; flex-direction:column; gap:12px; }
  h1 { font-size:16px; margin:0; } p { margin:0; color:var(--muted); font-size:13px; }
  input { height:36px; padding:0 10px; border:1px solid var(--border); border-radius:6px; background:var(--bg); color:var(--text); font:inherit; }
  button { height:36px; border:0; border-radius:6px; background:var(--primary); color:var(--primary-text); font:inherit; font-weight:600; }
  .err { color:var(--danger); font-size:13px; min-height:1em; }
</style></head>
<body><form id="f">
  <h1>Personal Agent</h1>
  <p>Enter the access token from <code>data/remote_token</code> on your computer.</p>
  <input id="t" type="password" autocomplete="current-password" placeholder="Access token" autofocus required>
  <button type="submit">Sign in</button>
  <div class="err" id="e"></div>
</form>
<script>
document.getElementById('f').onsubmit = async (ev) => {
  ev.preventDefault();
  const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                        body: JSON.stringify({ token: document.getElementById('t').value }) });
  if (r.ok) location.href = '/'; else document.getElementById('e').textContent = 'That token is not valid.';
};
</script></body></html>"""


def token() -> str:
    if not TOKEN_PATH.exists():
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(secrets.token_urlsafe(24))
    return TOKEN_PATH.read_text().strip()


def valid(candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, token())


def addresses() -> list[str]:
    """This machine's IPv4 addresses, Tailscale (100.x) first."""
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    found.discard("127.0.0.1")
    return sorted(found, key=lambda ip: (not ip.startswith("100."), ip))
