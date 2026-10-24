"""`bridge-plan learn --team`: the scorecard per (role|kind, peer) from ledger rows, and what it suggests."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

from bridgelib import ledger  # noqa: E402


def _load():
    loader = importlib.machinery.SourceFileLoader("bridge_plan_mod", str(BIN / "bridge-plan"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def run(peer, rc=0, role=None, cites=(0, 0), cost=0.1, asked=False, kind="review-change"):
    return {"type": "run", "ts": int(time.time()), "peer": peer, "kind": kind, "rc": rc, "cost": cost,
            "panel": "p" if role else None, "conv": f"panel-p-{role}" if role else None,
            "citations_ok": cites[0], "citations_total": cites[1], "asked_back": asked}


class Team(unittest.TestCase):
    def setUp(self):
        self.plan = _load()

    def test_scorecard_counts_what_the_ledger_proves(self):
        rows = [run("claude", role="root-cause", cites=(4, 4)), run("claude", role="root-cause", cites=(3, 3)),
                run("claude", role="root-cause", cites=(2, 2)),
                run("cursor", role="root-cause", cites=(1, 4)), run("cursor", role="root-cause", rc=1, cites=(0, 2)),
                run("cursor", role="root-cause", rc=5, asked=True, cites=(0, 1)),
                {"type": "shape_miss", "ts": 1, "peer": "cursor#41", "topic": "room:r", "request_seq": 3, "shape": "table"},
                {"type": "shape_miss", "ts": 2, "peer": "cursor#42", "topic": "room:r", "request_seq": 3, "shape": "table"},
                {"type": "shape_miss", "ts": 3, "peer": "cursor#43", "topic": "room:r", "request_seq": 9, "shape": "json"},
                {"type": "blocked", "ts": 4, "peer": "copilot#1", "topic": "room:r", "request_seq": 5, "missing": "x"},
                {"type": "verdict", "ts": 5, "role": "root-cause", "peer": "cursor", "kept": 1, "rejected": 3}]
        sc = self.plan.scorecard(rows)
        claude, cursor = sc[("root-cause", "claude")], sc[("root-cause", "cursor")]
        self.assertEqual((claude["runs"], claude["ok"], claude["cites_ok"], claude["cites_total"]), (3, 3, 9, 9))
        self.assertEqual((cursor["runs"], cursor["ok"], cursor["asked_back"], cursor["cites_ok"], cursor["cites_total"]),
                         (3, 1, 1, 1, 7))
        self.assertEqual((cursor["kept"], cursor["verdicts"]), (1, 4))
        board = sc[("board", "cursor")]
        self.assertEqual((board["shape_miss"], board["declined"]), (3, 1))              # request 3 missed twice
        self.assertEqual(sc[("board", "copilot")]["blocked"], 1)
        tips = self.plan.team_suggestions(sc)
        self.assertTrue(any(t.startswith("root-cause on cursor: only 1/7 citations resolved — prefer claude") for t in tips),
                        tips)
        self.assertTrue(any("cursor: 3 replies missed the requested shape, 1 request(s) missed twice" in t for t in tips))
        self.assertFalse(any(t.startswith("root-cause on claude") for t in tips))
        out = io.StringIO()
        with redirect_stdout(out):
            self.plan.team(rows, 30)
        text = out.getvalue()
        self.assertRegex(text, r"root-cause\s+claude\s+3 100%\s+0\s+0\.10\s+-\s+9/9\s+0\s+0\s+0\s+-")
        self.assertRegex(text, r"board\s+cursor\s+0\s+-\s+0\s+-\s+-\s+-\s+3\s+0\s+1\s+-")

    def test_retried_identical_misses_are_one_and_the_better_peer_is_the_best_one(self):
        rows = [{"type": "shape_miss", "ts": t, "peer": "cursor#1", "topic": "r", "request_seq": 3, "body_hash": "same"}
                for t in (1, 2, 3)]
        self.assertEqual(self.plan.scorecard(rows)[("board", "cursor")]["declined"], 0)
        rows.append({"type": "shape_miss", "ts": 4, "peer": "cursor#2", "topic": "r", "request_seq": 3, "body_hash": "new"})
        self.assertEqual(self.plan.scorecard(rows)[("board", "cursor")]["declined"], 1)
        runs = ([run("a", rc=1)] * 3 + [run("b")] * 2 + [run("b", rc=1)] + [run("c")] * 3)   # c is best, not first
        for r in runs:
            r["citations_ok"], r["citations_total"] = (0, 2) if r["peer"] == "a" else (0, 0)
        runs += [run("d", cites=(3, 3))] * 3
        tips = self.plan.team_suggestions(self.plan.scorecard(runs))
        self.assertIn("review-change on a: only 0/6 citations resolved — prefer d", tips)    # by citation rate
        mark = {"type": "verdict", "ts": 9, "kind": "review-change", "peer": "a", "kept": 2, "rejected": 1}
        self.assertEqual(self.plan.scorecard(runs + [mark])[("review-change", "a")]["kept"], 2)  # on the kind row
        with mock.patch.object(self.plan, "role_fallbacks", return_value={}):
            tips = self.plan.team_suggestions(self.plan.scorecard(runs))
        self.assertIn("review-change on a: only 0/6 citations resolved — prefer d", tips)

    def test_the_plan_hint_names_a_better_peer_only_with_enough_runs(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ledger, "LEDGER", Path(d) / "l.jsonl"):
            for r in [run("cursor", rc=1), run("cursor", rc=1), run("cursor"), run("claude"), run("claude")]:
                ledger.record(r)
            self.assertEqual(self.plan.scorecard_hint("review-change", "cursor"), "")   # claude has only 2 runs
            ledger.record(run("claude"))
            hint = self.plan.scorecard_hint("review-change", "cursor")
            self.assertIn("claude ended rc=0 in 3/3 runs vs cursor 1/3 — consider -p claude", hint)
            self.assertEqual(self.plan.scorecard_hint("review-change", "claude"), "")
            self.assertEqual(self.plan.scorecard_hint("implement", "cursor"), "")

    def test_learn_team_runs_on_an_empty_ledger(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ledger, "LEDGER", Path(d) / "l.jsonl"):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = self.plan.team([], 30)
        self.assertEqual(rc, 0)
        self.assertIn("no run, board or verdict rows", out.getvalue())


if __name__ == "__main__":
    unittest.main()
