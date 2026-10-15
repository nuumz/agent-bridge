"""bridge-sync mirror stripping — the Claude-side commit marker never reaches a peer."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path


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
        self.assertIn("never run a mutating git command", out)
        self.assertIn("Never commit unasked.", out)
        self.assertIn("### Format", out)

    def test_claude_skills_section_is_dropped(self):
        out = self.bs.strip_claude_only("## Skills (Claude Code)\n- setup.sh links skills\n## CodeGraph\nkeep\n")
        self.assertNotIn("setup.sh links skills", out)
        self.assertIn("## CodeGraph", out)


if __name__ == "__main__":
    unittest.main()
