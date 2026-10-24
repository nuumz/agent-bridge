"""Model-free evidence: citations resolve or they do not, an answer has the requested shape or it does not, and a
run's tool calls are counted from its transcript — the signals the scorecard is built from."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from bridgelib import evidence  # noqa: E402
from bridgelib.adapters import ADAPTERS, tool_calls  # noqa: E402


class Citations(unittest.TestCase):
    def test_verify_resolves_by_path_and_basename_and_scores_per_role(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "app").mkdir()
            (Path(d) / "app" / "cutoff.py").write_text("one\ntwo\n\nfour\n")
            rows = evidence.verify_citations(d, {"a": "see app/cutoff.py:2 and cutoff.py:4 and nope.py:1",
                                                 "b": "cutoff.py:2 is it; app/cutoff.py:3 blank; app/cutoff.py:9 far"})
            by = {r["cite"].split("  ")[0]: r for r in rows}
            self.assertEqual(by["app/cutoff.py:2"]["status"], "ok")
            self.assertEqual(by["app/cutoff.py:2"]["roles"], ["a", "b"])              # one line, one row
            self.assertEqual(by["app/cutoff.py:4"]["status"], "ok")
            self.assertEqual(by["nope.py:1"]["status"], "NO SUCH FILE")
            self.assertTrue(by["app/cutoff.py:3"]["status"].startswith("BLANK LINE"))
            self.assertTrue(by["app/cutoff.py:9"]["status"].startswith("OUT OF RANGE"))
            self.assertEqual(rows[-1]["status"], "ok")                                  # failures sort first
            self.assertEqual(evidence.cite_score(rows, "a"), (2, 3))
            self.assertEqual(evidence.cite_score(rows, "b"), (1, 3))
            self.assertEqual(evidence.low_evidence(rows, ["a", "b", "c"]), {"b": (1, 3)})
            rows2 = evidence.verify_citations(d, {"a": "app/cutoff.py:2", "a (cross)": "x.py:1 y.py:1 z.py:1"})
            self.assertEqual(evidence.cite_score(rows2, "a"), (1, 4))                     # cross-examination counts
            self.assertEqual(evidence.low_evidence(rows2, ["a"]), {"a": (1, 4)})
            (Path(d) / "node_modules" / "pkg").mkdir(parents=True)
            (Path(d) / "node_modules" / "pkg" / "cutoff.py").write_text("vendored\n")
            self.assertEqual(evidence.find_named(Path(d), "cutoff.py"), [Path(d) / "app" / "cutoff.py"])  # pruned
            self.assertIn("| `nope.py:1` | a | **NO SUCH FILE** |", evidence.citation_table(rows))
        self.assertEqual(evidence.citation_table([]), "")

    def test_a_role_with_one_citation_is_not_judged(self):
        rows = [{"cite": "x.py:1", "roles": ["a"], "status": "NO SUCH FILE", "text": ""}]
        self.assertEqual(evidence.low_evidence(rows, ["a"]), {})


class Shape(unittest.TestCase):
    TABLE = "| file:line | claim | evidence |\n|---|---|---|\n| a.py:1 | x | y |"

    def test_checkable_tokens(self):
        self.assertTrue(evidence.shape_met("table file:line · claim · evidence", self.TABLE))
        self.assertFalse(evidence.shape_met("table file:line · claim · evidence", "a.py:1 says x"))   # no table
        self.assertFalse(evidence.shape_met("table", "| only | one row |"))
        self.assertTrue(evidence.shape_met("json", "here:\n```json\n{}\n```"))
        self.assertFalse(evidence.shape_met("json", "{}"))
        self.assertTrue(evidence.shape_met("unified diff", "diff --git a/x b/x\n--- a/x\n+++ b/x"))
        self.assertTrue(evidence.shape_met("a patch", "--- a/x\n+++ b/x\n@@"))
        card = "| question | options | recommendation | if undecided |\n|---|---|---|---|\n| A or B? | A, B | A | A |"
        self.assertTrue(evidence.shape_met("decision", card))
        self.assertFalse(evidence.shape_met("decision", card.rsplit("\n", 1)[0]))      # the header echoed, no row
        self.assertFalse(evidence.shape_met("decision", "| q | o | r |\n|---|---|---|\n| a | b | c |"))
        self.assertTrue(evidence.shape_met("the verify command", "run `pytest -q tests`"))
        self.assertTrue(evidence.shape_met("command", "```bash\nmvn -o -q test\n```"))
        self.assertFalse(evidence.shape_met("command", "just run the tests"))
        self.assertTrue(evidence.shape_met("file:line per claim", "boardlib.py:12 does it"))

    def test_negation_alternatives_fences_and_echoes(self):
        self.assertIsNone(evidence.shape_met("a short paragraph, no table", "prose"))        # negated: not judged
        self.assertTrue(evidence.shape_met("json or a table", self.TABLE))                   # one alternative holds
        self.assertTrue(evidence.shape_met("json or a table", "```json\n{}\n```"))
        self.assertFalse(evidence.shape_met("json or a table", "neither"))
        self.assertTrue(evidence.shape_met("two tables", self.TABLE + "\n\n" + self.TABLE))  # plural counts
        self.assertFalse(evidence.shape_met("table", "```\n" + self.TABLE + "\n```"))        # quoted, not answered
        self.assertFalse(evidence.shape_met("table", "| file:line | claim | evidence |\n|---|---|---|\nnone found"))
        self.assertIsNone(evidence.shape_met("command output (last 10 lines)", "OK 12 tests"))  # output, not command
        self.assertEqual(evidence.shape_tokens("table, file:line or json"), [["table", "file:line"], ["json"]])

    def test_citation_and_command_tokens_ignore_ordinary_prose(self):
        for prose in ("api.example.com:443", "ratio 1.5:1", "at 10.30:45", "v1.2:3"):
            self.assertIsNone(evidence.CITE_RE.search(prose), prose)
        self.assertTrue(evidence.CITE_RE.search("see src/app/Main.java:12 and Makefile.cmake:3"))
        self.assertFalse(evidence.shape_met("command", "it costs $ 20 a month"))
        self.assertTrue(evidence.shape_met("command", "run `make test` first"))
        self.assertTrue(evidence.shape_met("command", "```console\n$ ls\n```"))

    def test_unknown_or_missing_shape_is_not_judged(self):
        self.assertIsNone(evidence.shape_met("a short paragraph", "anything"))
        self.assertIsNone(evidence.shape_met("", "anything"))
        self.assertIsNone(evidence.shape_met(None, "anything"))
        self.assertIsNone(evidence.shape_met("tables-of-contents", "x"))              # token inside a word: not it


class ToolCalls(unittest.TestCase):
    STREAM = "\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "looking"},
                                                      {"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "…"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Grep", "input": {}},
                                                      {"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "result", "result": "done", "session_id": "s1", "total_cost_usd": 0.1, "num_turns": 3,
         "usage": {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 5}},
    ]) + "\nnot json {tool_use}\n"

    def test_counts_tool_use_blocks_of_assistant_events(self):
        self.assertEqual(tool_calls(self.STREAM), 3)
        self.assertEqual(tool_calls('{"type":"result","result":"x"}'), 0)
        p = ADAPTERS["claude"].parse(self.STREAM)
        self.assertEqual((p.result, p.tok_in, p.tok_out), ("done", 10, 20))
        self.assertIn("tools=3", p.extra)
        self.assertIn("cost=0.1 turns=3 cache_write=5", p.extra)


if __name__ == "__main__":
    unittest.main()
