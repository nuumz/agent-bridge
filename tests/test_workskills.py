"""Unit tests for bridgelib.workskills.pick — trigger + path scoring."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

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

    def test_spec_edit_by_path(self):
        skill = workskills.pick(
            "update field mapping spec",
            files=["specs/api/field-mapping.md"],
            verify="python tools/spec-edit.py lint-delta specs/api/field-mapping.md",
        )
        self.assertIsNotNone(skill)
        self.assertEqual(skill["name"], "impl-spec-edit")

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
