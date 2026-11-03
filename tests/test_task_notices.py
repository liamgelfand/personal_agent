"""Finished-task notification logic (frontend/src/taskNotices.ts), run under Node's built-in TypeScript stripping."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = """
import assert from 'node:assert/strict'
const t = await import(process.argv[1])

const now = 1_000_000
const day = 24 * 3600
const tasks = [
  { id: 'old', status: 'ready', updated_at: now - 8 * day },
  { id: 'failed', status: 'failed', updated_at: now - 2 * day },
  { id: 'ready', status: 'ready', updated_at: now - 60 },
  { id: 'running', status: 'implementing', updated_at: now },
  { id: 'merged', status: 'merged', updated_at: now },
  { id: 'seen', status: 'ready', updated_at: now },
]
assert.deepEqual(t.taskNotices(tasks, ['seen'], now).map((x) => x.id), ['ready', 'failed'])
assert.deepEqual(t.taskNotices(tasks, ['seen', 'ready', 'failed'], now), [])

assert.deepEqual(t.pruneDismissed(['a', 'b', 'a', 3, null]), ['a', 'b'])
assert.deepEqual(t.pruneDismissed('junk'), [])
assert.deepEqual(t.pruneDismissed(['a', 'b', 'c'], 2), ['b', 'c'])
"""


def test_task_notice_logic():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    if tuple(int(p) for p in version.split(".")[:2]) < (22, 6):
        pytest.skip(f"node {version} cannot strip TypeScript types (needs 22.6+)")
    module = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "taskNotices.ts").as_uri()
    result = subprocess.run(
        [node, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", SCRIPT, module],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
