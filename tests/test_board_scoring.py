"""A reply to a request is held to the request's shape, and the misses and blocks become ledger rows."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import boardlib  # noqa: E402
from bridgelib import ledger  # noqa: E402

USER, CURSOR, COPILOT = "user:tester", "cursor#41", "copilot#42"
REQUEST = "shape: table file:line · claim · evidence\n\nList every caller of post()."
TABLE = "| file:line | claim | evidence |\n|---|---|---|\n| bin/boardlib.py:1 | one caller | read it |"


class Scoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        board_dir = home / ".agent-bridge" / "state" / "board"
        self.patches = [mock.patch.object(boardlib, "BOARD_DIR", board_dir),
                        mock.patch.object(boardlib, "DB_PATH", board_dir / "board.db"),
                        mock.patch.object(ledger, "LEDGER", home / "ledger.jsonl"),
                        mock.patch.dict(os.environ, {"HOME": str(home), "USER": "tester"})]
        for p in self.patches:
            p.start()
        self.conn = boardlib.connect()
        self.tid = boardlib.topic_id(self.conn, "room:r", USER)

    def tearDown(self):
        self.conn.close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_a_reply_in_the_wrong_shape_is_refused_and_logged(self):
        r = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to=CURSOR)
        with self.assertRaisesRegex(boardlib.BoardError, "incomplete: the request asks for `table file:line"):
            boardlib.post(self.conn, self.tid, CURSOR, "post() is called from three places, trust me", "done", reply_to=r)
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, CURSOR), 1)        # still open
        rows = ledger.rows()
        self.assertEqual([(x["type"], x["peer"], x["topic"], x["request_seq"], x["requested_by"]) for x in rows],
                         [("shape_miss", CURSOR, "room:r", r, USER)])
        self.assertEqual(rows[0]["shape"], "table file:line · claim · evidence")
        boardlib.post(self.conn, self.tid, CURSOR, TABLE, "done", reply_to=r)              # the shape: accepted
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, CURSOR), 0)
        self.assertEqual(len(ledger.rows()), 1)

    def test_blocked_is_logged_with_what_is_missing(self):
        r = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to=CURSOR)
        boardlib.post(self.conn, self.tid, CURSOR, "missing: read access to bin/\nwill assume none", "blocked", reply_to=r)
        rows = ledger.rows()
        self.assertEqual((rows[0]["type"], rows[0]["missing"]), ("blocked", "read access to bin/"))

    def test_who_is_not_scored(self):
        r = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to=CURSOR)
        boardlib.post(self.conn, self.tid, USER, "never mind", "done", reply_to=r)         # the requester withdraws
        r2 = boardlib.post(self.conn, self.tid, CURSOR, REQUEST, "request", to=USER)
        boardlib.post(self.conn, self.tid, USER, "three", "answer", reply_to=r2)           # a human is never scored
        card = boardlib.post(self.conn, self.tid, CURSOR, boardlib.card("decision", [("A or B?", "A|B", "A", "A")]),
                             "request", to=USER)
        boardlib.post(self.conn, self.tid, USER, "B", "decision", reply_to=card)           # a card: free text
        loose = boardlib.post(self.conn, self.tid, USER, "shape: a short paragraph\n\nwhy?", "request", to=CURSOR)
        boardlib.post(self.conn, self.tid, CURSOR, "because", "answer", reply_to=loose)    # nothing a script can check
        self.assertEqual(ledger.rows(), [])
        self.assertEqual(boardlib.user_cards(self.conn), [])

    def test_closed_requests_sibling_runs_and_retries(self):
        r = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to=CURSOR)
        for _ in range(3):                                                                  # the same wrong body thrice
            with self.assertRaises(boardlib.BoardError):
                boardlib.post(self.conn, self.tid, CURSOR, "trust me", "done", reply_to=r)
        self.assertEqual(len(ledger.rows()), 1)                                             # one miss, not three
        with self.assertRaises(boardlib.BoardError):
            boardlib.post(self.conn, self.tid, "cursor#99", "trust me more", "done", reply_to=r)  # a new body
        self.assertEqual(len(ledger.rows()), 2)
        boardlib.post(self.conn, self.tid, CURSOR, TABLE, "done", reply_to=r)                # closes it
        boardlib.post(self.conn, self.tid, CURSOR, "posted above", "done", reply_to=r)       # closed: not judged
        r2 = boardlib.post(self.conn, self.tid, CURSOR, REQUEST, "request", to=COPILOT)
        boardlib.post(self.conn, self.tid, "cursor#77", "never mind", "done", reply_to=r2)   # a later run withdraws
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, COPILOT), 0)
        r3 = boardlib.post(self.conn, self.tid, "claude@primary", REQUEST, "request", to="claude@live:p")
        with self.assertRaises(boardlib.BoardError):                                        # same family, not a run
            boardlib.post(self.conn, self.tid, "claude@live:p", "prose", "done", reply_to=r3)
        self.assertEqual(len(ledger.rows()), 3)

    def test_the_mcp_server_refuses_the_same_way(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("bridge_board_mod", str(BIN / "bridge-board"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        board = importlib.util.module_from_spec(spec)
        loader.exec_module(board)
        r = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to=CURSOR)
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_IDENTITY": CURSOR, "AGENT_BRIDGE_HOP": "1"}):
            srv = board.Server()
            self.addCleanup(srv.conn.close)
        out = srv.call("board_post", {"topic": "room:r", "body": "done, trust me", "kind": "done", "reply_to": r})
        self.assertTrue(out["isError"])
        self.assertIn("refused: incomplete", out["content"][0]["text"])
        self.assertEqual(ledger.rows()[0]["type"], "shape_miss")


if __name__ == "__main__":
    unittest.main()
