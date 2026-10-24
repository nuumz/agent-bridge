"""bridge-fix after a failed fix: a second `decide` replaces the earlier ruling in the agreement (the text the
implementer gets), and `run` on a resumed flow takes new --files / --verify instead of dropping them."""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
from bridgelib import ledger  # noqa: E402

REPORT = """# panel

## Root cause
- 1. `avg([])` → **ZeroDivisionError** at a.py:3

## Decisions for the user
| question | options | recommendation | if undecided |
|---|---|---|---|
| guard or raise? | A guard · B raise | A → **guard** | the fix waits |
| keep the old name? | A yes · B rename | A | the fix waits |

## Next actions
- edit a.py
"""


def load_fix():
    loader = importlib.machinery.SourceFileLoader("bridge_fix_rerule", str(BIN / "bridge-fix"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


class Reruling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fix = load_fix()
        self.work = root / "work"
        (root / "report.md").write_text(REPORT)
        self.patches = [mock.patch.object(self.fix, "WORK", self.work),
                        mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"),      # fix() records a row
                        mock.patch.object(self.fix, "board", lambda *a: "posted seq 1"),
                        mock.patch("sys.stderr", new_callable=io.StringIO)]
        for p in self.patches:
            p.start()
        fl = {"name": "t", "stage": "decide", "problem": "avg of [] crashes", "repo": str(root), "peers": "claude",
              "roles": "x", "files": "", "verify": "", "add_dirs": [], "report": str(root / "report.md"),
              "panel": "fix-t", "card_seq": 1}
        self.fix.save(fl)
        self.fix.write_agreement(fl)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def decide(self, **kw) -> int:
        base = dict(name="t", answer=None, go=False, files=None, verify=None, note=None, no_fix=True, rounds=2,
                    timeout=600, detach=False)
        return self.fix.decide(argparse.Namespace(**{**base, **kw}))

    def agreement(self) -> str:
        return (self.work / "t" / "agreement.md").read_text()

    def test_a_second_ruling_replaces_the_first_in_the_agreement(self):
        self.assertEqual(self.decide(answer=["1=A guard", "2=B rename"], files="a.py", verify="pytest -q"), 0)
        first = self.agreement()
        self.assertIn("→ **A guard**", first)
        self.assertIn("→ **B rename**", first)
        self.assertEqual(self.fix.load("t")["stage"], "fix")
        # the fix failed; the human rules differently and re-runs
        self.assertEqual(self.decide(answer=["1=B raise", "2=A yes"], note="second thoughts"), 0)
        second = self.agreement()
        self.assertIn("→ **B raise**", second)
        self.assertIn("→ **A yes**", second)
        self.assertNotIn("**A guard**", second)
        self.assertNotIn("**B rename**", second)
        self.assertIn("- note: second thoughts", second)
        self.assertEqual(self.fix.load("t")["ruling"], {"1": "B raise", "2": "A yes"})

    def test_go_after_an_explicit_ruling_takes_the_recommendations_again(self):
        self.decide(answer=["1=B raise", "2=B rename"], files="a.py", verify="pytest -q")
        self.assertEqual(self.decide(go=True), 0)
        text = self.agreement()
        self.assertEqual(text.count("→ **GO — the panel's recommendation as written**"), 2)
        self.assertNotIn("→ **B raise**", text)

    def test_the_implementer_gets_the_latest_ruling_and_gates(self):
        self.decide(answer=["1=A guard", "2=A yes"], files="a.py", verify="pytest -q")
        self.decide(answer=["1=B raise", "2=B rename"], files="a.py,b.py", verify="pytest -q tests/test_a.py")
        seen = {}

        def fake_stream(argv, prefixes):
            seen["argv"] = argv
            return 1

        with mock.patch.object(self.fix, "stream_filtered", fake_stream):
            self.fix.fix(self.fix.load("t"), argparse.Namespace(rounds=2, timeout=600))
        task = seen["argv"][-1]
        self.assertIn("→ **B raise**", task)
        self.assertNotIn("**A guard**", task)
        self.assertEqual(seen["argv"][seen["argv"].index("--files") + 1], "a.py,b.py")
        self.assertEqual(seen["argv"][seen["argv"].index("--verify") + 1], "pytest -q tests/test_a.py")

    def test_run_on_a_resumed_flow_takes_new_files_and_verify(self):
        self.decide(answer=["1=A guard", "2=A yes"], files="a.py", verify="pytest -q")
        seen = {}

        def fake_stream(argv, prefixes):
            seen["argv"] = argv
            return 1

        o = argparse.Namespace(name="t", problem=None, dir=str(self.tmp.name), peers="claude", roles=None,
                               files="a.py,c.py", verify="make check", add_dir=[], rounds=2, timeout=420,
                               detach=False)
        with mock.patch.object(self.fix, "stream_filtered", fake_stream):
            self.fix.run(o)
        self.assertEqual(seen["argv"][seen["argv"].index("--files") + 1], "a.py,c.py")
        self.assertEqual(seen["argv"][seen["argv"].index("--verify") + 1], "make check")
        text = self.agreement()
        self.assertIn("- files the fix may touch: a.py,c.py", text)
        self.assertIn("- verify command (must fail now, pass after): make check", text)
        fl = self.fix.load("t")
        self.assertEqual((fl["files"], fl["verify"]), ("a.py,c.py", "make check"))

    def test_only_the_ruling_section_is_rewritten(self):
        self.assertEqual(self.decide(answer=["1=B raise", "2=A yes"], files="a.py", verify="pytest -q"), 0)
        text = self.agreement()
        self.assertIn("- 1. `avg([])` → **ZeroDivisionError** at a.py:3", text)        # the panel's line, untouched
        self.assertIn("→ **B raise** (options: A guard · B raise; panel recommends: A → **guard**)", text)

    def test_a_refused_re_rule_changes_nothing(self):
        self.decide(answer=["1=A guard", "2=A yes"], files="a.py", verify="pytest -q")
        before = self.agreement(), self.fix.load("t")
        self.assertEqual(self.decide(answer=["1=B raise", "2=B rename"], files="a.py,b.py", note="TODO: later"), 2)
        self.assertEqual((self.agreement(), self.fix.load("t")), before)
        self.assertEqual(self.decide(answer=["1=B raise", "2=B rename", "7=zzz"]), 2)
        self.assertEqual((self.agreement(), self.fix.load("t")), before)

    def test_a_bold_answer_does_not_nest(self):
        self.decide(answer=["1=B **raise**", "2=A yes"], files="a.py", verify="pytest -q")
        self.decide(answer=["1=A guard", "2=A yes"])
        self.assertIn("→ **A guard** (options:", self.agreement())
        self.assertNotIn("****", self.agreement())

    def test_run_refuses_a_todo_gate_and_leaves_a_done_flow_alone(self):
        self.decide(answer=["1=A guard", "2=A yes"], files="a.py", verify="pytest -q")
        o = argparse.Namespace(name="t", problem=None, dir=str(self.tmp.name), peers="claude", roles=None,
                               files=None, verify="TODO: pick a test", add_dir=[], rounds=2, timeout=420, detach=False)
        self.assertEqual(self.fix.run(o), 2)
        self.assertEqual(self.fix.load("t")["verify"], "pytest -q")
        fl = self.fix.load("t")
        fl["stage"] = "done"
        self.fix.save(fl)
        o.verify, o.files = "make check", "zzz.py"
        self.assertEqual(self.fix.run(o), 0)
        self.assertEqual((self.fix.load("t")["files"], self.fix.load("t")["verify"]), ("a.py", "pytest -q"))
        self.assertIn("- files the fix may touch: a.py\n", self.agreement())

    def test_the_card_is_closed_on_the_re_rule_when_the_first_close_was_refused(self):
        calls: list[tuple[str, ...]] = []
        refuse_decision = {"on": True}

        def board(*a):
            calls.append(a)
            if a[0] == "post" and "decision" in a and refuse_decision["on"]:
                return ""
            return "posted seq 2"
        with mock.patch.object(self.fix, "board", board):
            self.decide(answer=["1=A guard", "2=A yes"], files="a.py", verify="pytest -q")
            self.assertFalse(self.fix.load("t").get("card_closed"))
            refuse_decision["on"] = False
            self.decide(answer=["1=B raise", "2=A yes"])
        self.assertTrue(self.fix.load("t")["card_closed"])
        self.assertEqual([a[0] for a in calls].count("close"), 1)

    def test_a_second_fix_is_told_about_the_earlier_attempt(self):
        self.decide(answer=["1=A guard", "2=A yes"], files="a.py", verify="pytest -q")
        tasks = []
        with mock.patch.object(self.fix, "stream_filtered", lambda argv, prefixes: tasks.append(argv[-1]) or 1):
            self.fix.fix(self.fix.load("t"), argparse.Namespace(rounds=2, timeout=600))
            self.assertEqual(self.fix.load("t")["work_rc"], 1)
            self.decide(answer=["1=B raise", "2=A yes"])
            self.assertNotIn("work_rc", self.fix.load("t"))          # the old outcome went with the old ruling
            self.fix.fix(self.fix.load("t"), argparse.Namespace(rounds=2, timeout=600))
        self.assertNotIn("earlier attempt", tasks[0])
        self.assertIn("An earlier attempt under a previous ruling is still in this worktree", tasks[1])
        self.assertEqual(self.fix.load("t")["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
