"""Chat tab list logic (frontend/src/chatTabs.ts), run under Node's built-in TypeScript stripping."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = """
import assert from 'node:assert/strict'
const t = await import(process.argv[1])

assert.deepEqual(t.addChatTab(['a'], 'b'), ['a', 'b'])
assert.deepEqual(t.addChatTab(['a', 'b'], 'a'), ['a', 'b'])

// Closing the active tab picks the right neighbour, else the left, else nothing.
assert.deepEqual(t.closeChatTab({ ids: ['a', 'b', 'c'], active: 'b' }, 'b'), { ids: ['a', 'c'], active: 'c' })
assert.deepEqual(t.closeChatTab({ ids: ['a', 'b', 'c'], active: 'c' }, 'c'), { ids: ['a', 'b'], active: 'b' })
assert.deepEqual(t.closeChatTab({ ids: ['a'], active: 'a' }, 'a'), { ids: [], active: null })
assert.deepEqual(t.closeChatTab({ ids: ['a', 'b'], active: 'a' }, 'b'), { ids: ['a'], active: 'a' })
assert.deepEqual(t.closeChatTab({ ids: ['a'], active: 'a' }, 'x'), { ids: ['a'], active: 'a' })

assert.equal(t.cycleChatTab({ ids: ['a', 'b', 'c'], active: 'c' }, 1), 'a')
assert.equal(t.cycleChatTab({ ids: ['a', 'b', 'c'], active: 'a' }, -1), 'c')
assert.equal(t.cycleChatTab({ ids: ['a', 'b'], active: null }, 1), 'a')
assert.equal(t.cycleChatTab({ ids: [], active: null }, 1), null)

assert.deepEqual(t.pruneChatTabs(['a', 'gone', 'b', 'a', 3], new Set(['a', 'b'])), ['a', 'b'])
assert.deepEqual(t.pruneChatTabs(['a', 'b', 'a', null]), ['a', 'b'])
assert.deepEqual(t.pruneChatTabs('junk', new Set(['a'])), [])

// Tabs deleted elsewhere: the active one hands over to a surviving neighbour.
assert.deepEqual(t.removeChatTabs({ ids: ['a', 'b', 'c', 'd'], active: 'b' }, ['b', 'c']), { ids: ['a', 'd'], active: 'd' })
assert.deepEqual(t.removeChatTabs({ ids: ['a', 'b'], active: 'a' }, ['a', 'b']), { ids: [], active: null })
assert.deepEqual(t.removeChatTabs({ ids: ['a', 'b'], active: 'a' }, []), { ids: ['a', 'b'], active: 'a' })
"""


def test_chat_tab_logic():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    if tuple(int(p) for p in version.split(".")[:2]) < (22, 6):
        pytest.skip(f"node {version} cannot strip TypeScript types (needs 22.6+)")
    module = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "chatTabs.ts").as_uri()
    result = subprocess.run(
        [node, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", SCRIPT, module],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
