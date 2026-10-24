"""bridge-panel report head — who really answered is one line a reader checks before trusting a synthesis."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"


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


if __name__ == "__main__":
    unittest.main()
