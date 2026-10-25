"""parse_card_gates + guarded_rc."""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
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
        self.assertFalse(self.bw.review_valid(0, "| I could not inspect the diff"))

    def test_self_review_is_disclosed(self):
        self.assertIn("not independent", self.bw.review_label("claude", "claude"))
        self.assertNotIn("not independent", self.bw.review_label("claude", "copilot"))


class GuardedRc(unittest.TestCase):
    SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

    def setUp(self):
        self._prev = {s: signal.getsignal(s) for s in self.SIGNALS}

    def tearDown(self):
        for s, h in self._prev.items():
            signal.signal(s, h)

    def test_sigterm_writes_143(self):
        from bridgelib import core
        from bridgelib.core import guarded_rc
        got = {}

        def write(n, rc):
            got["rc"] = rc
            return rc

        def body():
            os.kill(os.getpid(), signal.SIGTERM)
            return 0

        with mock.patch.object(core.threading, "Timer") as timer:        # the 10 s SIGKILL never fires for real here
            with self.assertRaises(SystemExit) as cm:
                guarded_rc("unit-sigterm", write, body)
        self.assertEqual(cm.exception.code, 143)
        self.assertEqual(got.get("rc"), 143)
        for s in self.SIGNALS:                                            # the handlers are the caller's again
            self.assertEqual(signal.getsignal(s), self._prev[s], s)
        self.assertIsNone(core._STOP)
        timer.assert_called_once()

    def test_the_kill_timer_targets_the_children_of_that_signal_only(self):
        """The handler SIGTERMs the run_tool children alive at the signal and arms a SIGKILL for *those* 10 s later.
        It must not read _CHILDREN when it fires: that killed whatever another test had started meanwhile (the
        intermittent -9 in test_work_signals), and in a real run the next pool task started after the kill."""
        from bridgelib import core
        stubborn = subprocess.Popen(["sh", "-c", 'trap "" TERM; sleep 30'], process_group=0)
        self.addCleanup(lambda: (os.killpg(stubborn.pid, signal.SIGKILL), stubborn.wait()) if stubborn.poll() is None else None)
        time.sleep(0.2)                                                   # the trap is installed
        with core._CHILDREN_LOCK:
            core._CHILDREN.add(stubborn)
        self.addCleanup(core._CHILDREN.discard, stubborn)
        real_timer, delays = threading.Timer, []

        def quick(delay, fn, args=(), kwargs=None):                       # the real timer, 1 s instead of 10
            delays.append(delay)
            return real_timer(1.0, fn, args, kwargs)

        with mock.patch.object(core.threading, "Timer", quick), self.assertRaises(SystemExit):
            core.guarded_rc("unit-timer", lambda n, rc: rc, lambda: os.kill(os.getpid(), signal.SIGTERM))
        later = subprocess.Popen(["sleep", "30"], process_group=0)      # started after the kill, by someone else
        self.addCleanup(lambda: (os.killpg(later.pid, signal.SIGKILL), later.wait()) if later.poll() is None else None)
        with core._CHILDREN_LOCK:
            core._CHILDREN.add(later)
        self.addCleanup(core._CHILDREN.discard, later)
        self.assertEqual(delays, [10])
        self.assertIsNone(stubborn.poll(), "ignores SIGTERM: still there, waiting for the SIGKILL")
        try:
            stubborn.wait(timeout=5)                                      # the timer fires: the snapshot child goes
        except subprocess.TimeoutExpired:
            self.fail("the kill timer did not SIGKILL the child that was alive at the signal")
        time.sleep(0.3)
        self.assertIsNone(later.poll(), "a child that did not exist at the signal is not the timer's business")

    def test_run_tool_spawns_nothing_after_a_handled_signal(self):
        """A pool worker that reaches run_tool while the main thread unwinds from the kill must not start a peer
        (bridge-panel's `ex.map` cancels its queue, so this is the belt for any other pool shape)."""
        from bridgelib import core
        spawned = []

        def body():
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except SystemExit:                                            # a worker, meanwhile, asks for a run
                with mock.patch.object(core.subprocess, "Popen", side_effect=lambda *a, **k: spawned.append(a)):
                    core.run_tool(["sleep", "30"], timeout=1)
            return 0

        with mock.patch.object(core.threading, "Timer"), self.assertRaises(SystemExit) as cm:
            core.guarded_rc("unit-stop", lambda n, rc: rc, body)
        self.assertEqual(cm.exception.code, 143)
        self.assertEqual(spawned, [], "nothing starts after the kill")
        self.assertIsNone(core._STOP, "cleared once the body has unwound")

    def test_a_signal_while_run_tool_holds_the_children_lock_still_exits_143(self):
        """The handler runs on the main thread; if it took _CHILDREN_LOCK while the interrupted frame holds it, the
        process would hang forever with no rc file — so it must not take the lock."""
        script = textwrap.dedent("""\
            import os, signal, sys
            sys.path.insert(0, sys.argv[1])
            from bridgelib import core
            def body():
                with core._CHILDREN_LOCK:
                    os.kill(os.getpid(), signal.SIGTERM)
                return 0
            core.guarded_rc("lock", lambda n, rc: rc, body)
            """)
        try:
            p = subprocess.run([sys.executable, "-c", script, str(Path(__file__).resolve().parents[1] / "bin")],
                               capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("guarded_rc deadlocked on _CHILDREN_LOCK")
        self.assertEqual(p.returncode, 143, p.stderr)

    def test_a_signal_right_after_popen_still_ends_the_child(self):
        """Between Popen and the child's registration a signal (SystemExit from the handler) used to leak the child:
        it was outside the try that ends the group."""
        from bridgelib import core
        seen: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def spy(*a, **k):
            proc = real_popen(*a, **k)
            seen.append(proc)
            return proc

        class LockThatIsInterrupted:
            """The first `with _CHILDREN_LOCK:` (the add) is where the signal lands."""
            armed = True

            def __enter__(self):
                if self.armed:
                    self.armed = False
                    raise SystemExit(143)

            def __exit__(self, *exc):
                return False

        self.addCleanup(lambda: [(os.killpg(p.pid, signal.SIGKILL), p.wait()) for p in seen if p.poll() is None])
        with mock.patch.object(core.subprocess, "Popen", spy), \
                mock.patch.object(core, "_CHILDREN_LOCK", LockThatIsInterrupted()), self.assertRaises(SystemExit):
            core.run_tool(["sleep", "30"])
        self.assertEqual(len(seen), 1)
        try:
            seen[0].wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("the child spawned right before the signal was leaked")

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



class ReviewVerdict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bw = _load_bridge_work()

    TABLE = "| file:line | finding | severity | must-fix | why |\n|---|---|---|---|---|\n| a.py:1 | x | low | no | y |"

    def test_verdict_line_is_read_and_does_not_break_the_shape_check(self):
        self.assertEqual(self.bw.review_verdict(self.TABLE + "\nBRIDGE_VERDICT=APPROVE\n"), "APPROVE")
        self.assertEqual(self.bw.review_verdict("NONE\n  BRIDGE_VERDICT = REVISE "), "REVISE")
        self.assertIsNone(self.bw.review_verdict(self.TABLE))
        self.assertTrue(self.bw.review_valid(0, "NONE\nBRIDGE_VERDICT=APPROVE\n"))
        self.assertTrue(self.bw.review_valid(0, "NONE\n`BRIDGE_VERDICT=APPROVE`\n"))   # dressed up — still not a shape failure
        self.assertEqual(self.bw.review_verdict("NONE\n**bridge_verdict: revise**"), "REVISE")
        self.assertFalse(self.bw.review_valid(0, "BRIDGE_VERDICT=APPROVE\n"))          # a verdict alone is not a review
        # Cursor narrates its board check before answering; with a verdict line that is still a review in shape
        chatty = "Checking the board, then the diff.\n\nNo open request — posting done.\n\nNONE\nBRIDGE_VERDICT=APPROVE"
        self.assertTrue(self.bw.review_valid(0, chatty))
        self.assertFalse(self.bw.review_valid(0, "Checking the diff.\n\nNONE"))             # no verdict: free text, no review
        self.assertFalse(self.bw.review_valid(0, "Looks fine to me.\nBRIDGE_VERDICT=APPROVE"))   # verdict without NONE/table

    def test_revise_without_a_must_fix_row_still_sends_the_implementer_back(self):
        fixes = self.bw.review_fixes(self.TABLE + "\nBRIDGE_VERDICT=REVISE", [], ["a.py"])
        self.assertEqual(len(fixes), 1)
        self.assertIn("BRIDGE_VERDICT=REVISE", fixes[0])
        self.assertIn("a.py:1 / x / low / no / y", fixes[0])                 # the implementer gets what the reviewer wrote
        # a must-fix row the agreement filter dropped (file outside the allowed set, untouched) is not this case
        outside = self.TABLE.replace("| a.py:1 | x | low | no |", "| other/b.py:3 | x | high | yes |")
        self.assertEqual(self.bw.review_fixes(outside + "\nBRIDGE_VERDICT=REVISE", ["src/**"], ["src/a.py"]), [])
        self.assertEqual(self.bw.review_fixes(self.TABLE + "\nBRIDGE_VERDICT=APPROVE", [], ["a.py"]), [])
        yes = self.TABLE.replace("| low | no |", "| high | yes |")
        self.assertEqual(len(self.bw.review_fixes(yes + "\nBRIDGE_VERDICT=APPROVE", [], ["a.py"])), 1)  # rows win

    def test_review_prompt_asks_for_the_verdict_line(self):
        o = mock.Mock(name="o", task="t", verify="make test")
        o.name = "n"
        self.assertIn("BRIDGE_VERDICT=REVISE", self.bw.review_prompt(o, ["a.py"], "diff", 0, "", []))


if __name__ == "__main__":
    unittest.main()
