"""bridge-sync skill links — a repo-relative link to the same skill survives a sync."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "bridge-sync"
    loader = importlib.machinery.SourceFileLoader("bridge_sync_mod", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class SkillLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bs = _load()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # repo/.claude/skills/x is the real skill; ~/.claude/skills/x links to it (as setup.sh arranges);
        # repo/.copilot/skills is the peer skill dir bridge-sync fills.
        self.repo_skill = root / "repo" / ".claude" / "skills" / "x"
        self.repo_skill.mkdir(parents=True)
        self.src = root / "home" / ".claude" / "skills" / "x"
        self.src.parent.mkdir(parents=True)
        self.src.symlink_to(self.repo_skill, target_is_directory=True)
        self.dest_dir = root / "repo" / ".copilot" / "skills"
        self.dest_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _sync(self):
        self.bs.sync_skills_into(self.dest_dir, "copilot-test", [self.src], set())

    def test_relative_link_to_same_skill_is_kept(self):
        (self.dest_dir / "x").symlink_to("../../.claude/skills/x", target_is_directory=True)
        self._sync()
        self.assertEqual(os.readlink(self.dest_dir / "x"), "../../.claude/skills/x")

    def test_link_to_another_dir_is_replaced(self):
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        (self.dest_dir / "x").symlink_to(other, target_is_directory=True)
        self._sync()
        self.assertEqual(Path(os.readlink(self.dest_dir / "x")), self.src)

    def test_missing_link_is_created(self):
        self._sync()
        self.assertEqual(Path(os.readlink(self.dest_dir / "x")), self.src)


if __name__ == "__main__":
    unittest.main()
