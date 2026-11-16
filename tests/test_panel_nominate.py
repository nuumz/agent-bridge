"""Self-nomination in a panel: the leader adds a bench role it needs, a worker passes on a question outside its role.
Both stay inside `-p`, and neither a pass nor an unanswered role is recorded as evidence or as `answered`."""

from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

from bridgelib import ledger


def _load(script):
    loader = importlib.machinery.SourceFileLoader(
        f"nominate_{script.replace('-', '_')}", str(BIN / script)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ROLES = {
    "leader": {"name": "leader", "peer": "claude"},
    "implementer": {"name": "implementer", "peer": "cursor"},
    "as-built-realist": {"name": "as-built-realist", "peer": "claude"},
    "design-critic": {
        "name": "design-critic",
        "peer": "cursor",
        "fallback_peer": "claude",
    },
    "contract-checker": {
        "name": "contract-checker",
        "peer": "cursor",
        "fallback_peer": "claude",
    },
    "reliability-critic": {
        "name": "reliability-critic",
        "peer": "copilot",
        "fallback_peer": "claude",
    },
    "root-cause": {"name": "root-cause", "peer": "cursor"},
}


def plan_json(*questions):
    return (
        "```json\n"
        + json.dumps(
            {
                "hypothesis": "h",
                "questions": [
                    {"role": r, "question": f"q for {r}", "why": f"why {r}"}
                    for r in questions
                ],
            }
        )
        + "\n```"
    )


class Bench(unittest.TestCase):
    def setUp(self):
        self.bp = _load("bridge-panel")

    def test_the_bench_is_every_other_role_on_a_peer_minus_p_names(self):
        bench = self.bp.bench(ROLES, ["as-built-realist"], ["claude", "copilot"])
        # its own peer when -p names it, else its fallback; never the leader or the implementer; root-cause has no
        # fallback and its peer is not in -p, so it is not on the bench at all
        self.assertEqual(
            bench,
            {
                "design-critic": "claude",
                "contract-checker": "claude",
                "reliability-critic": "copilot",
            },
        )

    def test_the_leader_adds_at_most_two_bench_roles_once_each(self):
        bench = {
            "design-critic": "claude",
            "contract-checker": "claude",
            "reliability-critic": "copilot",
        }
        text = plan_json(
            "as-built-realist",
            "design-critic",
            "design-critic",
            "root-cause",
            "contract-checker",
            "reliability-critic",
        )
        qs, hyp, requested = self.bp.parse_plan(text, ["as-built-realist"], "p", bench)
        self.assertEqual(
            [q["role"] for q in qs],
            ["as-built-realist", "design-critic", "contract-checker"],
        )
        self.assertEqual(
            requested,
            {
                "design-critic": "why design-critic",
                "contract-checker": "why contract-checker",
            },
        )
        self.assertEqual(hyp, "h")

    def test_an_unparsable_plan_asks_the_chosen_roles_and_adds_none(self):
        qs, _, requested = self.bp.parse_plan(
            "no json here",
            ["as-built-realist"],
            "the problem",
            {"design-critic": "claude"},
        )
        self.assertEqual(
            ([q["role"] for q in qs], requested), (["as-built-realist"], {})
        )

    def test_the_leader_sees_the_bench_and_each_worker_may_pass(self):
        roles = {
            r: {**m, "summary": f"{r} summary", "prompt": "persona"}
            for r, m in ROLES.items()
        }
        frame = self.bp.frame_prompt(
            "p",
            "problem",
            roles,
            ["as-built-realist"],
            roles["leader"],
            {"design-critic": "claude"},
            {},
        )
        self.assertIn("Roles you may add (at most 2)", frame)
        self.assertIn("- design-critic: design-critic summary (peer: claude)", frame)
        self.assertNotIn(
            "Roles you may add",
            self.bp.frame_prompt(
                "p", "problem", roles, ["as-built-realist"], roles["leader"]
            ),
        )
        worker = self.bp.worker_prompt(
            "p", roles["design-critic"], {"question": "q"}, "problem", "", ""
        )
        self.assertIn("reply with only `[[PASS]] <one-line reason>`", worker)


class LeaderOutput(unittest.TestCase):
    """The leader's plan is model output: a malformed item is skipped, never the end of the panel."""

    def setUp(self):
        self.bp = _load("bridge-panel")

    def test_a_malformed_item_is_skipped_and_the_rest_of_the_plan_stands(self):
        text = (
            "```json\n"
            + json.dumps(
                {
                    "hypothesis": "h",
                    "questions": [
                        "a string",
                        None,
                        {"role": ["as-built-realist"], "question": "q"},
                        {"role": {"x": 1}, "question": "q"},
                        {"role": "as-built-realist"},
                        {"role": "as-built-realist", "question": "q1"},
                        {"role": "design-critic", "question": "q2", "why": 7},
                    ],
                }
            )
            + "\n```"
        )
        qs, _, requested = self.bp.parse_plan(
            text, ["as-built-realist"], "p", {"design-critic": "claude"}
        )
        self.assertEqual([q["role"] for q in qs], ["as-built-realist", "design-critic"])
        self.assertEqual(requested, {"design-critic": "7"})

    def test_a_plan_of_bench_roles_only_is_not_a_plan(self):
        qs, _, requested = self.bp.parse_plan(
            plan_json("design-critic"),
            ["as-built-realist"],
            "the problem",
            {"design-critic": "claude"},
        )
        self.assertEqual(
            ([(q["role"], q["question"]) for q in qs], requested),
            ([("as-built-realist", "the problem")], {}),
        )


class Run(unittest.TestCase):
    """`bridge-panel run` with a stub `ask`: who is called on which peer and thread, and what status.json says."""

    def run_panel(self, roles, peers, peer_fn, *flags, bp=None):
        bp = bp or _load("bridge-panel")
        calls = []

        def ask(peer, conv, prompt, o, *a, **k):
            calls.append((peer, conv))
            return peer_fn(peer, conv)

        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "ws").mkdir()
            argv = [
                "bridge-panel",
                "run",
                "p",
                "Check it",
                "-p",
                peers,
                "-r",
                roles,
                "-d",
                str(root / "ws"),
                "--kind",
                "review-change",
                *flags,
            ]
            with (
                mock.patch.object(bp, "PANELS", root / "panel"),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(bp, "hop_active", return_value=False),
                mock.patch.object(bp, "ask", side_effect=ask),
                mock.patch.object(bp, "ledger_row") as row,
                mock.patch.object(bp, "post"),
                mock.patch.object(bp, "close_panel_topics") as close,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(err),
            ):
                rc = bp.main()
            out = root / "panel" / "p"
            status = (
                json.loads((out / "status.json").read_text())
                if (out / "status.json").is_file()
                else {}
            )
            report = (
                (out / "report.md").read_text() if (out / "report.md").is_file() else ""
            )
        return rc, calls, status, report, row, close

    def test_a_role_skipped_from_minus_r_is_not_offered_back_on_the_bench(self):
        frames = []

        def peer(p, conv):
            if conv.endswith("-leader"):
                frames.append(conv)
                return 0, plan_json("as-built-realist", "design-critic")
            return 0, "a finding"

        bp = _load("bridge-panel")
        with mock.patch.object(bp, "frame_prompt", wraps=bp.frame_prompt) as frame:
            rc, calls, status, _, _, _ = self.run_panel(
                "as-built-realist,design-critic",
                "claude",
                peer,
                "--no-cross",
                "--allow-single",
                bp=bp,
            )
        self.assertEqual(rc, 0)
        self.assertNotIn(
            "design-critic", frame.call_args.args[5]
        )  # cursor is not in -p: the user opts in
        self.assertEqual(status["requested"], {})
        self.assertFalse([c for c in calls if "design-critic" in c[1]])

    def test_a_role_the_leader_adds_runs_on_its_bench_peer_and_is_named(self):
        def peer(p, conv):
            return (
                (0, plan_json("as-built-realist", "design-critic"))
                if conv.endswith("-leader")
                else (0, "a finding")
            )

        rc, calls, status, report, row, close = self.run_panel(
            "as-built-realist", "claude,copilot", peer, "--no-cross"
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            ("claude", "panel-p-design-critic-claude"), calls
        )  # its fallback: cursor is not in -p
        self.assertFalse([c for c in calls if c[0] not in ("claude", "copilot")])
        self.assertEqual(status["requested"], {"design-critic": "why design-critic"})
        self.assertEqual(
            status["answered"],
            {"as-built-realist": "claude", "design-critic": "claude"},
        )
        self.assertIn(
            "> Roles the leader added (not in -r):\n> - design-critic (claude): why design-critic",
            report,
        )
        self.assertIn("design-critic (claude, added)", report)
        self.assertEqual(row.call_args.args[11], {"design-critic": "why design-critic"})
        self.assertEqual(
            close.call_args.args[1], ["as-built-realist", "design-critic"]
        )  # its threads close too

    def test_a_pass_is_not_a_finding_and_two_voices_still_need_two_answers(self):
        def peer(p, conv):
            if conv.endswith("-leader"):
                return 0, plan_json("as-built-realist", "design-critic")
            return (
                (0, "  [[PASS]] no code path named in the case")
                if "design-critic" in conv
                else (0, "a finding")
            )

        rc, _, status, report, row, _ = self.run_panel(
            "as-built-realist,design-critic=claude", "claude", peer, "--no-cross"
        )
        self.assertEqual(rc, 4)  # one answer left: below two
        self.assertEqual(
            status["passed"], {"design-critic": "no code path named in the case"}
        )
        self.assertEqual(status["answered"], {"as-built-realist": "claude"})
        self.assertTrue(status["degraded"])
        self.assertEqual(report, "")
        self.assertEqual(
            row.call_args.kwargs["passed"],
            {"design-critic": "no code path named in the case"},
        )

    def test_with_enough_answers_a_pass_is_reported_and_kept_out_of_the_record(self):
        def peer(p, conv):
            if conv.endswith("-leader"):
                return 0, plan_json(
                    "as-built-realist", "design-critic", "reliability-critic"
                )
            return (
                (0, "[[PASS]] outside my role")
                if "design-critic" in conv
                else (0, "a finding")
            )

        rc, calls, status, report, _, _ = self.run_panel(
            "as-built-realist,design-critic=claude,reliability-critic",
            "claude,copilot",
            peer,
        )
        self.assertEqual(rc, 0)
        # the first pass only: a pass gets no cross-examination, the two answering roles get one each
        self.assertEqual(calls.count(("claude", "panel-p-design-critic-claude")), 1)
        self.assertEqual(calls.count(("copilot", "panel-p-reliability-critic")), 2)
        self.assertIn("passed: design-critic", report)
        self.assertIn(
            "> Roles that passed (declined their question — not evidence):\n> - design-critic: outside my role",
            report,
        )
        self.assertNotIn("design-critic", status["answered"])


class Mark(unittest.TestCase):
    def test_a_role_that_passed_cannot_be_marked(self):
        plan = _load("bridge-plan")
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(ledger, "LEDGER", Path(d) / "l.jsonl"),
        ):
            ledger.record(
                {
                    "type": "panel",
                    "name": "p",
                    "kind": "review-change",
                    "answered": {},
                    "passed": ["design-critic"],
                    "rc": 4,
                }
            )
            o = argparse.Namespace(
                target="p", role="design-critic", kept=1, rejected=0, note=None
            )
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(plan.mark(o), 2)
            self.assertIn("passed on panel 'p'", err.getvalue())
            self.assertEqual([r["type"] for r in ledger.rows()], ["panel"])


class Backfill(unittest.TestCase):
    def test_a_rebuilt_panel_row_keeps_who_passed_and_who_was_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "panel" / "p"
            d.mkdir(parents=True)
            (root / "runs").mkdir()
            (d / "findings.json").write_text(json.dumps({"as-built-realist": "f"}))
            (d / "status.json").write_text(
                json.dumps(
                    {
                        "answered": {"as-built-realist": "claude"},
                        "passed": {"design-critic": "outside"},
                        "requested": {"design-critic": "why"},
                    }
                )
            )
            (d / "report.md").write_text(
                f"# Panel `p` — {time.strftime('%Y-%m-%d %H:%M')}\n"
            )
            with (
                mock.patch.object(ledger, "STATE", root),
                mock.patch.object(ledger, "RUNS", root / "runs"),
                mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"),
            ):
                ledger.backfill()
                row = next(r for r in ledger.rows() if r.get("type") == "panel")
        self.assertEqual(
            (row["passed"], row["requested"]), (["design-critic"], ["design-critic"])
        )


if __name__ == "__main__":
    unittest.main()
