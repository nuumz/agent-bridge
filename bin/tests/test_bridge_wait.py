"""bridge-wait — done / running / dead exit codes and the safe --max clamp."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load():
    path = Path(__file__).resolve().parents[1] / "bridge-wait"
    loader = importlib.machinery.SourceFileLoader("bridge_wait_mod", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class BridgeWait(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bw = _load()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # point every kind at the temp root; keep rc names and process patterns
        self.kinds = {k: (root, v[1], v[2], v[3]) for k, v in self.bw.KINDS.items()}
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv, alive):
        with mock.patch.object(self.bw, "KINDS", self.kinds), \
             mock.patch.object(self.bw, "alive", lambda kind, name: alive), \
             mock.patch.object(sys, "argv", ["bridge-wait", *argv]):
            return self.bw.main()

    def test_done_reports_rc_and_report(self):
        d = self.root / "p1"; d.mkdir()
        (d / "rc").write_text("0\n"); (d / "report.md").write_text("# r\n")
        with mock.patch("builtins.print") as pr:
            self.assertEqual(self._run(["panel", "p1", "--max", "1"], alive=True), 0)
        self.assertIn("done rc=0 report=", pr.call_args[0][0])

    def test_fix_rc_file_and_report_location(self):
        d = self.root / "f1"; d.mkdir()
        (d / "rc").write_text("0\n")                       # bridge-work's file must NOT satisfy a fix wait
        self.assertEqual(self._run(["fix", "f1", "--max", "1"], alive=True), 75)
        (d / "fix.rc").write_text("3\n")
        (self.root / "fix-f1").mkdir(); (self.root / "fix-f1" / "report.md").write_text("x")
        with mock.patch("builtins.print") as pr:
            self.assertEqual(self._run(["fix", "f1", "--max", "1"], alive=True), 0)
        self.assertIn("done rc=3 report=" + str(self.root / "fix-f1" / "report.md"), pr.call_args[0][0])

    def test_running_is_75_with_stage(self):
        d = self.root / "p2"; d.mkdir()
        (d / "plan.json").write_text("{}"); (d / "findings.json").write_text("{}")
        with mock.patch("builtins.print") as pr:
            self.assertEqual(self._run(["panel", "p2", "--max", "1", "--interval", "0.2"], alive=True), 75)
        self.assertIn("stage=findings.json", pr.call_args[0][0])

    def test_dead_is_1(self):
        (self.root / "p3").mkdir()
        self.assertEqual(self._run(["panel", "p3", "--max", "1", "--interval", "0.2"], alive=False), 1)

    def test_max_clamped_without_unsafe(self):
        d = self.root / "p4"; d.mkdir(); (d / "rc").write_text("0\n")
        with mock.patch("builtins.print") as pr:
            self._run(["panel", "p4", "--max", "500"], alive=True)
        msgs = " ".join(str(c.args[0]) for c in pr.call_args_list)
        self.assertIn("clamped to 110", msgs)

    def test_alive_pattern_matches_the_run_only(self):
        pat = self.bw.KINDS["panel"][2].format(name="abc")
        import re
        self.assertRegex("python3 /x/bridge-panel run abc -p claude", pat)
        self.assertNotRegex("python3 /x/bridge-panel run abcd -p claude", pat)
        self.assertNotRegex("python3 /x/bridge-panel show abc", pat)


if __name__ == "__main__":
    unittest.main()
