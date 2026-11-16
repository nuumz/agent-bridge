"""The roster: who runs each panel role among the peers `-p` allows (`bridge-plan`), and how `bridge-panel` takes it
as `-r role=peer` — an assignment never leaves `-p`, and the record says who answered, not who was asked."""
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

from bridgelib import grill, ledger


def _load(script):
    loader = importlib.machinery.SourceFileLoader(f"roster_{script.replace('-', '_')}", str(BIN / script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


META = {"design-critic": ("cursor", "claude"), "as-built-realist": ("claude", None),
        "reliability-critic": ("copilot", "claude")}


def marks(role, peer, kept, total, panels):
    """`total` marked findings for (role, peer), `kept` of them kept, spread over `panels` panels."""
    rows = [{"type": "verdict", "ts": int(time.time()), "role": role, "peer": peer, "panel": f"{role}-{peer}-{i}",
             "kept": 0, "rejected": 0} for i in range(panels)]
    rows[0].update(kept=kept, rejected=total - kept)
    return rows


class Roster(unittest.TestCase):
    def setUp(self):
        self.plan = _load("bridge-plan")
        patch = mock.patch.object(self.plan, "role_peers", return_value=META)
        patch.start()
        self.addCleanup(patch.stop)

    def pick(self, role, allowed, rows, blocked=lambda p: None):
        (_, peer, why), = self.plan.roster([role], allowed, rows, blocked)
        return peer, why

    def test_a_role_keeps_its_own_peer_until_both_sides_are_measured(self):
        rows = marks("design-critic", "claude", 10, 10, 3)
        self.assertEqual(self.pick("design-critic", ["claude", "cursor"], rows)[0], "cursor")       # cursor: no marks
        rows += marks("design-critic", "cursor", 1, 4, 3)                                            # 4 findings < 5
        self.assertEqual(self.pick("design-critic", ["claude", "cursor"], rows)[0], "cursor")
        thin = marks("design-critic", "cursor", 1, 6, 3) + marks("design-critic", "claude", 10, 10, 2)   # 2 panels < 3
        self.assertEqual(self.pick("design-critic", ["claude", "cursor"], thin)[0], "cursor")

    def test_a_measured_peer_takes_the_role_only_past_the_margin(self):
        rows = marks("design-critic", "cursor", 3, 6, 3) + marks("design-critic", "claude", 9, 10, 3)
        peer, why = self.pick("design-critic", ["claude", "cursor"], rows)
        self.assertEqual(peer, "claude")
        self.assertIn("measured: 9/10 kept over 3 panels vs cursor 3/6 kept over 3 panels", why)
        close = marks("design-critic", "cursor", 8, 10, 3) + marks("design-critic", "claude", 17, 20, 3)   # +0.05
        self.assertEqual(self.pick("design-critic", ["claude", "cursor"], close)[0], "cursor")
        edge = marks("design-critic", "cursor", 8, 10, 3) + marks("design-critic", "claude", 9, 10, 3)     # +0.10
        self.assertEqual(self.pick("design-critic", ["claude", "cursor"], edge)[0], "claude")
        # exactly +0.10 where float arithmetic says less (1/6 >= 1/15 + 0.10 is False in binary floating point)
        edge = marks("design-critic", "cursor", 1, 15, 3) + marks("design-critic", "claude", 1, 6, 3)
        self.assertEqual(self.pick("design-critic", ["claude", "cursor"], edge)[0], "claude")

    def test_no_role_ever_lands_outside_the_peers_allowed(self):
        rows = marks("design-critic", "cursor", 3, 6, 3) + marks("design-critic", "copilot", 10, 10, 3)
        peer, why = self.pick("design-critic", ["claude"], rows)
        self.assertEqual(peer, "claude")
        self.assertIn("its fallback — cursor is not in -p", why)
        peer, why = self.pick("design-critic", ["copilot"], rows)          # neither its peer nor its fallback
        self.assertIsNone(peer)
        self.assertIn("-r design-critic=<peer>", why)
        peer, why = self.pick("as-built-realist", ["copilot", "cursor"], [])     # a role with no fallback at all
        self.assertIsNone(peer)
        self.assertIn("its peer claude is not in -p", why)

    def test_a_peer_out_of_quota_is_passed_over_while_another_stands(self):
        def out(p):
            return "out of usage" if p == "copilot" else None

        peer, why = self.pick("reliability-critic", ["claude", "copilot"], [], out)
        self.assertEqual((peer, why), ("claude", "copilot is out of quota now"))
        peer, why = self.pick("reliability-critic", ["copilot"], [], out)
        self.assertEqual(peer, "copilot")
        self.assertIn("out of quota now, and neither its own peer nor its fallback can stand in", why)

    def test_the_gap_is_named_only_when_every_running_role_is_the_leaders_family(self):
        gap = self.plan.cross_model_gap
        self.assertIn("no worker role runs outside claude", gap([("a", "claude", ""), ("b", None, ""), ("c", "claude", "")]))
        self.assertEqual(gap([("a", "claude", ""), ("b", "copilot", "")]), "")
        self.assertEqual(gap([("a", None, "")]), "")


class Card(unittest.TestCase):
    """`bridge-plan` prints the roster, carries it in the command with the kind, and holds a design decision that has
    no cross-model view behind an open field."""

    def plan_card(self, kind, peers, level, rc=0):
        plan = _load("bridge-plan")
        book = {kind: {"name": kind, "summary": "s", "form": "panel", "level": level, "peers": "claude",
                       "panel_roles": "as-built-realist,design-critic,reliability-critic"}}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ledger, "LEDGER", Path(d) / "l.jsonl"), \
                mock.patch.object(ledger, "load_playbooks", return_value=book), \
                mock.patch.object(plan, "role_peers", return_value=META), \
                mock.patch.object(plan, "quota_block", return_value=None), \
                mock.patch.object(plan, "session_card_path", return_value=Path(d) / "card.md"), \
                contextlib.redirect_stdout(out):
            o = argparse.Namespace(request="decide x", kind=kind, name="t", peers=peers, dir="/r", force=False)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(plan.plan(o), rc)
            card = (Path(d) / "card.md").read_text() if (Path(d) / "card.md").is_file() else ""
        return out.getvalue(), card

    def test_the_command_runs_the_roster_the_card_shows(self):
        out, card = self.plan_card("design-decision", "claude,copilot", "confirm")
        self.assertIn("roster   design-critic -> claude — its fallback — cursor is not in -p", out)
        self.assertIn("-r as-built-realist=claude,design-critic=claude,reliability-critic=copilot -d /r -t 420 "
                      "--kind design-decision", out)
        self.assertIn("roles=as-built-realist=claude,design-critic=claude,reliability-critic=copilot "
                      "kind=design-decision agreement=", out)           # the Agent form carries the card too
        self.assertNotIn("WARNING", out)
        self.assertNotIn("Cross-model view", card)
        self.assertEqual(grill.open_field_todos(card), [])

    def test_a_design_decision_without_a_cross_model_view_waits_for_the_user(self):
        out, card = self.plan_card("design-decision", "claude", "confirm")
        self.assertIn("WARNING  no worker role runs outside claude", out)
        self.assertIn("open     ## Cross-model view", out)
        todos = grill.open_field_todos(card)             # the run refuses while it stands (bridge-panel --agreement)
        self.assertEqual(len(todos), 1, todos)
        self.assertIn("no worker role runs outside claude", todos[0])
        self.assertEqual(grill.open_field_todos(card.replace(todos[0], "accepted")), [])

    def test_a_review_without_one_is_warned_and_runs(self):
        out, card = self.plan_card("review-change", "claude", "confirm")
        self.assertIn("WARNING  no worker role runs outside claude", out)
        self.assertNotIn("Cross-model view", out)
        self.assertEqual(grill.open_field_todos(card), [])

    def test_no_role_left_under_the_peers_allowed_prints_no_command(self):
        out, _ = self.plan_card("review-change", "codex", "routine", rc=3)    # no role's peer or fallback is codex
        self.assertNotIn("command", out)
        self.assertIn("roster   as-built-realist -> (not run)", out)


class Assigned(unittest.TestCase):
    """`bridge-panel run -r role=peer`."""

    def run_panel(self, roles, peers, peer_fn, *flags):
        bp = _load("bridge-panel")
        calls = []

        def ask(peer, conv, prompt, o, *a, **k):
            calls.append((peer, conv))
            return peer_fn(peer, conv)

        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "ws").mkdir()
            argv = ["bridge-panel", "run", "p", "Check it", "-p", peers, "-r", roles, "-d", str(root / "ws"),
                    "--kind", "review-change", *flags]
            with mock.patch.object(bp, "PANELS", root / "panel"), mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(bp, "hop_active", return_value=False), mock.patch.object(bp, "ask", side_effect=ask), \
                    mock.patch.object(bp, "ledger_row"), mock.patch.object(bp, "post"), \
                    mock.patch.object(bp, "close_panel_topics"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = bp.main()
            out = root / "panel" / "p"
            status = json.loads((out / "status.json").read_text()) if (out / "status.json").is_file() else {}
            report = (out / "report.md").read_text() if (out / "report.md").is_file() else ""
        return rc, calls, status, report, err.getvalue()

    def test_an_assignment_outside_the_egress_gate_runs_nothing(self):
        rc, calls, _, _, err = self.run_panel("design-critic=cursor,as-built-realist", "claude", lambda p, c: (0, "x"))
        self.assertEqual((rc, calls), (2, []))
        self.assertIn("design-critic=cursor", err)
        rc, calls, _, _, err = self.run_panel("design-critic,design-critic=claude", "claude", lambda p, c: (0, "x"))
        self.assertEqual((rc, calls), (2, []))
        self.assertIn("named twice", err)

    def test_an_assigned_role_runs_on_that_peer_in_one_thread_and_the_report_says_one_family_answered(self):
        rc, calls, status, report, err = self.run_panel("design-critic=claude,as-built-realist", "claude",
                                                        lambda p, c: (0, "a finding"))
        self.assertEqual(rc, 0)
        self.assertEqual(status["answered"], {"design-critic": "claude", "as-built-realist": "claude"})
        self.assertEqual(calls.count(("claude", "panel-p-design-critic-claude")), 2)     # first pass, cross-examination
        self.assertFalse([c for c in calls if c[1] == "panel-p-design-critic"])
        self.assertIn("**No cross-model view** — every answering role ran on claude", report)
        self.assertIn("[panel] WARNING no cross-model view", err)

    def test_a_bare_role_whose_peer_is_not_allowed_is_skipped_and_the_way_to_run_it_is_named(self):
        rc, _, status, _, err = self.run_panel("design-critic,as-built-realist", "claude", lambda p, c: (0, "f"),
                                                   "--allow-single", "--no-cross")
        self.assertEqual(rc, 0)
        self.assertEqual(status["answered"], {"as-built-realist": "claude"})
        self.assertIn("-r design-critic=claude", err)

    def test_a_failed_assigned_peer_falls_back_in_the_fallbacks_own_thread(self):
        def peer(p, conv):
            return (7, "out of usage") if p == "copilot" else (0, "a finding")

        rc, calls, status, report, _ = self.run_panel("design-critic=copilot,as-built-realist", "claude,copilot", peer,
                                                      "--no-cross")
        self.assertEqual(rc, 0)
        self.assertIn(("copilot", "panel-p-design-critic-copilot"), calls)
        self.assertIn(("claude", "panel-p-design-critic-claude"), calls)        # not …-copilot-claude
        self.assertEqual(status["answered"]["design-critic"], "claude")
        self.assertIn("design-critic", status["fallbacks"])
        self.assertIn("No cross-model view", report)                            # the fallback removed the other family

    def test_a_role_the_leader_did_not_dispatch_is_not_recorded_as_answered(self):
        plan = '```json\n{"hypothesis": "h", "questions": [{"role": "as-built-realist", "question": "q"}]}\n```'
        rc, _, status, _, _ = self.run_panel("design-critic=claude,as-built-realist", "claude",
                                             lambda p, c: (0, plan if c.endswith("-leader") else "f"),
                                             "--allow-single", "--no-cross")
        self.assertEqual(rc, 0)
        self.assertEqual(status["answered"], {"as-built-realist": "claude"})

    def test_a_role_whose_run_failed_is_not_recorded_as_answered(self):
        def peer(p, conv):
            return (1, "crash") if "reliability-critic" in conv else (0, "a finding")

        # copilot fails and so does its fallback; as-built-realist (no fallback) answers
        rc, _, status, _, _ = self.run_panel("reliability-critic,as-built-realist", "claude,copilot", peer,
                                             "--allow-single", "--no-cross")
        self.assertEqual(rc, 0)
        self.assertEqual(status["answered"], {"as-built-realist": "claude"})
        self.assertIn("reliability-critic", status["failed"])
        self.assertIn("reliability-critic", status["fallbacks"])

    def test_an_ask_back_names_the_thread_of_the_peer_that_asked(self):
        def peer(p, conv):
            return (5, "[[ASK]] which spec?") if "design-critic" in conv else (0, "a finding")

        _, _, _, report, _ = self.run_panel("design-critic=claude,as-built-realist", "claude", peer, "--no-cross")
        self.assertIn('`ask-agent claude --conv panel-p-design-critic-claude "<answer>"`', report)


if __name__ == "__main__":
    unittest.main()
