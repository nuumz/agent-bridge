"""Unit tests for bridgelib.workskills.pick — trigger + path scoring."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from bridgelib import workskills  # noqa: E402


class Pick(unittest.TestCase):
    def test_mapper_by_path_beats_test_word(self):
        skill = workskills.pick(
            "extend UserMapper",
            files=["src/main/java/foo/UserMapper.java"],
            verify="mvn -o -q test -Dtest=UserMapperTest",
        )
        self.assertIsNotNone(skill)
        self.assertEqual(skill["name"], "impl-mapper")

    def test_test_skill_by_path(self):
        skill = workskills.pick(
            "add coverage for the mapper",
            files=["src/test/java/foo/UserMapperTest.java"],
            verify="mvn -o -q test -Dtest=UserMapperTest",
        )
        self.assertIsNotNone(skill)
        self.assertEqual(skill["name"], "impl-test")

    def test_local_skill_is_loaded_and_wins_by_path(self):
        # a machine-local skill (work-skills.local/) joins the public set and is picked on path evidence alone
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "impl-docs.md").write_text(
                "---\nname: impl-docs\nsummary: edit docs\ntriggers: \"docs-lint\"\npaths: \"docs/\"\n---\n"
                "### implementer\n- keep headings.\n### reviewer\n- must-fix=yes if a link breaks.\n")
            with mock.patch.object(workskills, "LOCAL_SKILLS", Path(d)):
                skill = workskills.pick("update the install guide", files=["docs/install.md"], verify="make docs-lint")
        self.assertIsNotNone(skill)
        self.assertEqual(skill["name"], "impl-docs")

    def test_small_fix_by_trigger(self):
        skill = workskills.pick(
            "hotfix null pointer in payment",
            files=["src/main/java/foo/Pay.java"],
            verify="mvn -o -q test -Dtest=PayTest",
        )
        self.assertIsNotNone(skill)
        self.assertEqual(skill["name"], "impl-small-fix")

    def test_forced_none(self):
        self.assertIsNone(workskills.pick("anything", files=["a.java"], forced="none"))

    def test_forced_unknown_raises(self):
        with self.assertRaises(KeyError):
            workskills.pick("x", forced="no-such-skill")


if __name__ == "__main__":
    unittest.main()
