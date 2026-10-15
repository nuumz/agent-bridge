"""Cursor ask-mode deny set: Write(**) + write-capable Shell, not Shell(*)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from bridgelib.adapters import Cursor  # noqa: E402


class CursorDenyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = root / "cli-config.json"
        self.state = root / "cursor-deny.json"
        self.lock = root / "cursor-deny.lock"
        self.cfg.write_text(json.dumps({
            "permissions": {"deny": ["Shell(git commit)", "Shell(*)"]},
        }, indent=2) + "\n")
        self.cur = Cursor()
        self.cur.CLI_CONFIG = self.cfg
        self.cur.DENY_STATE = self.state
        self.cur.DENY_LOCK = self.lock
        self.cur._held = False

    def tearDown(self):
        if self.cur._held:
            self.cur.finish(None)
        self.tmp.cleanup()

    def test_deny_entries_allow_read_shell(self):
        self.assertIn("Write(**)", Cursor.DENY_ENTRIES)
        self.assertNotIn("Shell(*)", Cursor.DENY_ENTRIES)
        self.assertIn("Shell(printf *)", Cursor.DENY_ENTRIES)
        for read_ok in ("Shell(git log)", "Shell(rg)", "Shell(cat)", "Shell(git diff)"):
            self.assertNotIn(read_ok, Cursor.DENY_ENTRIES)

    def test_acquire_strips_legacy_shell_star_and_adds_write_denies(self):
        note = self.cur._deny_acquire()
        self.assertIn("Shell(*) not denied", note)
        deny = json.loads(self.cfg.read_text())["permissions"]["deny"]
        self.assertNotIn("Shell(*)", deny)
        self.assertIn("Write(**)", deny)
        self.assertIn("Shell(printf *)", deny)
        self.assertIn("Shell(git commit)", deny)  # pre-existing user deny kept
        self.cur.finish(None)
        deny_after = json.loads(self.cfg.read_text())["permissions"]["deny"]
        self.assertNotIn("Write(**)", deny_after)
        self.assertNotIn("Shell(*)", deny_after)
        self.assertIn("Shell(git commit)", deny_after)

    def test_ask_preamble_mentions_readonly_shell(self):
        o = mock.Mock(mode="ask", worktree=None)
        text = self.cur.preamble(o)
        self.assertIn("Read-only run", text)
        self.assertIn("git log", text)


if __name__ == "__main__":
    unittest.main()
