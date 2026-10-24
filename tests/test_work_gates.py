"""parse_card_gates + guarded_rc."""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import signal
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _load_bridge_work():
    path = Path(__file__).resolve().parents[1] / "bin" / "bridge-work"
    loader = importlib.machinery.SourceFileLoader("bridge_work_mod", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class ParseCardGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bw = _load_bridge_work()

    def test_basic(self):
        files, verify = self.bw.parse_card_gates(
            "- touch only: src/A.java, src/B.java\n"
            "- verify command (done when it passes): mvn -q test\n"
        )
        self.assertEqual(files, ["src/A.java", "src/B.java"])
        self.assertEqual(verify, "mvn -q test")

    def test_strips_backticks_and_skips_todo(self):
        files, verify = self.bw.parse_card_gates(
            "- touch only: `src/A.java`\n"
            "- verify command (done when it passes): TODO: fill\n"
        )
        self.assertEqual(files, ["src/A.java"])
        self.assertEqual(verify, "")

    def test_empty(self):
        self.assertEqual(self.bw.parse_card_gates("no gates here\n"), ([], ""))

    def test_todo_globs_rejected(self):
        files, verify = self.bw.parse_card_gates(
            "- touch only: TODO: globs\n- verify command (done when it passes): true\n"
        )
        self.assertEqual(files, [])
        self.assertEqual(verify, "true")

    def test_fix_flow_verify_label(self):
        # bridge-fix agreement wording
        files, verify = self.bw.parse_card_gates(
            "- touch only: src/X.java\n"
            "- verify command (must fail now, pass after): pytest -q\n"
        )
        self.assertEqual(files, ["src/X.java"])
        self.assertEqual(verify, "pytest -q")


class FallbackAndReviewGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bw = _load_bridge_work()

    def test_fallback_only_to_a_peer_the_user_allowed(self):
        calls = []

        def fake_ask(peer, *a):
            calls.append(peer)
            return (7, "quota", "") if peer == "copilot" else (0, "done", "")

        with mock.patch.object(self.bw, "ask", fake_ask), contextlib.redirect_stderr(io.StringIO()):
            rc, _, _, used = self.bw.ask_with_fallback("copilot", "write", "/r", "p", 5, "c", "implement", ["copilot"])
            self.assertEqual((rc, used, calls), (7, "copilot", ["copilot"]))
            calls.clear()
            rc, _, _, used = self.bw.ask_with_fallback("copilot", "write", "/r", "p", 5, "c", "implement",
                                                       ["copilot", "claude"])
            self.assertEqual((rc, used, calls), (0, "claude", ["copilot", "claude"]))

    def test_review_counts_only_when_it_finished_in_the_requested_shape(self):
        table = "| file:line | finding | severity | must-fix | why |\n|---|---|---|---|---|\n| a.py:1 | x | low | no | y |"
        self.assertTrue(self.bw.review_valid(0, table))
        self.assertTrue(self.bw.review_valid(0, " NONE \n"))
        self.assertFalse(self.bw.review_valid(5, "[[ASK]] which branch?"))
        self.assertFalse(self.bw.review_valid(0, "Looks good to me overall."))
        self.assertFalse(self.bw.review_valid(1, table))

    def test_self_review_is_disclosed(self):
        self.assertIn("not independent", self.bw.review_label("claude", "claude"))
        self.assertNotIn("not independent", self.bw.review_label("claude", "copilot"))


class GuardedRc(unittest.TestCase):
    def setUp(self):
        self._prev = signal.getsignal(signal.SIGTERM)

    def tearDown(self):
        signal.signal(signal.SIGTERM, self._prev)

    def test_sigterm_writes_143(self):
        from bridgelib.core import guarded_rc
        got = {}

        def write(n, rc):
            got["rc"] = rc
            return rc

        def body():
            os.kill(os.getpid(), signal.SIGTERM)
            return 0

        with self.assertRaises(SystemExit) as cm:
            guarded_rc("unit-sigterm", write, body)
        self.assertEqual(cm.exception.code, 143)
        self.assertEqual(got.get("rc"), 143)

    def test_normal_rc(self):
        from bridgelib.core import guarded_rc
        got = {}

        def write(n, rc):
            got["rc"] = rc
            return rc

        self.assertEqual(guarded_rc("unit-ok", write, lambda: 0), 0)
        self.assertEqual(got["rc"], 0)

    def test_exception_writes_1(self):
        from bridgelib.core import guarded_rc
        got = {}

        def write(n, rc):
            got["rc"] = rc
            return rc

        def body():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            guarded_rc("unit-exc", write, body)
        self.assertEqual(got.get("rc"), 1)


if __name__ == "__main__":
    unittest.main()
