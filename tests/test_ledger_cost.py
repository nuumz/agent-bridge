"""Unit tests for bridgelib.ledger.run_cost — per-run cost out of Claude's session-cumulative total_cost_usd.

Fixtures are the real chains from the ledger (sessions 498141d2 and c8da5cb0, 2026-09-27/28).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from bridgelib.ledger import run_cost  # noqa: E402


def row(session: str, cost_session: float, end: int, duration_s: int = 10) -> dict:
    return {"ts": end, "type": "run", "session": session, "cost_session": cost_session, "duration_s": duration_s}


class RunCost(unittest.TestCase):
    def test_fresh_session_keeps_the_reported_cost(self):
        self.assertEqual(run_cost("s1", 0.42, started=100, prior=[]), 0.42)

    def test_sequential_resumes_subtract_the_previous_total(self):
        prior = [row("498141d2", 0.40, end=57)]
        self.assertAlmostEqual(run_cost("498141d2", 0.54, started=82, prior=prior), 0.14)
        prior.append(row("498141d2", 0.54, end=102))
        self.assertAlmostEqual(run_cost("498141d2", 1.10, started=115, prior=prior), 0.56)

    def test_parallel_resumes_both_start_from_the_last_finished_total(self):
        # two leader runs resumed 2 s apart; the first finished after the second started, so both resumed from 0.42
        prior = [row("c8da5cb0", 0.42, end=1000), row("c8da5cb0", 0.87, end=1678)]
        self.assertAlmostEqual(run_cost("c8da5cb0", 0.87, started=1595, prior=prior), 0.45)

    def test_other_sessions_and_forks_are_not_subtracted(self):
        prior = [row("parent", 0.46, end=10)]
        self.assertEqual(run_cost("fork", 0.028, started=20, prior=prior), 0.028)

    def test_backfilled_rows_finish_at_start_plus_duration(self):
        prior = [{**row("s1", 0.40, end=100, duration_s=60), "backfilled": True}]   # ts = start → finished at 160
        self.assertEqual(run_cost("s1", 0.90, started=130, prior=prior), 0.90)
        self.assertAlmostEqual(run_cost("s1", 0.90, started=170, prior=prior), 0.50)

    def test_no_cost_or_no_session_passes_through(self):
        self.assertIsNone(run_cost("s1", None, started=1, prior=[]))
        self.assertEqual(run_cost(None, 1.5, started=1, prior=[row("s1", 1.0, end=0)]), 1.5)


if __name__ == "__main__":
    unittest.main()
