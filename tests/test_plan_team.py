"""`bridge-plan learn --team`: the scorecard per (role|kind, peer) from ledger rows, and what it suggests."""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
FAKEBIN = BIN.parent / "tests" / "fakebin"
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(FAKEBIN))

import _fakebin  # noqa: E402
from bridgelib import evidence, ledger  # noqa: E402


def _load():
    loader = importlib.machinery.SourceFileLoader("bridge_plan_mod", str(BIN / "bridge-plan"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def run(peer, rc=0, role=None, cites=(0, 0), cost=0.1, asked=False, kind="review-change", ask=None,
        rev=evidence.RESOLVER_REV):
    return {"type": "run", "ts": int(time.time()), "peer": peer, "kind": kind, "rc": rc, "cost": cost,
            "panel": "p" if role else None, "conv": f"panel-p-{role}" if role else None,
            "citations_ok": cites[0], "citations_total": cites[1], "citations_rev": rev,
            "asked_back": asked or bool(ask), "ask": ask}


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
        self.assertRegex(text, r"root-cause\s+claude\s+3 100%\s+0\s+0\s+0\.10\s+-\s+9/9\s+0\s+0\s+0\s+-")
        self.assertRegex(text, r"board\s+cursor\s+0\s+-\s+0\s+0\s+-\s+-\s+-\s+3\s+0\s+1\s+-")

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

    def test_scorecard_counts_only_current_resolver_citations(self):
        # three rows scored by the resolver that missed absolute paths and bridge worktrees, one by today's
        rows = [run("claude", role="spec-reader", cites=(0, 5), rev=None) for _ in range(3)]
        rows.append(run("claude", role="spec-reader", cites=(4, 5)))
        sc = self.plan.scorecard(rows)
        c = sc[("spec-reader", "claude")]
        self.assertEqual((c["runs"], c["cites_ok"], c["cites_total"]), (4, 4, 5))
        self.assertFalse(any("citations resolved" in s for s in self.plan.team_suggestions(sc)))

    def test_ask_backs_for_access_are_counted_apart_and_suggest_a_grant(self):
        rows = [run("copilot", role="reliability-critic", ask="Allow access to /x/p1.diff so I can read the patch"),
                run("copilot", role="reliability-critic", ask="Which option should the user pick, 1 or 2?"),
                run("copilot", role="reliability-critic")]
        c = self.plan.scorecard(rows)[("reliability-critic", "copilot")]
        self.assertEqual((c["asked_back"], c["asked_access"]), (2, 1))
        self.assertFalse(any("asked for access" in s for s in self.plan.team_suggestions(self.plan.scorecard(rows))))
        rows.append(run("copilot", role="reliability-critic", ask="I have no shell here to run git show main:a.py"))
        hint = [s for s in self.plan.team_suggestions(self.plan.scorecard(rows)) if "asked for access" in s]
        self.assertEqual(len(hint), 1, hint)
        self.assertIn("reliability-critic on copilot: 2 ask-backs asked for access", hint[0])
        self.assertIn("--add-dir", hint[0])

    def test_learn_team_runs_on_an_empty_ledger(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ledger, "LEDGER", Path(d) / "l.jsonl"):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = self.plan.team([], 30)
        self.assertEqual(rc, 0)
        self.assertIn("no run, board or verdict rows", out.getvalue())


class Replay(unittest.TestCase):
    """`learn --replay`: the saved answers re-scored by a base resolver and the working tree's, read-only."""

    def test_replay_compares_the_resolvers_per_slice_and_writes_nothing(self):
        plan = _load()
        self.assertFalse(plan.held_out("20261116-000000-1-claude"))   # sha256 of the name: the same split every run
        self.assertTrue(plan.held_out("20261116-000001-1-claude"))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ws, runs = root / "ws", root / "runs"
            ws.mkdir()
            runs.mkdir()
            (ws / "a.py").write_text("a = 1\nb = 2\nc = 3\n")
            answer = f"The bug is at {ws}/a.py:2, and a.py:3 reads it."
            fixture = (FAKEBIN / "fixtures" / "claude.jsonl").read_text()
            rows = []
            for stem in ("20261116-000000-1-claude", "20261116-000001-1-claude"):
                log = runs / f"{stem}.json"
                log.write_text(fixture.replace(_fakebin.ANSWER, json.dumps(answer)[1:-1]))
                rows.append({"type": "run", "ts": int(time.time()), "peer": "claude", "kind": "review-change",
                             "rc": 0, "log": str(log), "dir": str(ws)})
            # the resolver before rev 2 cut the leading `/` off an absolute citation and found no such file
            base = types.ModuleType("evidence_before_rev2")
            base.verify_citations = lambda workspace, texts: [
                {"cite": f"{str(ws)[1:]}/a.py:2", "roles": ["run"], "status": "NO SUCH FILE", "text": ""},
                {"cite": "a.py:3", "roles": ["run"], "status": "ok", "text": "c = 3"}]
            led = root / "ledger.jsonl"
            led.write_text("".join(json.dumps(r) + "\n" for r in rows))
            before = led.read_bytes()
            out = io.StringIO()
            with mock.patch.object(ledger, "LEDGER", led), mock.patch.object(ledger, "STATE", root), \
                    redirect_stdout(out):
                self.assertEqual(plan.replay(ledger.rows(), "HEAD", 30, base=base), 0)
            self.assertEqual(led.read_bytes(), before)
        text = out.getvalue()
        self.assertRegex(text, r"held-in\s+1\s+1/2 \(50%\)\s+2/2 \(100%\)\s+1\s+0\n")
        self.assertRegex(text, r"held-out\s+1\s+1/2 \(50%\)\s+2/2 \(100%\)\s+1\s+0\n")
        self.assertIn("20261116-000000-1-claude  1/2 → 2/2", text)
        self.assertNotIn("20261116-000001-1-claude", text)          # held-out: counts only, never an example
        self.assertRegex(text, r"review-change\s+claude\s+4/4 \(100%\)")

    def test_replay_takes_the_runs_bare_prompt_and_what_each_verdict_judged(self):
        from bridgelib.runner import PROTOCOL
        plan = _load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ws, runs = root / "ws", root / "runs"
            ws.mkdir()
            runs.mkdir()
            fixture = (FAKEBIN / "fixtures" / "claude.jsonl").read_text()
            a, b = "20261116-000000-1-claude", "20261116-000002-1-claude"
            (runs / f"{a}.json").write_text(fixture)
            (runs / f"{b}.recover.json").write_text(fixture)         # a recovered run's row names its recovery log
            (runs / f"{a}.prompt.txt").write_text(
                "(Bridge workspace bootstrap\nWorking directory: /w\nRepository root: /repo\n)\n\nReview /x/a.py"
                + PROTOCOL + "\n")
            (root / "panel" / "P").mkdir(parents=True)
            (root / "panel" / "P" / "findings.json").write_text("{}")
            (root / "panel" / "P" / "status.json").write_text(json.dumps({"dir": str(ws)}))
            now = int(time.time())
            rows = [{"type": "run", "ts": now, "peer": "claude", "kind": "review-change",
                     "log": str(runs / f"{a}.json"), "dir": str(ws)},
                    {"type": "run", "ts": now, "peer": "claude", "kind": "review-change",
                     "log": str(runs / f"{b}.recover.json"), "dir": str(ws)},
                    {"type": "verdict", "ts": now, "run": f"{b}.recover", "kept": 1, "rejected": 0},  # as `mark` has it
                    {"type": "verdict", "ts": now, "run": a, "panel": "P", "kept": 1, "rejected": 0}]  # a role in P
            with mock.patch.object(ledger, "STATE", root):
                units = {u["stem"]: u for u in plan.replay_units(rows)}
        self.assertEqual(sorted(units), [a, b])                          # no `panel P`: no verdict judged the panel
        self.assertTrue(all("adjudicated" in u["slices"] for u in units.values()))
        self.assertEqual(units[a]["prompt"], "Review /x/a.py")           # the roots come from what the run was given

    def test_a_trace_replay_cannot_stat_is_left_out_not_fatal(self):
        plan = _load()
        real_is_dir, real_is_file = Path.is_dir, Path.is_file
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ws, locked, runs = root / "ws", root / "locked", root / "runs"
            for p in (ws, locked, runs, root / "panel" / "P"):
                p.mkdir(parents=True)
            fixture = (FAKEBIN / "fixtures" / "claude.jsonl").read_text()
            a, b, c = "20261116-000000-1-claude", "20261116-000003-1-claude", "20261116-000004-1-claude"
            for stem in (a, b, c):
                (runs / f"{stem}.json").write_text(fixture)
            (root / "panel" / "P" / "findings.json").write_text("{}")
            (root / "panel" / "P" / "status.json").write_text(json.dumps({"dir": str(locked)}))
            now = int(time.time())
            rows = [{"type": "run", "ts": now, "peer": "claude", "kind": "review-change",
                     "log": str(runs / f"{s}.json"), "dir": str(w)} for s, w in ((a, locked), (b, ws), (c, ws))]
            rows.append({"type": "verdict", "ts": now, "panel": "P", "kept": 1, "rejected": 0})

            def is_dir(self):          # Python 3.11-3.13: EPERM on a folder macOS privacy controls hide raises
                if self == locked:
                    raise PermissionError(1, "Operation not permitted")
                return real_is_dir(self)

            def is_file(self):         # … and on a log inside one
                if self == runs / f"{c}.json":
                    raise PermissionError(1, "Operation not permitted")
                return real_is_file(self)

            with mock.patch.object(ledger, "STATE", root), mock.patch.object(Path, "is_dir", is_dir), \
                    mock.patch.object(Path, "is_file", is_file):
                units = plan.replay_units(rows)
        self.assertEqual([u["stem"] for u in units], [b])

    def test_the_task_comes_back_from_every_adapters_saved_prompt(self):
        from bridgelib.adapters import ADAPTERS
        from bridgelib.runner import PROTOCOL, protocol
        plan = _load()
        task = "Review /x/a.py:2\n\nthen /y/b.py"
        board = types.SimpleNamespace(topic=1, topic_name="t")          # a --conv run with a board topic
        with tempfile.TemporaryDirectory() as d:
            for name, adapter in ADAPTERS.items():
                for mode in ("ask", "plan", "write"):
                    for worktree, on_board in ((None, False), ("wt", True)):
                        o = argparse.Namespace(mode=mode, dir=d, add_dir=[], worktree=worktree, board=on_board,
                                               conv="rv" if on_board else "", discard_ok=False)
                        for tail in (PROTOCOL, protocol(board)):
                            with self.subTest(peer=name, mode=mode, worktree=worktree):
                                saved = adapter.preamble(o) + task + tail + "\n"      # as runner._execute saves it
                                self.assertEqual(plan.bare_prompt(saved), task)

    def test_an_answer_a_resolver_cannot_stat_is_skipped_not_fatal(self):
        plan = _load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ws, runs = root / "ws", root / "runs"
            ws.mkdir()
            runs.mkdir()
            log = runs / "20261116-000000-1-claude.json"
            log.write_text((FAKEBIN / "fixtures" / "claude.jsonl").read_text())
            rows = [{"type": "run", "ts": int(time.time()), "peer": "claude", "kind": "review-change",
                     "log": str(log), "dir": str(ws)}]
            base = types.ModuleType("evidence_before_rev2")

            def denied(workspace, texts):          # HEAD's resolver on Python 3.11-3.13: is_file() raises on EPERM
                raise PermissionError(13, "Operation not permitted")

            base.verify_citations = denied
            out = io.StringIO()
            with mock.patch.object(ledger, "STATE", root), redirect_stdout(out):
                self.assertEqual(plan.replay(rows, "HEAD", 30, base=base), 0)
        self.assertIn("skipped 1 answer(s)", out.getvalue())

    def test_replay_only_reads(self):
        plan = _load()
        # a sandboxed state even though the guard refuses first: without the guard this call rebuilds the ledger,
        # and a red check of that once rewrote the real one (2026-11-16)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ledger, "STATE", Path(d)), \
                mock.patch.object(ledger, "RUNS", Path(d) / "runs"), \
                mock.patch.object(ledger, "LEDGER", Path(d) / "ledger.jsonl"):
            ledger.record({"type": "verdict", "kept": 1, "rejected": 0})
            before = ledger.LEDGER.read_bytes()
            for o in (argparse.Namespace(replay=True, backfill=True, team=False, days=30, base="HEAD"),
                      argparse.Namespace(replay=True, backfill=False, team=True, days=30, base="HEAD")):
                with redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
                    self.assertEqual(plan.learn(o), 2)
                self.assertIn("--replay only reads", err.getvalue())
            self.assertEqual(ledger.LEDGER.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
