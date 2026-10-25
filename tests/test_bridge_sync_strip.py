"""bridge-sync mirror stripping — the Claude-side commit marker never reaches a peer."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path
from unittest import mock


def _load():
    path = Path(__file__).resolve().parents[1] / "bin" / "bridge-sync"
    loader = importlib.machinery.SourceFileLoader("bridge_sync_strip_mod", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class StripClaudeOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bs = _load()

    def test_inline_commit_marker_line_is_replaced_by_peer_note(self):
        body = ("### Commit discipline\n"
                "Never commit unasked.\n"
                "A PreToolUse hook gates commit. Prefix: `CLAUDE_COMMIT_AUTHORIZED=1 git commit -m \"x\"`.\n"
                "### Format\n")
        out = self.bs.strip_claude_only(body)
        self.assertNotIn("CLAUDE_COMMIT_AUTHORIZED", out)
        self.assertIn("only when the user asked for it in the current turn", out)
        self.assertIn("Never commit unasked.", out)
        self.assertIn("### Format", out)

    def test_claude_skills_section_is_dropped(self):
        with mock.patch.object(self.bs, "SKIP_HEADINGS", {"## Skills (Claude Code)"}):
            out = self.bs.strip_claude_only("## Skills (Claude Code)\n- setup.sh links skills\n## CodeGraph\nkeep\n")
        self.assertNotIn("setup.sh links skills", out)
        self.assertIn("## CodeGraph", out)


if __name__ == "__main__":
    unittest.main()


class FailLoud(unittest.TestCase):
    """The Claude-only filters say what they did and never let the commit recipe through silently."""

    @classmethod
    def setUpClass(cls):
        cls.bs = _load()

    GIT_BULLET = ("## Git\n"
                  "- **Never `git commit` / `push` / `tag` without the user asking in the current turn.** "
                  "The gate asks for `CLAUDE_COMMIT_AUTHORIZED=1 git commit …` (one call) and for a recorded review. "
                  "A new MR is a Draft with no reviewer. A commit that lands without authorization: report the SHA "
                  "and ask.\n")

    def test_only_the_sentence_with_the_marker_is_replaced(self):
        out = self.bs.strip_claude_only(self.GIT_BULLET)
        self.assertNotIn("CLAUDE_COMMIT_AUTHORIZED", out)
        self.assertIn("- **Never `git commit` / `push` / `tag` without the user asking in the current turn.**", out)
        self.assertIn("A new MR is a Draft with no reviewer.", out)
        self.assertIn("report the SHA and ask.", out)
        self.assertNotIn("recorded review", out)                 # the marker's own sentence went with it
        self.assertEqual(out.count("## Git"), 1)

    def test_the_run_to_done_grant_sentence_is_replaced_like_the_marker(self):
        line = ('- **Never `git commit` without the user asking in the current turn.** "ok" is not a request: offer the '
                'chain, wait. "ทำให้จบ" in the turn → `commit-autonomy on --repo <the MR worktree> -- "<their words>"` '
                'once, then report what landed. A commit that lands without authorization: report the SHA and ask.\n')
        out = self.bs.strip_claude_only(line)
        self.assertNotIn("commit-autonomy", out)
        self.assertEqual(self.bs.leaks(out), [])                  # so the mirror is written, not refused
        self.assertIn(self.bs.COMMIT_TOKEN_NOTE, out)
        self.assertIn("report the SHA and ask", out)              # the sentences around it survive
        self.assertIn('"ok" is not a request', out)

    def test_the_note_agrees_with_the_bridge_note(self):
        out = self.bs.strip_claude_only(self.GIT_BULLET)
        self.assertNotIn("even when asked", out)                 # ruling 2026-10-23: a peer commits when the user asks
        self.assertIn("asked for it in the current turn", out)

    def test_old_fenced_block_is_still_replaced_whole(self):
        for fence in ("```", "```bash"):
            out = self.bs.strip_claude_only(f"Prefix:\n{fence}\ntouch .commit-authorized && git commit\n```\nnext\n")
            self.assertNotIn("commit-authorized", out)
            self.assertIn("next", out)

    def test_sentence_boundaries_a_marker_sentence_can_end_with(self):
        out = self.bs.strip_claude_only("Commit with the token (`CLAUDE_COMMIT_AUTHORIZED=1 git commit`.) Next rule.\n")
        self.assertIn("Next rule.", out)
        self.assertNotIn("CLAUDE_COMMIT_AUTHORIZED", out)
        twice = self.bs.strip_claude_only("- CLAUDE_COMMIT_AUTHORIZED=1 a. CLAUDE_COMMIT_AUTHORIZED=1 b. keep.\n")
        self.assertEqual(twice.count("Claude-side only"), 1)
        self.assertIn("keep.", twice)
        nested = self.bs.strip_claude_only("- outer\n      use `CLAUDE_COMMIT_AUTHORIZED=1 git commit`. keep.\n")
        self.assertIn("\n      *(The authorization marker", nested)                  # continuation indent kept

    def test_marker_on_the_last_line_without_newline_is_caught(self):
        out = self.bs.strip_claude_only("rules\nuse CLAUDE_COMMIT_AUTHORIZED=1 git commit")
        self.assertNotIn("CLAUDE_COMMIT_AUTHORIZED", out)

    def test_report_names_what_was_dropped_and_what_was_not_found(self):
        with mock.patch.object(self.bs, "SKIP_HEADINGS", {"## Skills (Claude Code)", "## Efficient Agent Usage"}):
            text, notes = self.bs.strip_report("## A\nkeep <!-- bridge:skip -->secret<!-- /bridge:skip --> on\n"
                                               "## Skills (Claude Code)\n- x CLAUDE_COMMIT_AUTHORIZED=1 y\n## B\n")
        self.assertNotIn("secret", text)
        self.assertEqual(notes, [("mirror: 1 bridge:skip region(s) and 1 section(s) dropped, 0 commit-marker line(s) "
                                  "replaced by the peer note"),
                                 ("mirror: section '## Efficient Agent Usage' not found (SKIP_HEADINGS entry is stale "
                                  "or the file moved on)")])         # configured but absent: said, not silent

    def test_leaks_are_named(self):
        self.assertEqual(self.bs.leaks("plain text"), [])
        self.assertEqual(self.bs.leaks("run `commit-autonomy on -- x` first"), ["commit-autonomy on"])
        self.assertEqual(self.bs.leaks("a <!-- bridge:skip --> b"), ["<!-- bridge:skip -->"])
        self.assertEqual(self.bs.leaks("touch ~/.claude/.commit-authorized"), [".commit-authorized"])
