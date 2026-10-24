"""bridge-work's fix loop: a finding that comes back unchanged after two red verifies hands the next round to the other
peer, with the failed attempt spelled out as something not to repeat."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
from bridgelib import ledger


def _load():
    loader = importlib.machinery.SourceFileLoader("bridge_work_mod", str(BIN / "bridge-work"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


import argparse
import os
import subprocess
import tempfile
from unittest import mock

ROW_A = "| `src/x.py:12` | null check missing on `user` | high | yes | NPE on empty list |"
ROW_A2 = "| src/x.py:14 | Null check missing on user | high | yes | moved two lines |"
ROW_B = "| src/y.py:3 | wrong constant | low | yes | off by one |"


class Rounds(unittest.TestCase):
    def setUp(self):
        self.w = _load()

    def test_finding_key_ignores_line_case_and_punctuation(self):
        self.assertEqual(self.w.finding_key(ROW_A), self.w.finding_key(ROW_A2))
        self.assertNotEqual(self.w.finding_key(ROW_A), self.w.finding_key(ROW_B))
        self.assertEqual(self.w.finding_key(ROW_B), "src/y.py: wrong constant")
        self.assertEqual(self.w.repeated([ROW_A, ROW_B], [ROW_A2]), ["src/x.py: null check missing on user"])
        self.assertEqual(self.w.repeated([], [ROW_A]), [])

    def test_switch_needs_two_red_rounds_and_a_returning_finding(self):
        red_repeat = [{"verify_rc": 1, "repeat_findings": []}, {"verify_rc": 2, "repeat_findings": ["k"]}]
        self.assertTrue(self.w.should_switch(red_repeat))
        self.assertFalse(self.w.should_switch(red_repeat[1:]))                           # one round only
        self.assertFalse(self.w.should_switch([{"verify_rc": 0, "repeat_findings": []},
                                               {"verify_rc": 1, "repeat_findings": ["k"]}]))   # first was green
        self.assertFalse(self.w.should_switch([{"verify_rc": 1, "repeat_findings": []},
                                               {"verify_rc": 1, "repeat_findings": []}]))      # new findings each time
        self.assertFalse(self.w.should_switch([{"verify_rc": 1, "repeat_findings": ["k"]},
                                               {"impl_rc": 7, "impl": "quota"}]))               # no verify at all

    def test_next_hands_swaps_writer_and_reviewer(self):
        self.assertEqual(self.w.next_hands(["claude", "cursor"], "claude", "cursor"), ("cursor", "claude"))
        self.assertEqual(self.w.next_hands(["claude", "cursor", "copilot"], "claude", "copilot"), ("cursor", "copilot"))
        self.assertEqual(self.w.next_hands(["claude"], "claude", "claude"), ("claude", "claude"))   # nobody else

    def test_anti_pattern_block_quotes_the_failed_attempt(self):
        block = self.w.anti_pattern({"impl": "I added a try/except around the call.\nVERIFY: still red",
                                     "repeat_findings": ["src/x.py: null check missing on user"],
                                     "files": ["src/x.py"]}, "abcdef0123456789")
        lines = block.splitlines()
        self.assertTrue(lines[0].startswith("- already tried and FAILED"))
        self.assertEqual(lines[1], "    I added a try/except around the call.")               # quoted, never a header
        self.assertIn("findings that came back unchanged: src/x.py: null check missing on user", block)
        self.assertIn("`git checkout abcdef012345 -- src/x.py`", block)


class Loop(unittest.TestCase):
    """The round loop with scripted peers: a repeated finding after two red verifies hands round 3 to the other peer,
    once, and earns that round even with the default --rounds 2."""

    def run_loop(self, rounds: int, verify_rcs: list[int], reviews: list[str]) -> tuple[dict, list[str], str]:
        w = _load()
        calls: list[str] = []
        state = {"n": 0}

        def ask(peer, mode, cwd, prompt, timeout, conv, kind, peers=None):
            calls.append(f"{mode}:{peer}")
            if mode == "write":
                return 0, f"attempt by {peer}", "", peer
            i = min(state["n"], len(reviews) - 1)
            state["n"] += 1
            return 0, reviews[i], "", peer

        def verify(wt, cmd, timeout=600):
            return verify_rcs[min(state["n"], len(verify_rcs) - 1)], "tail"
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@t"}
            for cmd in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "base"]):
                subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True)
            o = argparse.Namespace(name="w", task="t", dir=str(repo), peers="claude,cursor", implementer=None,
                                   reviewer=None, base=None, files="src/x.py", verify="true", agreement=None,
                                   rounds=rounds, timeout=5, skill="none", detach=False)
            with mock.patch.object(w, "WORK", Path(d) / "work"), mock.patch.object(w, "ask_with_fallback", ask), \
                    mock.patch.object(w, "verify", verify), mock.patch.object(w, "close_threads", lambda n: None), \
                    mock.patch.object(w, "touched", lambda wt, b: ["src/x.py"]), \
                    mock.patch.object(ledger, "LEDGER", Path(d) / "ledger.jsonl"), \
                    mock.patch("sys.stderr"), mock.patch("sys.stdout"):
                w.run(o)
            rounds_json = (Path(d) / "work" / "w" / "rounds.json").read_text()
            report = (Path(d) / "work" / "w" / "report.md").read_text()
        import json
        return {"rounds": json.loads(rounds_json)}, calls, report

    def test_switch_once_and_earn_the_round(self):
        red = "| file:line | finding | severity | must-fix | why |\n|---|---|---|---|---|\n| src/x.py:1 | null check missing | high | yes | boom |"
        got, calls, report = self.run_loop(2, [1, 1, 1], [red, red, red])
        self.assertEqual(calls, ["write:claude", "ask:cursor", "write:claude", "ask:cursor", "write:cursor", "ask:claude"])
        self.assertEqual([r["repeat_findings"] for r in got["rounds"]], [[], ["src/x.py: null check missing"],
                                                                        ["src/x.py: null check missing"]])
        self.assertIn("switched implementer: round 3: claude→cursor", report)
        self.assertIn("rounds: 3/3", report)

    def test_revise_verdict_without_a_row_sends_the_implementer_back_from_the_loop(self):
        # the loop must consume review_fixes (verdict-aware), not must_fix: a bare REVISE earns a second round
        got, calls, report = self.run_loop(2, [0, 0], ["NONE\nBRIDGE_VERDICT=REVISE", "NONE\nBRIDGE_VERDICT=APPROVE"])
        self.assertEqual(calls, ["write:claude", "ask:cursor", "write:claude", "ask:cursor"])
        self.assertEqual([r["verdict"] for r in got["rounds"]], ["REVISE", "APPROVE"])
        self.assertIn("BRIDGE_VERDICT=REVISE", got["rounds"][0]["must_fix"][0])
        self.assertEqual(got["rounds"][1]["must_fix"], [])
        self.assertIn("review verdict: APPROVE", report)
        self.assertIn("READY FOR REVIEW", report)

    def test_no_switch_without_a_repeat_or_when_green(self):
        one = "| file:line | finding | severity | must-fix | why |\n|---|---|---|---|---|\n| src/x.py:1 | a | high | yes | x |"
        _got, calls, report = self.run_loop(2, [1, 0], [one, "NONE"])
        self.assertEqual(calls, ["write:claude", "ask:cursor", "write:claude", "ask:cursor"])
        self.assertIn("READY FOR REVIEW", report)
        self.assertNotIn("switched", report)


if __name__ == "__main__":
    unittest.main()


class EarlierAttempt(unittest.TestCase):
    """A second `bridge-work run <name>` finds the worktree of the first: what it carries is named to the implementer
    (files and diff, with the base to discard against), or discarded up front with --fresh."""

    ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}

    def git(self, cwd: Path, *a: str) -> str:
        return subprocess.run(["git", *a], cwd=cwd, env=self.ENV, check=True, capture_output=True, text=True).stdout

    def start(self, d: Path, fresh: bool, between=None):
        w = _load()
        repo = d / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "x.py").write_text("x = 1\n")
        (repo / ".gitignore").write_text(".claude/worktrees/\n")
        self.git(repo, "init", "-q")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-q", "-m", "base")
        base = self.git(repo, "rev-parse", "HEAD").strip()
        wt = repo / ".claude" / "worktrees" / "bridge-w"
        self.git(repo, "worktree", "add", "-q", "-b", "bridge/w", str(wt), base)
        (wt / "src" / "x.py").write_text("x = 2  # first attempt\n")                  # the earlier attempt
        (wt / "src" / "new.py").write_text("print('untracked from attempt one')\n")
        if between:                                                                     # what happened since run one
            between(repo, wt)
        prompts: list[str] = []

        def ask(peer, mode, cwd, prompt, timeout, conv, kind, peers=None):
            if mode == "write":
                prompts.append(prompt)
                return 0, "attempt", "", peer
            return 0, "NONE", "", peer
        o = argparse.Namespace(name="w", task="t", dir=str(repo), peers="claude,cursor", implementer=None,
                               reviewer=None, base=None, files="src/x.py,src/new.py", verify="true", agreement=None,
                               rounds=1, timeout=5, skill="none", detach=False, fresh=fresh)
        with mock.patch.object(w, "WORK", d / "work"), mock.patch.object(w, "ask_with_fallback", ask), \
                mock.patch.object(w, "verify", lambda wt, cmd, timeout=600: (0, "ok")), \
                mock.patch.object(w, "close_threads", lambda n: None), \
                mock.patch.object(ledger, "LEDGER", d / "ledger.jsonl"), \
                mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            rc = w.run(o)
        report = d / "work" / "w" / "report.md"
        return prompts[0] if prompts else "", report.read_text() if report.is_file() else "", wt, rc

    def test_the_implementer_is_told_what_the_worktree_carries(self):
        with tempfile.TemporaryDirectory() as d:
            prompt, report, wt, _ = self.start(Path(d), fresh=False)
            self.assertIn("earlier run left changes in this worktree", prompt)
            self.assertIn("src/x.py", prompt)
            self.assertIn("src/new.py", prompt)
            self.assertIn("git checkout", prompt)                                      # how to discard, with the base
            self.assertIn("earlier attempt: 2 file(s) carried", report)
            self.assertEqual((wt / "src" / "x.py").read_text(), "x = 2  # first attempt\n")   # nothing discarded

    def test_fresh_resets_the_worktree_to_base_first(self):
        with tempfile.TemporaryDirectory() as d:
            prompt, report, wt, _ = self.start(Path(d), fresh=True)
            self.assertNotIn("earlier run left changes", prompt)
            self.assertEqual((wt / "src" / "x.py").read_text(), "x = 1\n")
            self.assertFalse((wt / "src" / "new.py").exists())
            self.assertIn("earlier attempt: 2 file(s) discarded (--fresh)", report)

    def test_the_base_is_the_branch_point_even_after_main_moved_on(self):
        def main_moves(repo, wt):
            (repo / "src" / "y.py").write_text("y = 1\n")
            self.git(repo, "add", "src/y.py")
            self.git(repo, "commit", "-q", "-m", "main moved")
        with tempfile.TemporaryDirectory() as d:
            prompt, report, wt, _ = self.start(Path(d), fresh=False, between=main_moves)
            self.assertIn("2 file(s): src/new.py, src/x.py", prompt)                       # not main's y.py
            self.assertNotIn("y.py", prompt)

    def test_fresh_refuses_to_drop_commits_the_user_made_on_the_branch(self):
        def user_commits(repo, wt):
            self.git(wt, "add", "src/x.py")
            self.git(wt, "commit", "-q", "-m", "kept part of attempt one")
        with tempfile.TemporaryDirectory() as d:
            prompt, report, wt, rc = self.start(Path(d), fresh=True, between=user_commits)
            self.assertEqual(rc, 2)
            self.assertEqual(prompt, "")                                                     # no peer was spent
            self.assertEqual((wt / "src" / "x.py").read_text(), "x = 2  # first attempt\n")  # the commit stands
            self.assertTrue((wt / "src" / "new.py").exists())
