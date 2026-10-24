"""docs/PROTOCOL.md, docs/protocol/envelope.schema.json and docs/protocol/vectors.json are held to the board and to each
other: the schema's enumerations are the board's, the vectors pass or fail as labelled, every kind the board accepts
exports as a conformant envelope (`bridge-board read --json` included), and the spec names every kind and header key."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import boardlib  # noqa: E402
from bridgelib import ledger, protocol  # noqa: E402

USER, PRIMARY, CURSOR, COPILOT = "user:tester", "claude@primary", "cursor#41", "copilot#42"
SHAPED = "| file:line | claim | evidence |\n|---|---|---|\n| bin/boardlib.py:1 | one caller | read it |"


class Spec(unittest.TestCase):
    def setUp(self):
        self.schema = protocol.load_schema()
        self.vectors = json.loads(protocol.VECTORS_PATH.read_text(encoding="utf-8"))

    def test_schema_stays_in_the_validators_subset(self):
        # a keyword the validator does not implement would pass silently — so it raises instead, and this test would fail
        protocol.validate({"v": 1, "topic": "t", "seq": 1, "author": "user:a", "kind": "note", "body": ""}, self.schema)
        with self.assertRaises(ValueError):
            protocol.validate({}, {"type": "object", "patternProperties": {}})

    def test_schema_enumerations_are_the_boards(self):
        props = self.schema["properties"]
        self.assertEqual(props["kind"]["enum"], list(boardlib.KINDS))
        self.assertEqual(props["headers"]["properties"]["mode"]["enum"], list(boardlib.TASK_MODES))
        self.assertEqual(props["headers"]["properties"]["card"]["enum"], ["decision", "fact"])
        self.assertEqual(protocol.NO_VALUE, boardlib.NO_VALUE)
        self.assertEqual(self.schema["properties"]["v"]["const"], protocol.VERSION)
        ident = re.compile(self.schema["$defs"]["identity"]["pattern"])
        for name in ("user:alice", "claude@primary", "cursor#41", "copilot:task3", "claude@live:critic", "claude@hop",
                     "board", "bridge-fix", "user:", "@leader", "*", "a b", "a+b", "", "ユーザー:x"):
            with self.subTest(name):
                try:
                    boardlib.identity(name)
                    board_ok = True
                except boardlib.BoardError:
                    board_ok = False
                self.assertEqual(bool(ident.search(name)), board_ok)
        role = re.compile(self.schema["$defs"]["role_ref"]["pattern"])
        for name in ("leader", "root-cause", "Leader", "a_b", "9x", ""):
            with self.subTest(name):
                self.assertEqual(bool(role.search("@" + name)), bool(boardlib.ROLE_RE.fullmatch(name)))

    def test_vectors(self):
        for case in self.vectors["valid"]:
            with self.subTest(valid=case["name"]):
                self.assertEqual(protocol.check(case["envelope"], self.schema), [])
        for case in self.vectors["invalid"]:
            with self.subTest(invalid=case["name"]):
                self.assertTrue(protocol.check(case["envelope"], self.schema), case["why"])
        kinds = {c["envelope"]["kind"] for c in self.vectors["valid"]}
        self.assertEqual(kinds, set(boardlib.KINDS), "every kind has a valid vector")

    def test_spec_names_every_kind_header_key_and_artifact(self):
        text = (ROOT / "docs" / "PROTOCOL.md").read_text(encoding="utf-8")
        for kind in boardlib.KINDS:
            self.assertIn(f"`{kind}`", text, kind)
        for key in self.schema["properties"]["headers"]["properties"]:
            self.assertIn(f"`{key}`", text, key)
        for ref in ("protocol/envelope.schema.json", "protocol/vectors.json", "bridge-board read <topic> --json",
                    "tests/test_protocol.py"):
            self.assertIn(ref, text, ref)
        self.assertEqual(self.vectors["valid"][0]["envelope"]["v"], protocol.VERSION)

    def test_header_and_citations_follow_the_board_and_the_evidence_module(self):
        for body in ("shape: table\nowner: x\n\nshape: not this", "Summary: all good", "| a | b |\n|---|---|", "",
                     "key with space: v\nshape: t", "Shape: UPPER\nmissing:  padded  "):
            with self.subTest(body=body):
                self.assertEqual(protocol.header(body), boardlib.header(body))
        cites = protocol.citations("see ./src/a.py:10, src/a.py:10 again, `b/c.ts:3-5` and host:443 and v1.2:3")
        self.assertEqual(cites, [{"path": "src/a.py", "line": 10}, {"path": "b/c.ts", "line": 3, "end_line": 5}])
        env = protocol.envelope("t", {"seq": 1, "author": "user:a", "kind": "note", "body": "shape: x\n\nsrc/a.py:1",
                                      "reply_to": None, "created_at": 5, "to": None})
        self.assertEqual(list(env), list(protocol.FIELDS))
        self.assertEqual(protocol.check(env, self.schema), [])


class Board(unittest.TestCase):
    """Every kind the board accepts, on a fresh board under a temporary HOME, is a conformant envelope."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.board_dir = self.home / ".agent-bridge" / "state" / "board"
        self.patches = [mock.patch.object(boardlib, "BOARD_DIR", self.board_dir),
                        mock.patch.object(boardlib, "DB_PATH", self.board_dir / "board.db"),
                        mock.patch.object(ledger, "LEDGER", self.board_dir / "ledger.jsonl"),
                        mock.patch.dict(os.environ, {"HOME": str(self.home), "USER": "tester"})]
        for p in self.patches:
            p.start()
        for var in ("AGENT_BRIDGE_HOP", "AGENT_BRIDGE_IDENTITY", "AGENT_BRIDGE_AGENT", "AGENT_BRIDGE_TASK"):
            os.environ.pop(var, None)
        self.conn = boardlib.connect()
        self.schema = protocol.load_schema()

    def tearDown(self):
        self.conn.close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def fill(self) -> tuple[str, int]:
        """One topic that carries every kind: a card, a request answered in shape, a blocked, a decision, a turn (its
        task log row), a handoff, a done summary; plus a dm reopened for its system row."""
        name = "room:all-kinds"
        tid, _ = boardlib.open_topic(self.conn, name, USER, False)
        boardlib.member_add(self.conn, tid, CURSOR, "root-cause")
        post = lambda author, body, kind="note", reply_to=None, to=None: boardlib.post(  # noqa: E731
            self.conn, tid, author, body, kind, reply_to, to)
        post(USER, "Let us settle the retry policy.")
        post(CURSOR, "Who owns the ledger?", "question")
        req = post(PRIMARY, "shape: table file:line · claim · evidence\n\nList every caller of post().", "request", to=CURSOR)
        post(CURSOR, SHAPED, "answer", req, PRIMARY)
        req2 = post(PRIMARY, "shape: json\n\nWhich call sets the deadline?", "request", to="@root-cause")
        post(CURSOR, "missing: the spec root is not granted; default: answer from this repo after one wait", "blocked",
             req2, PRIMARY)
        card = post(COPILOT, boardlib.card("decision", [("cap retries at 3 or 5?", "3 / 5", "3", "3 at 17:00")]),
                    "request", to=USER)
        post(USER, "3", "decision", card)
        post(USER, "shape: file:line findings\nmode: ask\n\nReview src/payment/ for retry bugs.", "request", to="cursor")
        post(CURSOR, "The mapper tests are yours. I keep the parser.", "handoff", to=COPILOT)
        post(PRIMARY, "Retry cap is 3 (seq 8). Evidence bin/boardlib.py:1-2. Nothing open.", "done", to="*")
        dm = boardlib.dm_name(PRIMARY, USER)
        boardlib.open_topic(self.conn, dm, USER, False)
        boardlib.close_topic(self.conn, dm)
        boardlib.open_topic(self.conn, dm, USER, False)              # posts the `reopened by` system row
        return name, tid

    def rows(self, tid: int) -> list[dict]:
        msgs, _ = boardlib.read(self.conn, tid, USER, 0, 200)
        return msgs

    def test_every_kind_the_board_accepts_is_a_conformant_envelope(self):
        name, tid = self.fill()
        envs = [protocol.envelope(name, m) for m in self.rows(tid)]
        dm_tid = boardlib.topic_id(self.conn, boardlib.dm_name(PRIMARY, USER), None)
        envs += [protocol.envelope(boardlib.dm_name(PRIMARY, USER), m) for m in self.rows(dm_tid)]
        for env in envs:
            with self.subTest(seq=env["seq"], kind=env["kind"]):
                self.assertEqual(protocol.check(env, self.schema), [], env)
        self.assertEqual({e["kind"] for e in envs}, set(boardlib.KINDS))
        self.assertEqual([e["seq"] for e in envs[:-1]], list(range(1, len(envs))), "seq is 1-based and dense")
        by_kind = {e["kind"]: e for e in envs}                   # the last of each kind
        first_request = next(e for e in envs if e["kind"] == "request")
        self.assertEqual(by_kind["task"]["author"], "board")
        self.assertEqual(first_request["headers"]["shape"], "table file:line · claim · evidence")
        self.assertEqual(by_kind["request"]["to"], "cursor", "the turn is the last request")
        self.assertEqual(by_kind["request"]["headers"], {"shape": "file:line findings", "mode": "ask"})
        self.assertEqual(by_kind["answer"]["citations"], [{"path": "bin/boardlib.py", "line": 1}])
        self.assertEqual(by_kind["done"]["citations"], [{"path": "bin/boardlib.py", "line": 1, "end_line": 2}])
        self.assertEqual(by_kind["system"]["body"], f"reopened by {USER}")

    def test_the_board_refuses_what_the_vectors_call_invalid(self):
        """The transport, not the prompt, enforces §6.1 and §5.1: the same refusals the vectors list."""
        tid, _ = boardlib.open_topic(self.conn, "room:refusals", USER, False)
        for name, args in (("request without to", (PRIMARY, "shape: table\n\nx", "request", None, None)),
                           ("request without shape", (PRIMARY, "please", "request", None, CURSOR)),
                           ("blocked without missing", (CURSOR, "stuck", "blocked", None, PRIMARY)),
                           ("blocked with n/a", (CURSOR, "missing: n/a\nstuck", "blocked", None, PRIMARY)),
                           ("decision by an agent", (PRIMARY, "3", "decision", None, None)),
                           ("unknown kind", (PRIMARY, "x", "reply", None, None)),
                           ("upper-case role", (PRIMARY, "x", "note", None, "@Leader"))):
            with self.subTest(name), self.assertRaises(boardlib.BoardError):
                boardlib.post(self.conn, tid, *args)

    def test_read_json_prints_one_conformant_envelope_per_line(self):
        name, tid = self.fill()
        self.rows(tid)                                     # the user's cursor is at the end; --after 0 reads all
        run = lambda *a: subprocess.run([sys.executable, str(BIN / "bridge-board"), "read", name, *a],  # noqa: E731
                                        capture_output=True, text=True, env=dict(os.environ))
        out = run("--json", "--after", "0", "-n", "200")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
        envs = [json.loads(ln) for ln in lines]
        self.assertEqual(len(envs), len(self.rows(tid)))
        for env in envs:
            with self.subTest(seq=env["seq"]):
                self.assertEqual(protocol.check(env, self.schema), [], env)
                self.assertEqual(env["topic"], name)
        self.assertNotIn("next_after_seq", out.stdout, "JSON mode prints envelopes only")
        plain = run("--after", "0")
        self.assertIn("-- next_after_seq=", plain.stdout)
        self.assertIn("[seq=1 author=user:tester kind=note]", plain.stdout)


if __name__ == "__main__":
    unittest.main()
