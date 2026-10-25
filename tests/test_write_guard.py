"""Write-mode guard: a `-m write` peer in the caller's checkout cannot move or discard the caller's uncommitted work
(git restore / checkout / switch / stash / reset / clean are denied); a worktree the bridge owns (`-w`, or bridge-work's
branch `bridge/<name>`) keeps them, so a failed attempt there can still be discarded."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
from bridgelib import core  # noqa: E402
from bridgelib.adapters import ADAPTERS, Cursor  # noqa: E402


def opts(**kw) -> argparse.Namespace:
    base = dict(mode="write", board=False, deny=[], model=None, add_dir=[], worktree=None, session=None, cont=False)
    return argparse.Namespace(**{**base, **kw})


def git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


def repo(path: str, branch: str) -> None:
    git(path, "init", "-q", "-b", branch)
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")


class Allowlists(unittest.TestCase):
    def test_write_allowlist_no_longer_pre_approves_discard_verbs(self):
        for verb in core.GIT_DISCARD:
            self.assertFalse(any(c == verb or c.startswith(verb + " ") for c in core.CLAUDE_WRITE_BASH), verb)
        self.assertIn("git stash list", core.CLAUDE_WORKTREE_BASH)         # reads, but `git stash` is denied: moved
        self.assertIn("git add", core.CLAUDE_WRITE_BASH)

    def test_bridge_owned_is_the_bridge_work_branch_only(self):
        with tempfile.TemporaryDirectory() as owned, tempfile.TemporaryDirectory() as mine, \
                tempfile.TemporaryDirectory() as plain:
            repo(owned, "bridge/x")
            repo(mine, "main")
            self.assertTrue(core.bridge_owned(owned))
            self.assertFalse(core.bridge_owned(mine))
            self.assertFalse(core.bridge_owned(plain))

    def test_dirty_paths_counts_tracked_changes_only(self):
        with tempfile.TemporaryDirectory() as d:
            repo(d, "main")
            (Path(d) / "a.txt").write_text("1")
            git(d, "add", "a.txt")
            git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "a")
            self.assertEqual(core.dirty_paths(d), [])
            (Path(d) / "a.txt").write_text("2")
            (Path(d) / "scratch.txt").write_text("untracked")
            self.assertEqual(core.dirty_paths(d), ["a.txt"])


class ClaudeArgv(unittest.TestCase):
    def test_callers_checkout_denies_discard_verbs_and_drops_them_from_the_allowlist(self):
        a = ADAPTERS["claude"].argv(opts(), "q")
        deny, allow = a[a.index("--disallowed-tools") + 1], a[a.index("--allowed-tools") + 1]
        for verb in core.GIT_DISCARD:
            self.assertIn(f"Bash({verb}:*)", deny)
        self.assertIn("Bash(git -C:*)", deny)                             # the verb after an option is the same verb
        self.assertNotIn("Bash(git restore:*)", allow)
        self.assertNotIn("Bash(git stash list:*)", allow)
        self.assertNotIn("Bash(git checkout --:*)", allow)
        self.assertIn("git restore/checkout/switch/stash/reset/clean are denied", ADAPTERS["claude"].preamble(opts()))

    def test_bridge_owned_worktree_keeps_restore_and_checkout(self):
        a = ADAPTERS["claude"].argv(opts(discard_ok=True), "q")
        deny, allow = a[a.index("--disallowed-tools") + 1], a[a.index("--allowed-tools") + 1]
        self.assertNotIn("Bash(git restore:*)", deny)
        self.assertNotIn("Bash(git checkout:*)", deny)
        self.assertIn("Bash(git restore:*)", allow)
        self.assertIn("Bash(git checkout --:*)", allow)
        self.assertIn("Bash(git stash list:*)", allow)
        self.assertIn("Bash(git commit:*)", deny)                       # history stays the user's everywhere

    def test_read_only_runs_are_unchanged(self):
        a = ADAPTERS["claude"].argv(opts(mode="ask"), "q")
        self.assertNotIn("Bash(git restore:*)", a[a.index("--disallowed-tools") + 1])


class CopilotArgv(unittest.TestCase):
    def test_callers_checkout_denies_discard_verbs(self):
        a = ADAPTERS["copilot"].argv(opts(), "q")
        self.assertIn("--deny-tool=shell(git restore)", a)
        self.assertIn("--deny-tool=shell(git checkout:*)", a)
        self.assertIn("--deny-tool=shell(git -C:*)", a)
        self.assertIn("--deny-tool=shell(git commit)", a)
        owned = ADAPTERS["copilot"].argv(opts(discard_ok=True), "q")
        self.assertNotIn("--deny-tool=shell(git restore)", owned)
        self.assertIn("--deny-tool=shell(git commit)", owned)


class CursorHold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = root / "cli-config.json"
        self.cfg.write_text(json.dumps({"permissions": {"deny": ["Shell(git commit)"]}}, indent=2) + "\n")
        self.cur = Cursor()
        self.cur.CLI_CONFIG, self.cur.DENY_STATE, self.cur.DENY_LOCK = self.cfg, root / "deny.json", root / "deny.lock"
        self.cur._held = False
        self.repo = root / "repo"
        self.repo.mkdir()
        repo(str(self.repo), "main")

    def tearDown(self):
        if self.cur._held:
            self.cur.finish(None)
        self.tmp.cleanup()

    def deny(self) -> list[str]:
        return json.loads(self.cfg.read_text())["permissions"]["deny"]

    def test_write_run_in_the_callers_checkout_holds_discard_and_history_denies(self):
        self.cur.prepare(opts(dir=str(self.repo)), False)
        deny = self.deny()
        for e in ("Shell(git restore)", "Shell(git restore *)", "Shell(git checkout *)", "Shell(git stash)",
                  "Shell(git -C *)", "Shell(git push *)", "Shell(git tag *)"):
            self.assertIn(e, deny)
        self.assertNotIn("Write(**)", deny)                              # a write run keeps its edits
        self.cur.finish(None)
        self.assertEqual(self.deny(), ["Shell(git commit)"])             # byte-identical restore

    def test_bridge_owned_worktree_holds_history_denies_only(self):
        self.cur.prepare(opts(dir=str(self.repo), discard_ok=True), False)
        deny = self.deny()
        self.assertIn("Shell(git push *)", deny)
        self.assertNotIn("Shell(git restore)", deny)

    def other(self) -> Cursor:
        other = Cursor()
        other.CLI_CONFIG, other.DENY_STATE = self.cur.CLI_CONFIG, self.cur.DENY_STATE
        other.DENY_LOCK = self.cur.DENY_LOCK
        other._held = False
        return other

    def test_a_read_only_run_in_another_process_adds_its_set_on_top_and_its_death_is_swept(self):
        self.cur.prepare(opts(dir=str(self.repo)), False)               # write hold: this pid
        child = subprocess.Popen([sys.executable, "-c", (
            "import sys, time; from pathlib import Path; sys.path.insert(0, sys.argv[1])\n"
            "from bridgelib.adapters import Cursor\n"
            "c = Cursor(); c.CLI_CONFIG, c.DENY_STATE, c.DENY_LOCK = (Path(p) for p in sys.argv[2:5])\n"
            "c._deny_acquire(); time.sleep(60)"),
            str(BIN), str(self.cur.CLI_CONFIG), str(self.cur.DENY_STATE), str(self.cur.DENY_LOCK)])
        try:
            for _ in range(100):
                try:                                            # read outside the lock: the child may be mid-write
                    if child.pid in json.loads(self.cur.DENY_STATE.read_text()).get("pids", []):
                        break
                except (OSError, json.JSONDecodeError):
                    pass
                __import__("time").sleep(0.05)
            deny = self.deny()
            self.assertIn("Write(**)", deny)                              # the ask set landed on top of the write hold
            self.assertIn("Shell(git restore)", deny)
            self.assertEqual(self.cur._deny_holders(), [child.pid])     # and it is the holder a write run refuses under
        finally:
            child.kill()
            child.wait()
        self.cur.finish(None)                           # last live holder out: the dead one is swept, config restored
        self.assertEqual(self.deny(), ["Shell(git commit)"])

    def test_an_edit_by_cursor_agent_between_two_holds_survives_the_restore(self):
        self.cur.prepare(opts(dir=str(self.repo)), False)
        cfg = json.loads(self.cfg.read_text())
        cfg["model"] = {"modelId": "picked-meanwhile"}                   # cursor-agent rewrote its own config
        self.cfg.write_text(json.dumps(cfg, indent=2) + "\n")
        other = self.other()
        other._deny_acquire()
        other.finish(None)
        self.cur.finish(None)
        after = json.loads(self.cfg.read_text())
        self.assertEqual(after["model"], {"modelId": "picked-meanwhile"})
        self.assertEqual(after["permissions"]["deny"], ["Shell(git commit)"])

    def test_write_refusal_fires_on_a_write_deny_holder_not_on_another_write_run(self):
        self.cur.prepare(opts(dir=str(self.repo)), False)               # a write hold is not a Write(**) hold
        self.assertEqual(self.cur._deny_holders(), [])
        other = self.other()
        other._deny_acquire()
        self.assertEqual(self.cur._deny_holders(), [os.getpid()])
        other.finish(None)

    def test_an_ask_hold_counts_even_when_the_user_already_denies_write(self):
        self.cfg.write_text(json.dumps({"permissions": {"deny": ["Write(**)"]}}) + "\n")
        self.cur._deny_acquire()
        self.assertEqual(self.cur._deny_holders(), [os.getpid()])


class AskAgentWiring(unittest.TestCase):
    """`ask-agent --dry-run` sets discard_ok from the branch and -w, and warns once about a dirty checkout."""

    def run_dry(self, cwd: str, *extra: str) -> tuple[dict, str]:
        with tempfile.TemporaryDirectory() as home:
            env = {k: v for k, v in os.environ.items() if k != core.HOP_VAR}
            env.update(HOME=home, USER="tester")
            out = subprocess.run([str(BIN / "ask-agent"), "claude", "-m", "write", "--dry-run", "-d", cwd, *extra, "q"],
                                 capture_output=True, text=True, env=env, cwd=cwd)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout), out.stderr

    def test_callers_checkout_is_protected_and_a_dirty_tree_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            repo(d, "main")
            (Path(d) / "a.txt").write_text("1")
            git(d, "add", "a.txt")
            git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "a")
            clean, err = self.run_dry(d)
            self.assertIn("Bash(git restore:*)", clean["argv"][clean["argv"].index("--disallowed-tools") + 1])
            self.assertNotIn("uncommitted", err)
            (Path(d) / "a.txt").write_text("2")
            _, err = self.run_dry(d)
            self.assertIn("[bridge] write mode in your checkout: 1 uncommitted path (a.txt)", err)
            with_wt, err = self.run_dry(d, "-w", "x")
            self.assertNotIn("Bash(git restore:*)", with_wt["argv"][with_wt["argv"].index("--disallowed-tools") + 1])
            self.assertNotIn("uncommitted", err)

    def test_bridge_work_branch_is_not_protected(self):
        with tempfile.TemporaryDirectory() as d:
            repo(d, "bridge/x")
            owned, _ = self.run_dry(d)
            self.assertNotIn("Bash(git restore:*)", owned["argv"][owned["argv"].index("--disallowed-tools") + 1])


if __name__ == "__main__":
    unittest.main()
