"""Lets the suite run without a config.yaml (e.g. in a fresh task worktree).

Runs before any test module imports personal_agent, so config.CONFIG_PATH picks up the override.
An existing config.yaml, or a PERSONAL_AGENT_CONFIG already set, is left alone.
"""

import atexit
import importlib.util
import os
import shutil
import tempfile
from pathlib import Path

# Where config.py will look: next to whichever checkout `personal_agent` is imported from (found without importing it).
_package = importlib.util.find_spec("personal_agent")
_default = Path(_package.origin).resolve().parents[2] / "config.yaml"
if not os.environ.get("PERSONAL_AGENT_CONFIG") and not _default.exists():
    _dir = tempfile.mkdtemp(prefix="personal-agent-tests-")
    atexit.register(shutil.rmtree, _dir, ignore_errors=True)
    (Path(_dir) / "config.yaml").write_text("repos: []\n", encoding="utf-8")
    os.environ["PERSONAL_AGENT_CONFIG"] = str(Path(_dir) / "config.yaml")
