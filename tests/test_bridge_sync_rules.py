"""bridge-sync rule files — every installed peer gets the bridge note on a fresh machine; a user's file is never clobbered."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"


def _load():
    loader = importlib.machinery.SourceFileLoader("bridge_sync_rules_mod", str(BIN / "bridge-sync"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class RuleFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bs = _load()
        cls.bs.QUIET = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        (home / ".cursor" / "rules").mkdir(parents=True)
        (home / ".copilot").mkdir()
        peers = copy.deepcopy(self.bs.PEERS)
        for name, peer in peers.items():
            for key in ("rules", "skills", "note_file", "also_reads"):
                if key in peer:
                    peer[key] = home / peer[key].relative_to(self.bs.HOME)
        self.peers = peers
        self.home = home
        self.patches = [mock.patch.object(self.bs, "PEERS", peers),
                        mock.patch.object(self.bs, "CLAUDE_MD", home / ".claude" / "CLAUDE.md"),
                        mock.patch.object(self.bs, "RULES_MIRROR", home / "mirror" / "user-rules.md"),
                        mock.patch.object(self.bs, "BRIDGE_NOTE", home / "mirror" / "agent-bridge.md"),
                        mock.patch.object(self.bs, "DISABLED", {})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_fresh_machine_without_claude_md_still_gets_the_bridge_note(self):
        self.bs.sync_rules()
        note = self.peers["cursor"]["note_file"].read_text()
        self.assertIn(self.bs.MARKER, note)
        self.assertIn("alwaysApply: true", note)
        self.assertIn("One hop only", note)                      # BRIDGE_NOTE_BODY
        self.assertIn(self.bs.CURSOR_PULL, note)                 # the Cursor-only board contract
        self.assertFalse(self.peers["cursor"]["rules"].exists())  # nothing to mirror → no empty user-rules file
        copilot = self.peers["copilot"]["rules"].read_text()
        self.assertIn("One hop only", copilot)
        self.assertNotIn("you pull", copilot)                    # CURSOR_PULL never reaches another peer

    def test_claude_md_is_mirrored_next_to_the_note(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "CLAUDE.md").write_text("# Rules\n- line length 120\n")
        self.bs.sync_rules()
        self.assertIn("line length 120", self.peers["cursor"]["rules"].read_text())
        self.assertIn("line length 120", self.peers["copilot"]["rules"].read_text())
        self.assertNotIn("line length 120", self.peers["cursor"]["note_file"].read_text())

    def test_user_owned_note_is_left_alone(self):
        mine = "---\ndescription: my own\n---\nmy rule\n"
        self.peers["cursor"]["note_file"].write_text(mine)
        self.bs.sync_rules()
        self.assertEqual(self.peers["cursor"]["note_file"].read_text(), mine)

    def test_sync_is_idempotent(self):
        self.bs.sync_rules()
        first = self.peers["cursor"]["note_file"].read_text()
        self.bs.sync_rules()
        self.assertEqual(self.peers["cursor"]["note_file"].read_text(), first)

    def test_a_mirror_that_would_leak_the_commit_recipe_is_not_written(self):
        (self.home / ".claude").mkdir()
        md = self.home / ".claude" / "CLAUDE.md"
        md.write_text("# Rules\n- line length 120\n")
        self.bs.sync_rules()
        before = self.peers["copilot"]["rules"].read_text()
        # an unterminated skip marker: the region regex cannot strip it, and the grant recipe sits right after it
        md.write_text("# Rules\n- line length 120\n- <!-- bridge:skip -->run `commit-autonomy on -- go` first\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            leaked = self.bs.sync_rules()
        self.assertCountEqual(leaked, ["<!-- bridge:skip -->", "commit-autonomy on"])
        self.assertIn("not written", err.getvalue())
        self.assertEqual(self.peers["copilot"]["rules"].read_text(), before)      # the last good mirror stays
        self.assertNotIn("commit-autonomy", (self.home / "mirror" / "user-rules.md").read_text())
        self.peers["copilot"]["rules"].unlink()                                    # a peer with no mirror yet
        with contextlib.redirect_stderr(io.StringIO()):
            self.bs.sync_rules()
        fresh = self.peers["copilot"]["rules"].read_text()
        self.assertIn("One hop only", fresh)                                        # still gets the bridge note
        self.assertNotIn("line length 120", fresh)                                  # and no user rules at all
        self.assertNotIn("commit-autonomy", fresh)

    def test_a_missing_import_is_said_not_pasted(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "CLAUDE.md").write_text("# Rules\n@gone.md\n- keep\n")
        out = io.StringIO()
        self.bs.QUIET = False
        try:
            with contextlib.redirect_stdout(out):
                self.bs.sync_rules()
        finally:
            self.bs.QUIET = True
        mirror = self.peers["copilot"]["rules"].read_text()
        self.assertNotIn("\n@gone.md", mirror)
        self.assertIn("gone.md", out.getvalue())
        self.assertIn("not found", out.getvalue())


class Help(unittest.TestCase):
    def test_help_prints_usage_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home}
            r = subprocess.run([sys.executable, str(BIN / "bridge-sync"), "--help"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("usage: bridge-sync", r.stdout)
            self.assertEqual(os.listdir(home), [])



class RetirePaths(RuleFiles):
    def test_disabled_peer_loses_both_generated_files_but_not_a_users(self):
        self.bs.sync_rules()
        self.assertTrue(self.peers["cursor"]["note_file"].exists())
        mine = "---\ndescription: mine\n---\nkeep\n"
        self.peers["cursor"]["rules"].write_text(mine)                 # user-owned, no marker
        with mock.patch.object(self.bs, "DISABLED", {"cursor": "firewall"}):
            self.bs.sync_rules()
        self.assertFalse(self.peers["cursor"]["note_file"].exists())
        self.assertEqual(self.peers["cursor"]["rules"].read_text(), mine)

    def test_mirror_is_retired_when_claude_md_disappears(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "CLAUDE.md").write_text("# Rules\n- a\n")
        self.bs.sync_rules()
        self.assertTrue(self.peers["cursor"]["rules"].exists())
        (self.home / ".claude" / "CLAUDE.md").unlink()
        self.bs.sync_rules()
        self.assertFalse(self.peers["cursor"]["rules"].exists())       # bridge-owned mirror of a gone source
        self.assertTrue(self.peers["cursor"]["note_file"].exists())    # the bridge note has no such source


class PluginInstall(unittest.TestCase):
    def test_subagents_are_not_linked_when_the_checkout_is_a_plugin(self):
        bs = _load()
        bs.QUIET = True
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plugin = home / ".claude" / "plugins" / "cache" / "x" / "agent-bridge" / "agents"
            plugin.mkdir(parents=True)
            (plugin / "peer-ask.md").write_text("---\nname: peer-ask\n---\n")
            agents = home / ".claude" / "agents"
            with mock.patch.object(bs, "AGENTS_SRC", plugin), mock.patch.object(bs, "CLAUDE_AGENTS", agents):
                bs.sync_agents()
            self.assertFalse(agents.exists())
            plain = home / "src" / "agent-bridge" / "agents"
            plain.mkdir(parents=True)
            (plain / "peer-ask.md").write_text("---\nname: peer-ask\n---\n")
            with mock.patch.object(bs, "AGENTS_SRC", plain), mock.patch.object(bs, "CLAUDE_AGENTS", agents):
                bs.sync_agents()
            self.assertTrue((agents / "peer-ask.md").is_symlink())



class BoardMcpRepair(unittest.TestCase):
    """The board MCP entry follows the checkout: a plugin version that was swept or an old clone is re-registered."""

    def setUp(self):
        self.bs = _load()
        self.bs.QUIET = True
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cursor_entry_from_another_checkout_is_rewritten(self):
        (self.home / ".cursor").mkdir()
        mcp = self.home / ".cursor" / "mcp.json"
        stale = {"mcpServers": {"bridge_board": {"type": "stdio", "command": "python3",
                                                  "args": ["/old/clone/bin/bridge-board", "serve"]},
                                "mine": {"command": "x"}}}
        mcp.write_text(json.dumps(stale))
        with mock.patch.object(self.bs, "CURSOR_MCP", mcp), mock.patch.object(self.bs, "DISABLED", {}), \
             mock.patch.object(self.bs.shutil, "which", lambda name: None):
            self.bs.sync_mcp()
        data = json.loads(mcp.read_text())
        self.assertEqual(data["mcpServers"]["bridge_board"]["args"], [str(self.bs.BOARD_BIN), "serve"])
        self.assertEqual(data["mcpServers"]["mine"], {"command": "x"})

    def test_claude_entry_from_another_checkout_is_removed_then_added(self):
        bindir = self.home / "bin"; bindir.mkdir()
        log = self.home / "calls.log"
        (bindir / "claude").write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
        (bindir / "claude").chmod(0o755)
        cj = self.home / ".claude.json"
        cj.write_text(json.dumps({"mcpServers": {"bridge_board": {"command": "python3", "args": ["/old/bin/bridge-board", "serve"]}}}))
        env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
        with mock.patch.object(self.bs, "CLAUDE_JSON", cj), mock.patch.object(self.bs, "DISABLED", {}), \
             mock.patch.object(self.bs, "CURSOR_MCP", self.home / "nocursor" / "mcp.json"), mock.patch.dict(os.environ, env):
            self.bs.sync_mcp()
        calls = log.read_text().splitlines()
        self.assertEqual(calls[0], "mcp remove -s user bridge_board")
        self.assertTrue(calls[1].startswith("mcp add -s user bridge_board -e AGENT_BRIDGE_AGENT=claude -- python3 "))
        self.assertIn(str(self.bs.BOARD_BIN), calls[1])


if __name__ == "__main__":
    unittest.main()
