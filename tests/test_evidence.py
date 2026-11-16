"""Model-free evidence: citations resolve or they do not, an answer has the requested shape or it does not, and a
run's tool calls are counted from its transcript — the signals the scorecard is built from."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from bridgelib import core, evidence  # noqa: E402
from bridgelib.adapters import ADAPTERS, tool_calls  # noqa: E402


@contextlib.contextmanager
def home_at(home: Path):
    """HOME for both of its readers: `~/` expands from the environment, the grant rule reads `core.HOME` (set at
    import)."""
    with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(core, "HOME", Path(home)):
        yield


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

    def test_absolute_nested_checkout_and_named_roots_resolve(self):
        with tempfile.TemporaryDirectory() as tmp, home_at(Path(tmp)):
            d, other = Path(tmp) / "ws", Path(tmp) / "other"
            (d / "app").mkdir(parents=True)
            (d / "app" / "cutoff.py").write_text("one\ntwo\n")
            wt = d / ".claude" / "worktrees" / "bridge-x"            # a bridge-work worktree: a nested checkout
            (wt / "app").mkdir(parents=True)
            (wt / ".git").write_text("gitdir: elsewhere\n")
            (wt / "app" / "cutoff.py").write_text("one\ntwo\n")
            (wt / "app" / "only_here.py").write_text("x\n")
            (other / ".git").mkdir(parents=True)
            (other / "src").mkdir()
            (other / "src" / "TaskClient.java").write_text("a\nb\n")
            (other / "src" / "Brief.java").write_text("c\n")
            (d / "nested_twice.py").write_text("ws\n")
            (other / "src" / "nested_twice.py").write_text("other\n")

            self.assertEqual(evidence.find_named(d, "cutoff.py"), [d / "app" / "cutoff.py"])
            self.assertEqual(evidence.find_named(d, "only_here.py"), [wt / "app" / "only_here.py"])
            brief = f"Review the client in {other}/src/Brief.java and the tablet repo."
            roots = evidence.named_roots(brief, [], str(d))
            self.assertIn(other.resolve(), roots)                       # resolved: one place, one root
            text = (f"{d}/app/cutoff.py:2 and cutoff.py:2 and TaskClient.java:2 and {other}/src/Brief.java:1 "
                    "and nested_twice.py:1")
            by = {r["cite"].split("  ")[0]: r for r in evidence.verify_citations(str(d), {"a": text}, roots)}
            self.assertEqual(by["app/cutoff.py:2"]["status"], "ok")                   # absolute + short: one row
            self.assertEqual(by[f"{other.resolve()}/src/TaskClient.java:2"]["status"], "ok")   # in a named checkout
            self.assertEqual(by[f"{other.resolve()}/src/Brief.java:1"]["status"], "ok")
            self.assertEqual(by["nested_twice.py:1"]["text"], "ws")                   # the workspace wins
            self.assertEqual(evidence.verify_citations(str(d), {"a": "TaskClient.java:2"})[0]["status"],
                             "NO SUCH FILE")                                           # no brief, no extra roots
            self.assertEqual(evidence.named_roots("", ["../other"], str(d)), [other.resolve()])
            (Path(tmp) / ".git").mkdir()                                    # HOME itself as a checkout: no root
            (Path(tmp) / "loose.md").write_text("x\n")
            self.assertEqual(evidence.named_roots(f"see {tmp}/loose.md", [], str(d)),
                             [Path(tmp).resolve() / "loose.md"])

    def test_a_path_is_read_only_inside_the_roots(self):
        # a resolved line goes into the other roles' prompts: a citation must not carry a file nobody granted
        with tempfile.TemporaryDirectory() as tmp:
            ws, outside, home = Path(tmp) / "ws", Path(tmp) / "outside", Path(tmp) / "home"
            for f, text in ((ws / "app" / "x.py", "app\n"), (ws / "lib" / "x.py", "lib\n"),
                            (outside / "secret.txt", "token\n"), (home / ".config" / "gh" / "hosts.yml", "token\n")):
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text)
            (ws / "linked").symlink_to(outside)
            (Path(tmp) / "beside.txt").write_text("token\n")             # where `linked/../beside.txt` really lands
            text = (f"{outside}/secret.txt:1 ~/.config/gh/hosts.yml:1 ../outside/secret.txt:1 linked/../beside.txt:1 "
                    f"{ws}/linked/../beside.txt:1 /app/x.py:1")
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                by = {r["cite"].split("  ")[0]: r for r in evidence.verify_citations(str(ws), {"a": text})}
                linked = evidence.verify_citations(str(ws), {"a": f"{ws}/linked/secret.txt:1"})
        # each exists, so no "NO SUCH FILE"; a `..` path counts where it lands, links followed, not where it is spelled
        for cite in (f"{outside}/secret.txt:1", "~/.config/gh/hosts.yml:1", "../outside/secret.txt:1",
                     "linked/../beside.txt:1", f"{ws}/linked/../beside.txt:1"):
            self.assertEqual((by[cite]["status"], by[cite]["text"]),
                             ("OUTSIDE THE ROOTS (not read; --add-dir grants it)", ""), cite)
        self.assertEqual(by["app/x.py:1"]["text"], "app")             # a container's /app/x.py reads workspace-relative
        self.assertEqual(linked[0]["status"], "ok")                   # a link the workspace holds is part of it

    def test_named_roots_are_resolved_and_bounded_by_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            home, far, link = base / "home", base / "far", base / "link"
            repo, ws = home / "repo", home / "ws"
            for f in (repo / "src" / "A.java", repo / "src" / "B.java", far / "src" / "C.java", home / "deep" / "D.py"):
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("x\n")
            for d in (repo / ".git", far / ".git", ws):
                d.mkdir()
            link.symlink_to(repo)                                       # one checkout, two spellings
            with home_at(home):
                roots = evidence.named_roots(f"see {link}/src/A.java and {far}/src/C.java", [str(repo)], str(ws))
                # `home` as an extra root: what `--add-dir ~` hands the resolver
                rows = evidence.verify_citations(str(ws), {"a": "B.java:1 D.py:1 ~/deep/D.py:1"}, [*roots, home])
        self.assertEqual(roots, [repo, repo / "src" / "A.java", far / "src" / "C.java"])   # no checkout outside HOME
        by = {r["cite"].split("  ")[0]: r["status"] for r in rows}
        self.assertEqual(by[f"{repo}/src/B.java:1"], "ok")                  # not AMBIGUOUS
        self.assertEqual(by["D.py:1"], "NO SUCH FILE")              # a root at HOME: read by path, never walked
        self.assertEqual(by[f"{home}/deep/D.py:1"], "ok")

    def test_a_climbing_path_reads_where_it_lands(self):
        # `..` was looked up by basename alone: `../sib/README.md` read the workspace's own README.md
        with tempfile.TemporaryDirectory() as tmp:
            ws, sib = Path(tmp) / "ws", Path(tmp) / "sib"
            for f, text in ((ws / "README.md", "ws\n"), (sib / "README.md", "sib\n"), (ws / "app" / "x.py", "app\n"),
                            (ws / "lib" / "x.py", "lib\n")):
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text)
            roots = evidence.named_roots("", ["../sib"], str(ws))
            readme, _ = evidence.resolve_cited(ws, "../sib/README.md", roots)
            self.assertEqual(readme.read_text(), "sib\n")
            self.assertEqual(evidence.resolve_cited(ws, "app/../lib/x.py", roots), (ws / "app/../lib/x.py", "lib/x.py"))
            self.assertEqual(evidence.resolve_cited(ws, "../sib/gone.md", roots), (None, "NO SUCH FILE"))

    def test_named_roots_are_what_the_run_is_granted(self):
        # roots come from `core.cited_roots`, the run's own grant: a brief naming a bridge artifact made the bridge
        # checkout a root, and a basename walk there read state/runs — every other run's prompt — into the table
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            ws, state = home / "ws", home / ".agent-bridge" / "state"
            for f in (state / "panel" / "p1" / "report.md", state / "runs" / "20261116-1-claude.prompt.txt",
                      ws / "a.py"):
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("brief\n")
            (state.parent / ".git").mkdir()                              # the bridge is a checkout under HOME
            with home_at(home), mock.patch.object(core, "STATE", state):
                roots = evidence.named_roots(f"see {state}/panel/p1/report.md", [], str(ws))
                rows = evidence.verify_citations(str(ws), {"a": "20261116-1-claude.prompt.txt:1"}, roots)
        self.assertEqual(roots, [state / "panel" / "p1" / "report.md", state / "panel" / "p1"])
        self.assertEqual((rows[0]["status"], rows[0]["text"]), ("NO SUCH FILE", ""))

    def test_a_tilde_word_names_no_root(self):
        # `~10/20/30 ms` is no one's home directory: expanduser() raised on it and took the run's ledger row along
        self.assertEqual(evidence.named_roots("latency ~10/20/30 ms, see ~build/logs/x"), [])

    def test_a_path_the_resolver_cannot_stat_reads_unreadable(self):
        # before Python 3.14 is_file() raises on EACCES/EPERM (/var/root, another user's home, a TCC-guarded folder)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(Path, "is_file", side_effect=PermissionError(13, "Permission denied")):
            rows = evidence.verify_citations(d, {"a": "/var/root/x.sh:1 and app/x.py:2"})
        self.assertEqual([r["status"] for r in rows], ["UNREADABLE", "UNREADABLE"])

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
