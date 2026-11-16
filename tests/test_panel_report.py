"""bridge-panel: the report head (who really answered — one line a reader checks before trusting a synthesis), the
cross-examination's list of a role's own unresolved citations, and the traces gc keeps."""
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
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

from bridgelib import core, ledger  # noqa: E402


def _load():
    loader = importlib.machinery.SourceFileLoader("bridge_panel_mod", str(BIN / "bridge-panel"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Voices(unittest.TestCase):
    def test_contribution_line_separates_real_voices_from_fallbacks_failures_and_ask_backs(self):
        bp = _load()
        findings = {"root-cause": "...", "design-critic": "...", "spec-reader": "[[ASK]] which spec?"}
        used = {"root-cause": "cursor", "design-critic": "claude", "spec-reader": "claude"}
        fallbacks = {"design-critic": "cursor → claude (quota)"}
        failed = {"reliability-critic": "(run failed rc=7) out of usage"}
        line = bp.contribution_line(findings, used, fallbacks, failed, {"spec-reader": "..."})
        self.assertEqual(line, "contributed: root-cause (cursor) · degraded: design-critic (cursor → claude) · "
                               "failed: reliability-critic · asked back: spec-reader")
        self.assertEqual(bp.contribution_line({}, {}, {}, {}, {}), "contributed: none")
        # a role whose fallback also failed is failed, not degraded
        line = bp.contribution_line({"a": "."}, {"a": "x"}, {"b": "x → y (quota)"}, {"b": "(run failed)"}, {})
        self.assertEqual(line, "contributed: a (x) · failed: b")

    def test_synth_rebuilds_the_same_line_from_status_json(self):
        bp = _load()
        status = {"answered": {"r": "cursor", "d": "claude"}, "fallbacks": {"d": "cursor → claude (quota)"},
                  "failed": {"f": "(run failed rc=7)"}, "asked_back": ["s"], "degraded": True}
        self.assertEqual(bp.voices_from_status({"r": ".", "d": ".", "s": "."}, status),
                         "contributed: r (cursor) · degraded: d (cursor → claude) · failed: f · asked back: s")


class Traces(unittest.TestCase):
    def test_cross_prompt_lists_only_the_roles_own_unresolved(self):
        bp = _load()
        rows = [{"cite": "a.py:9", "roles": ["A"], "status": "BLANK LINE (wrong anchor?)", "text": ""},
                {"cite": "b.py:2", "roles": ["B"], "status": "NO SUCH FILE", "text": ""},
                {"cite": "c.py:1", "roles": ["A"], "status": "ok", "text": "x = 1"}]
        prompt = bp.cross_prompt("p", {"name": "A"}, {"B": "B's finding"}, [], "", rows)
        self.assertIn("Your own citations that did not resolve:\n- `a.py:9` — BLANK LINE (wrong anchor?)\n", prompt)
        self.assertNotIn("b.py:2", prompt)
        self.assertNotIn("c.py:1", prompt)
        self.assertNotIn("did not resolve", bp.cross_prompt("p", {"name": "C"}, {"B": "B's finding"}, [], "", rows))

    def test_gc_keeps_traces_a_verdict_names(self):
        bp = _load()
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            runs, panels = state / "runs", state / "panel"
            runs.mkdir()
            files = [runs / n for n in ("A-claude.json", "A-claude.err", "A-claude.prompt.txt",
                                        "A-claude.recover.json", "B-claude.json", "C-cursor.json",
                                        "C-cursor.prompt.txt", "C-cursor.recover.json", "D-claude.json")]
            for p in ("P1", "P2"):
                (panels / p).mkdir(parents=True)
                files.append(panels / p / "findings.json")
            old = time.time() - 30 * 86400
            for f in files:
                f.write_text("{}")
                os.utime(f, (old, old))
            out = io.StringIO()
            with mock.patch.object(ledger, "LEDGER", state / "ledger.jsonl"), mock.patch.object(bp, "STATE", state), \
                    mock.patch.object(bp, "PANELS", panels), redirect_stdout(out):
                ledger.record({"type": "verdict", "run": "A-claude", "kept": 2, "rejected": 0})
                ledger.record({"type": "verdict", "panel": "P1", "role": "root-cause", "kept": 1, "rejected": 1})
                # `mark` given the log= path of a recovered run keeps `.recover`; a role's run names its panel
                ledger.record({"type": "verdict", "run": "C-cursor.recover", "kept": 1, "rejected": 0})
                ledger.record({"type": "verdict", "run": "D-claude", "panel": "P2", "kept": 1, "rejected": 0})
                self.assertEqual(bp.gc(argparse.Namespace(days=14, dry_run=False)), 0)
            self.assertEqual(sorted(f.name for f in runs.iterdir()),
                             ["A-claude.err", "A-claude.json", "A-claude.prompt.txt", "A-claude.recover.json",
                              "C-cursor.json", "C-cursor.prompt.txt", "C-cursor.recover.json", "D-claude.json"])
            self.assertTrue((panels / "P1" / "findings.json").is_file())
            self.assertFalse((panels / "P2").exists())
        self.assertIn("1 panel dir(s) and 8 run file(s) a verdict names", out.getvalue())

    def test_synth_checks_citations_against_the_roots_the_run_had(self):
        bp = _load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ws, spec, extra, panel = root / "ws", root / "spec", root / "extra", root / "panel" / "p"
            for p in (ws, spec, extra, panel):
                p.mkdir(parents=True)
            (spec / "rules.md").write_text("one\ntwo\nthree\n")
            (extra / "notes.md").write_text("a note\n")
            (ws / "a.py").write_text("x = 1\n")
            # the run had -d ws, --add-dir spec and its kind; synth runs again from another directory with none of
            # them, given `--add-dir extra` (main grants it through the environment)
            (panel / "plan.json").write_text(json.dumps({"questions": [{"question": "Is the rule followed?"}]}))
            (panel / "findings.json").write_text(json.dumps(
                {"critic": "The rule is at rules.md:2, used at a.py:1, noted at notes.md:1."}))
            status = {"answered": {"critic": "claude"}, "problem": "Check it", "dir": str(ws),
                      "kind": "doc-readiness", "add_dir": [str(spec), str(root / "gone")]}   # one root removed since
            (panel / "status.json").write_text(json.dumps(status))
            o = argparse.Namespace(name="p", leader="claude", peers="claude", dir=None, add_dir=[], timeout=420,
                                   allow_single=True)     # one saved finding: synthesised only when asked to
            runs = []

            def leader(argv, *a, **k):
                runs.append((argv, os.environ.get(core.ADD_DIRS_VAR, "").split(os.pathsep)))
                return subprocess.CompletedProcess(argv, 0, "synthesis\n", "")

            with mock.patch.object(bp, "PANELS", root / "panel"), mock.patch.object(bp, "close_panel_topics"), \
                    mock.patch.object(bp, "run_tool", side_effect=leader), \
                    mock.patch.dict(os.environ, {core.ADD_DIRS_VAR: str(extra)}), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(bp.synth(o), 0)
            cites = json.loads((panel / "citations.json").read_text())
        self.assertEqual(len(runs), 1)
        argv, granted = runs[0]
        self.assertEqual(granted, [str(extra), str(spec.resolve())])    # its root and the run's, not the removed one
        # … but report.md, which reaches `fix` and its implementer's vendor, carries lines from the run's roots only
        self.assertEqual({Path(c["cite"].split()[0]).name: c["status"] for c in cites},     # minus "(written as …)"
                         {"rules.md:2": "ok", "a.py:1": "ok", "notes.md:1": "NO SUCH FILE"})
        self.assertEqual(argv[argv.index("-d") + 1], str(ws))              # in the run's workspace
        self.assertEqual(argv[argv.index("--kind") + 1], "doc-readiness")  # under the run's kind
        self.assertIn("Problem:\n\nCheck it", argv[-1])                    # on the run's question, not the leader's

    def test_synth_from_one_answering_role_needs_allow_single(self):
        bp = _load()
        with tempfile.TemporaryDirectory() as d:
            panel = Path(d) / "panel" / "p"
            panel.mkdir(parents=True)
            (panel / "plan.json").write_text(json.dumps({"questions": [{"question": "q"}]}))
            (panel / "findings.json").write_text(json.dumps({"critic": "one voice"}))
            o = argparse.Namespace(name="p", leader="claude", peers="claude", dir=None, add_dir=[], timeout=420,
                                   allow_single=False)
            err = io.StringIO()
            with mock.patch.object(bp, "PANELS", Path(d) / "panel"), mock.patch.object(bp, "close_panel_topics"), \
                    mock.patch.object(bp, "ask", return_value=(0, "synthesis")) as ask, \
                    redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(bp.synth(o), 4)
            self.assertFalse((panel / "report.md").exists())
        ask.assert_not_called()
        self.assertIn("1 answering role(s) (critic); need 2", err.getvalue())

    def test_a_run_refused_at_its_input_checks_keeps_the_earlier_record(self):
        bp = _load()
        with tempfile.TemporaryDirectory() as d:
            panel = Path(d) / "panel" / "p"
            panel.mkdir(parents=True)
            for f in ("findings.json", "status.json", "cross.json"):
                (panel / f).write_text("{}")
            argv = ["bridge-panel", "run", "p", "Check it", "-p", "claude", "-r", "no-such-role", "-d", d]
            with mock.patch.object(bp, "PANELS", Path(d) / "panel"), mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(bp, "hop_active", return_value=False), mock.patch.object(bp, "ask") as ask, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(bp.main(), 2)
            left = sorted(f.name for f in panel.iterdir())
        ask.assert_not_called()
        self.assertEqual(left, ["cross.json", "findings.json", "rc", "status.json"])

    def test_fix_rechecks_the_patch_in_the_runs_workspace(self):
        bp = _load()
        imp = bp.load_roles()["implementer"]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ws, panel = root / "ws", root / "panel" / "p"
            for p in (ws, panel):
                p.mkdir(parents=True)
            (root / "spec").mkdir()
            (panel / "report.md").write_text("# Panel `p`\n")
            # status.json keeps the roots the run's roles were granted, absolute, and the run's kind
            status = {"dir": str(ws), "add_dir": [str(root / "spec")], "kind": "doc-readiness"}
            (panel / "status.json").write_text(json.dumps(status))
            o = argparse.Namespace(name="p", peers=imp["peer"], dir=None, add_dir=[])
            granted = []

            def implementer(*a, **k):
                granted.append(os.environ.get(core.ADD_DIRS_VAR, "").split(os.pathsep))
                return 0, "a patch"

            with mock.patch.object(bp, "PANELS", root / "panel"), mock.patch.dict(os.environ), \
                    mock.patch.object(bp, "ask", side_effect=implementer) as ask, \
                    mock.patch.object(bp, "check_patch", return_value="verdict") as check, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(bp.fix(o), 0)
            self.assertIn(str((root / "spec").resolve()), granted[0])
        self.assertEqual((ask.call_args.args[3].dir, check.call_args.args[0]), (str(ws), str(ws)))
        self.assertEqual(ask.call_args.args[3].kind_resolved, "doc-readiness")      # ask() passes it as --kind

    def test_a_run_keeps_what_synth_and_fix_re_run_with(self):
        bp = _load()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            ws, cwd, spec, given = root / "ws", root / "cwd", root / "spec", root / "given"
            for p in (ws, cwd, spec, given):
                p.mkdir()
            grants = []

            def peer(name, conv, prompt, o, *a, **k):      # the leader frames; the role fails, so the panel aborts
                grants.append(os.environ.get(core.ADD_DIRS_VAR, "").split(os.pathsep))
                return (0, "") if conv.endswith("-leader") else (1, "")

            # `--add-dir ../spec` is granted from the cwd, not from -d; `given` was passed down by an orchestrator
            # (bridge-fix grants its roots through the environment, never as --add-dir)
            argv = ["bridge-panel", "run", "p", "Check it", "-p", "claude,cursor", "-r", "root-cause", "-d", str(ws),
                    "--add-dir", "../spec", "--kind", "doc-readiness"]
            earlier = root / "panel" / "p"            # an earlier run under this name, and the session card
            earlier.mkdir(parents=True)
            for f in ("cross.json", "report.md", "patch.md", "agreement.md"):
                (earlier / f).write_text("from before")
            with mock.patch.object(bp, "PANELS", root / "panel"), mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(bp, "hop_active", return_value=False), \
                    mock.patch.object(bp, "ask", side_effect=peer), mock.patch.object(bp, "ledger_row"), \
                    mock.patch.object(bp, "post"), mock.patch.object(bp, "close_panel_topics"), \
                    mock.patch.dict(os.environ, {core.ADD_DIRS_VAR: str(given)}), chdir(cwd), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(bp.main(), 4)                       # below two answering roles
            status = json.loads((root / "panel" / "p" / "status.json").read_text())
            left = sorted(f.name for f in earlier.iterdir())
        # synth after this run must not read the earlier run's cross-examination; the session card stays
        self.assertNotIn("cross.json", left)
        self.assertNotIn("patch.md", left)
        self.assertIn("agreement.md", left)
        roots = [str(given), str(spec)]
        self.assertEqual({k: status[k] for k in ("problem", "dir", "add_dir", "kind")},
                         {"problem": "Check it", "dir": str(ws), "add_dir": roots, "kind": "doc-readiness"})
        self.assertTrue(grants and all(g == roots for g in grants))    # what the leader and every role were granted


if __name__ == "__main__":
    unittest.main()
