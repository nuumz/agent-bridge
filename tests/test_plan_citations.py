"""`bridge-plan learn`: where `peer review:` rows are looked for, which spellings count, and what an unscanned tree says."""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

from bridgelib import ledger


def _load():
    loader = importlib.machinery.SourceFileLoader("bridge_plan_mod", str(BIN / "bridge-plan"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def panel(name, kind="doc-readiness"):
    return {"type": "panel", "ts": int(time.time()), "name": name, "kind": kind, "roles": ["spec-reader"],
            "answered": {"spec-reader": "claude"}, "failed": [], "rc": 0}


def learn_output(plan, rows, roots):
    with tempfile.TemporaryDirectory() as d, mock.patch.object(ledger, "LEDGER", Path(d) / "l.jsonl"), \
            mock.patch.object(plan, "doc_roots", lambda _rows: roots):
        ledger.LEDGER.write_text("".join(json.dumps(r) + "\n" for r in rows))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = plan.learn(argparse.Namespace(replay=False, backfill=False, team=False, days=30, base="HEAD"))
    return rc, out.getvalue()


class DocRoots(unittest.TestCase):
    def setUp(self):
        self.plan = _load()

    def test_the_environment_names_the_roots_when_set(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, \
                mock.patch.dict(os.environ, {"BRIDGE_DOC_ROOTS": a}):
            self.assertEqual(self.plan.doc_roots([{"type": "run", "dir": b}]), [Path(a)])

    def test_without_it_the_roots_are_where_the_runs_ran_and_the_repository_learn_is_called_from(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as repo, \
                mock.patch.dict(os.environ, {"BRIDGE_DOC_ROOTS": ""}):
            subprocess.run(["git", "init", "-q", repo], check=True)
            inside = Path(repo) / "docs"
            (Path(a) / "specs").mkdir()
            inside.mkdir()
            rows = [{"type": "run", "dir": a}, {"type": "run", "dir": a}, {"type": "run", "dir": str(Path(a) / "specs")},
                    {"type": "run", "dir": str(Path(a) / "gone")}, {"type": "run"}]
            with mock.patch.object(os, "getcwd", lambda: str(inside)):
                roots = self.plan.doc_roots(rows)
            # one root per tree: a directory under another root is not read twice, a removed one not at all
            self.assertEqual(roots, sorted([Path(a).resolve(), Path(repo).resolve()]))
            with mock.patch.object(os, "getcwd", lambda: a):           # outside a repository: the cwd is not a root
                self.assertEqual(self.plan.doc_roots([]), [])


class Citations(unittest.TestCase):
    def setUp(self):
        self.plan = _load()

    def test_a_backticked_panel_name_is_the_same_citation(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.md").write_text("x (peer review: panel `owner-branch`, 2026-10-18)\n"
                                          "y peer review: panel owner-branch, 2026-10-18\n"
                                          "z peer review: ask-claude ×3, 2026-09-18\n")
            (Path(d) / "b.txt").write_text("peer review: panel owner-branch\n")
            cites = self.plan.citations([Path(d)])
        self.assertEqual(cites, {"panel owner-branch": 2, "ask-claude": 1})

    def test_learn_counts_a_cited_panel_and_names_the_roots_it_read(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.md").write_text("peer review: panel `p1`, 2026-10-18\n")
            rc, out = learn_output(self.plan, [panel("p1"), panel("p2"), panel("p3")], [Path(d)])
        self.assertEqual(rc, 0)
        self.assertNotIn("none cited", out)
        self.assertIn(f"citations in docs (scanned: {d})", out)
        self.assertIn("panel p1=1", out)

    def test_learn_still_says_so_when_the_docs_it_read_cite_no_panel(self):
        with tempfile.TemporaryDirectory() as d:
            _, out = learn_output(self.plan, [panel("p1"), panel("p2"), panel("p3")], [Path(d)])
        self.assertIn("doc-readiness: 3 panels, none cited", out)

    def test_no_root_to_read_is_not_a_count_of_zero(self):
        rc, out = learn_output(self.plan, [panel("p1"), panel("p2"), panel("p3")], [])
        self.assertEqual(rc, 0)
        self.assertNotIn("none cited", out)
        self.assertIn("citations in docs: not scanned", out)
        self.assertRegex(out, r"doc-readiness\s+0\s+0%\s+3\s+0\s+0\.0\s+0\.00\s+-\s")


class MarkCoverage(unittest.TestCase):
    def setUp(self):
        self.plan = _load()

    def test_learn_says_how_much_of_the_work_a_kept_rate_rests_on(self):
        now = int(time.time())
        rows = [panel("p1"), panel("p2"),
                {"type": "run", "ts": now, "peer": "claude", "kind": "review-change", "rc": 0,
                 "log": "/x/20261115-1-claude.json"},
                {"type": "run", "ts": now, "peer": "copilot", "kind": "review-change", "rc": 0,
                 "log": "/x/20261115-2-copilot.json"},
                {"type": "run", "ts": now, "peer": "claude", "kind": "doc-readiness", "rc": 0, "panel": "p1",
                 "conv": "panel-p1-spec-reader", "log": "/x/20261115-3-claude.json"},
                {"type": "verdict", "ts": now, "panel": "p1", "role": "spec-reader", "peer": "claude", "kept": 2,
                 "rejected": 1},
                {"type": "verdict", "ts": now, "run": "20261115-1-claude", "kind": "review-change", "peer": "claude",
                 "kept": 1, "rejected": 0}]
        _, out = learn_output(self.plan, rows, [])
        self.assertIn("marked: 1/2 panels, 1/2 direct runs", out)


if __name__ == "__main__":
    unittest.main()
