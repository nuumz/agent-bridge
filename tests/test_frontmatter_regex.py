"""Quoted frontmatter values are JSON-style: `"\\bMR\\b"` in the file must reach the regex as \\bMR\\b."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from bridgelib import ledger, workskills  # noqa: E402


class QuotedTriggers(unittest.TestCase):
    def test_playbook_word_boundary_trigger_matches_a_bare_word(self):
        books = ledger.load_playbooks()
        self.assertIn("\\bMR\\b", books["review-change"]["triggers"])
        self.assertEqual(ledger.classify("the MR", books)[0][0], "review-change")

    def test_work_skill_escaped_dot_matches_a_file_name(self):
        skills = workskills.load_skills()
        self.assertIn("Test\\.java", skills["impl-test"]["triggers"])
        self.assertEqual(workskills.classify("touch OrderServiceTest.java", skills)[0][0], "impl-test")
        self.assertNotIn("impl-test", [n for n, _ in workskills.classify("touch OrderServiceTestXjava", skills)])


if __name__ == "__main__":
    unittest.main()
