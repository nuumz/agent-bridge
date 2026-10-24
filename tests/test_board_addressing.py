"""Addressed messages, topic members, dm/room topics and the request kinds (P1 of the teamwork plan): a message says
whom it is for, a wait with `mention` sleeps through other people's traffic, a request stays open until a reply
closes it, and who may open / join / close a topic follows its members."""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import boardlib  # noqa: E402
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402

USER, PRIMARY, CURSOR, COPILOT = "user:tester", "claude@primary", "cursor#41", "copilot#42"
REQUEST = "owner: cursor#41\nshape: table file:line · claim · evidence\n\nList every caller of post()."
DONE = "| file:line | claim | evidence |\n|---|---|---|\n| bin/boardlib.py:1 | one caller | read it |"   # in shape


def _load(name: str):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_") + "_mod", str(BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Board(unittest.TestCase):
    """Every test runs against a fresh board under a temporary HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.board_dir = Path(self.tmp.name) / ".agent-bridge" / "state" / "board"
        self.patches = [mock.patch.object(boardlib, "BOARD_DIR", self.board_dir),
                        mock.patch.object(boardlib, "DB_PATH", self.board_dir / "board.db"),
                        mock.patch.dict(os.environ, {"HOME": self.tmp.name, "USER": "tester"})]
        for p in self.patches:
            p.start()
        for var in ("AGENT_BRIDGE_HOP", "AGENT_BRIDGE_IDENTITY", "AGENT_BRIDGE_AGENT", "AGENT_BRIDGE_TASK"):
            os.environ.pop(var, None)
        self.conn = boardlib.connect()

    def tearDown(self):
        self.conn.close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def room(self, name: str = "room:r", *agents: str) -> int:
        tid, _ = boardlib.open_topic(self.conn, name, USER, False)
        for a in agents:
            boardlib.member_add(self.conn, tid, a)
        return tid

    def cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(BIN / "bridge-board"), *args], capture_output=True, text=True,
                              env={**os.environ, **(env or {})})


class Migration(Board):
    def test_a_board_from_before_the_column_is_upgraded_in_place(self):
        self.conn.close()
        for f in self.board_dir.glob("board.db*"):          # setUp made a current board; replace it with an old one
            f.unlink()
        old = sqlite3.connect(self.board_dir / "board.db")
        legacy = re.sub(r"CREATE TABLE IF NOT EXISTS members .*?;\n", "", boardlib.SCHEMA.replace(" addressee TEXT,", ""),
                        flags=re.S)
        self.assertNotIn("members", legacy)
        old.executescript(legacy)
        old.execute("INSERT INTO topics(name, created_by, created_at) VALUES('conv:old', 'user:tester', 1)")
        old.execute("INSERT INTO messages(topic_id, seq, author, kind, body, created_at) "
                    "VALUES(1, 1, 'user:tester', 'note', 'hi', 1)")
        old.commit()
        self.assertNotIn("addressee", {r[1] for r in old.execute("PRAGMA table_info(messages)")})
        old.close()
        self.conn = boardlib.connect()
        self.assertIn("addressee", {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")})
        msgs, _ = boardlib.read(self.conn, 1, CURSOR, 0)
        self.assertEqual([(m["body"], m["to"]) for m in msgs], [("hi", None)])        # legacy rows read as broadcast
        self.assertTrue(boardlib.others_after(self.conn, 1, CURSOR, 0, mention=True))  # and still wake a mention wait
        st = boardlib.status(self.conn, CURSOR)
        self.assertEqual([t["name"] for t in st["topics"]], ["conv:old"])             # no members = visible to all


class Posting(Board):
    def test_to_is_stored_and_validated(self):
        tid = self.room("room:r", CURSOR)
        boardlib.post(self.conn, tid, USER, "all", to="*")
        boardlib.post(self.conn, tid, USER, "you", to=CURSOR)
        boardlib.post(self.conn, tid, USER, "lead", to="@leader")
        boardlib.post(self.conn, tid, USER, "none", to="")
        rows = [r[0] for r in self.conn.execute("SELECT addressee FROM messages ORDER BY seq")]
        self.assertEqual(rows, ["*", CURSOR, "@leader", None])
        for bad in ("@Boss", "@x y", "two words"):
            with self.assertRaises(boardlib.BoardError):
                boardlib.post(self.conn, tid, USER, "x", to=bad)

    def test_a_request_needs_an_owner_and_a_shape(self):
        tid = self.room("room:r", CURSOR)
        with self.assertRaisesRegex(boardlib.BoardError, "needs to="):
            boardlib.post(self.conn, tid, USER, REQUEST, "request")
        with self.assertRaisesRegex(boardlib.BoardError, "shape"):
            boardlib.post(self.conn, tid, USER, "owner: cursor#41\nplease list callers", "request", to=CURSOR)
        self.assertEqual(boardlib.post(self.conn, tid, USER, REQUEST, "request", to=CURSOR), 1)
        self.assertEqual(boardlib.header(REQUEST),
                         {"owner": "cursor#41", "shape": "table file:line · claim · evidence"})
        self.assertEqual(boardlib.header("just prose: with a colon\nshape: x"), {})     # a header key has no spaces
        self.assertEqual(boardlib.header("owner: a\ndue: 900\nprio: 1\nshape: d\n\nshape: not this")["shape"], "d")
        with self.assertRaisesRegex(boardlib.BoardError, "reserved for the user"):
            boardlib.post(self.conn, tid, CURSOR, "go", "decision")
        with self.assertRaisesRegex(boardlib.BoardError, "reply_to=99"):
            boardlib.post(self.conn, tid, CURSOR, "answer to nothing", "answer", reply_to=99)

    def test_identities_are_checked_wherever_they_enter(self):
        for bad in ("*x", "\tcursor", "cursor#41\n", "@X", "@", "a+b", "user:", "two words"):
            with self.subTest(bad), self.assertRaises(boardlib.BoardError):
                boardlib.addressee(bad)
        for ok in ("claude@primary", "cursor#41", "claude:task3", "user:nuumz", "claude-A", "*", "@leader"):
            self.assertEqual(boardlib.addressee(ok), ok)
        tid = self.room("room:r")
        with self.assertRaises(boardlib.BoardError):
            boardlib.join(self.conn, tid, USER, "not an identity")


class Waiting(Board):
    def test_mention_wait_sleeps_through_traffic_for_others(self):
        tid = self.room("room:r", CURSOR, COPILOT)
        boardlib.post(self.conn, tid, USER, "copilot only", to=COPILOT)
        boardlib.post(self.conn, tid, COPILOT, "talking to the user", to=USER)
        self.assertTrue(boardlib.others_after(self.conn, tid, CURSOR, 0))                   # plain wait: anything
        self.assertFalse(boardlib.others_after(self.conn, tid, CURSOR, 0, mention=True))    # mention: nothing for me
        boardlib.post(self.conn, tid, USER, "cursor, go", to=CURSOR)
        self.assertTrue(boardlib.others_after(self.conn, tid, CURSOR, 0, mention=True))

    def test_task_log_rows_are_bookkeeping_not_mail(self):
        tid = self.room("room:r", CURSOR)
        boardlib.task_add(self.conn, tid, "t", "", USER, False)                           # writes a kind=task row
        self.assertTrue(boardlib.others_after(self.conn, tid, CURSOR, 0))
        self.assertFalse(boardlib.others_after(self.conn, tid, CURSOR, 0, mention=True))
        self.assertEqual(boardlib.read(self.conn, tid, CURSOR, 0, mine=True)[0], [])

    def test_mention_wait_wakes_on_role_everyone_and_replies(self):
        tid = self.room("room:r")
        boardlib.member_add(self.conn, tid, CURSOR, "observer")
        mine = boardlib.post(self.conn, tid, CURSOR, "a question", "question", to=USER)
        cases = {"role": dict(to="@observer"), "everyone": dict(to="*"), "broadcast": {},
                 "reply": dict(reply_to=mine, to=COPILOT)}          # a reply to me reaches me whoever it names
        for label, kw in cases.items():
            with self.subTest(label):
                seq = boardlib.post(self.conn, tid, USER, label, **kw)
                roles = boardlib.my_roles(self.conn, tid, CURSOR)
                self.assertEqual(roles, ["@observer"])
                self.assertTrue(boardlib.others_after(self.conn, tid, CURSOR, seq - 1, mention=True, roles=roles))
                self.assertFalse(boardlib.others_after(self.conn, tid, CURSOR, seq, mention=True, roles=roles))

    def test_a_refused_wait_does_not_spend_the_budget(self):
        board = _load("bridge-board")
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_AGENT": "claude"}):
            srv = board.Server()
            self.addCleanup(srv.conn.close)
        out = srv.call("board_wait", {"topic": "no-such-topic", "timeout_s": 1})
        self.assertTrue(out["isError"], out)
        self.assertEqual(srv.waits, 0)
        tid = self.room("room:r", PRIMARY)
        boardlib.post(self.conn, tid, USER, "for copilot", to=COPILOT)
        out = srv.call("board_wait", {"topic": "room:r", "timeout_s": 1, "mention": True})
        self.assertIn("timed_out", out["content"][0]["text"])
        self.assertEqual(srv.waits, 1)
        out = srv.call("board_wait", {"topic": "room:r", "timeout_s": 1})              # primary default: any post wakes
        self.assertIn("for copilot", out["content"][0]["text"])
        self.assertIn("to=copilot#42", out["content"][0]["text"])

    def test_a_wait_after_a_mine_read_does_not_replay_what_was_read(self):
        board = _load("bridge-board")
        tid = self.room("room:r", CURSOR, COPILOT)
        r1 = boardlib.post(self.conn, tid, USER, REQUEST, "request", to=CURSOR)
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_IDENTITY": CURSOR, "AGENT_BRIDGE_HOP": "1"}):
            peer = board.Server()
            self.addCleanup(peer.conn.close)
        self.assertIn("kind=request", peer.call("board_read", {"topic": "room:r", "mine": True})["content"][0]["text"])
        peer.call("board_post", {"topic": "room:r", "body": DONE, "kind": "done", "reply_to": r1})
        out = peer.call("board_wait", {"topic": "room:r", "timeout_s": 1})
        self.assertIn("timed_out", out["content"][0]["text"], out)                      # nothing new: no replay of r1
        for _ in range(3):
            boardlib.post(self.conn, tid, USER, "copilot chatter", to=COPILOT)
        wake = boardlib.post(self.conn, tid, USER, "cursor: next", to=CURSOR)
        text = peer.call("board_wait", {"topic": "room:r", "timeout_s": 1})["content"][0]["text"]
        self.assertIn("cursor: next", text)
        self.assertNotIn("copilot chatter", text)  # the wake reads what reaches me
        self.assertEqual(self.conn.execute("SELECT seq FROM cursors WHERE agent=?", (CURSOR,)).fetchone()[0], wake)


class Reading(Board):
    def test_mine_and_open_requests_filter_without_moving_the_cursor(self):
        tid = self.room("room:r", CURSOR, COPILOT)
        r1 = boardlib.post(self.conn, tid, USER, REQUEST, "request", to=CURSOR)
        boardlib.post(self.conn, tid, USER, "copilot's", to=COPILOT)
        r3 = boardlib.post(self.conn, tid, USER, REQUEST, "request", to="*")
        # to=* request: anyone closes
        r4 = boardlib.post(self.conn, tid, COPILOT, DONE, "done", reply_to=r3, to=USER)
        mine, nxt = boardlib.read(self.conn, tid, CURSOR, None, mine=True, roles=["@member"])
        self.assertEqual([m["seq"] for m in mine], [r1, r3])                             # not seq 2 (copilot's) nor r4
        self.assertEqual(nxt, r3)
        self.assertEqual(self.conn.execute("SELECT seq FROM cursors WHERE agent=?", (CURSOR,)).fetchone()[0], r3)
        boardlib.read(self.conn, tid, CURSOR, None)                                      # plain read continues after it
        self.assertEqual(self.conn.execute("SELECT seq FROM cursors WHERE agent=?", (CURSOR,)).fetchone()[0], r4)
        opened, _ = boardlib.read(self.conn, tid, CURSOR, None, open_requests=True)  # from the start, cursor ignored
        self.assertEqual([m["seq"] for m in opened], [r1])
        # a third party's reply does not close it
        boardlib.post(self.conn, tid, COPILOT, "missing: access\nnot mine to close", "blocked", reply_to=r1)
        self.assertEqual([m["seq"] for m in boardlib.read(self.conn, tid, CURSOR, None, open_requests=True)[0]], [r1])
        self.assertEqual(self.conn.execute("SELECT seq FROM cursors WHERE agent=?", (CURSOR,)).fetchone()[0], r4)
        boardlib.post(self.conn, tid, CURSOR, "missing: repo access", "blocked", reply_to=r1)
        self.assertEqual(boardlib.read(self.conn, tid, CURSOR, None, open_requests=True)[0], [])
        r5 = boardlib.post(self.conn, tid, USER, REQUEST, "request", to="@member")        # a role: any holder closes it
        boardlib.post(self.conn, tid, COPILOT, DONE, "done", reply_to=r5)
        self.assertEqual(boardlib.read(self.conn, tid, CURSOR, None, open_requests=True)[0], [])
        r6 = boardlib.post(self.conn, tid, USER, REQUEST, "request", to=CURSOR)
        boardlib.post(self.conn, tid, USER, "never mind", "done", reply_to=r6)            # the requester withdraws it
        self.assertEqual(boardlib.open_requests_to(self.conn, tid, CURSOR), 0)

    def test_status_counts_open_requests_and_shows_only_my_topics(self):
        tid = self.room("room:r", CURSOR)
        boardlib.open_topic(self.conn, "room:other", USER, False)  # user leads it; cursor is not in it
        boardlib.topic_id(self.conn, "conv:legacy", USER)
        boardlib.topic_id(self.conn, "conv:gone", USER)
        boardlib.close_topic(self.conn, "conv:gone")
        boardlib.post(self.conn, tid, USER, REQUEST, "request", to=CURSOR)
        boardlib.post(self.conn, tid, USER, REQUEST, "request", to="@member")
        boardlib.post(self.conn, tid, USER, REQUEST, "request", to=COPILOT)
        boardlib.post(self.conn, tid, CURSOR, REQUEST, "request", to=USER)                # my own: not owed by me
        st = boardlib.status(self.conn, CURSOR)
        self.assertEqual([t["name"] for t in st["topics"]], ["room:r", "conv:legacy"])
        self.assertEqual(st["topics"][0]["open_requests_to_me"], 2)
        self.assertEqual(st["topics"][0]["members"],
                         [{"agent": USER, "role": "leader"}, {"agent": CURSOR, "role": "member"}])
        self.assertEqual(st["open_requests_to_me"], 2)
        boardlib.open_topic(self.conn, "room:prim", PRIMARY, False)                      # the user is not a member
        self.assertEqual([t["name"] for t in boardlib.status(self.conn, USER)["topics"]],
                         ["room:r", "room:other", "conv:legacy", "room:prim"])          # but sees every open topic
        self.assertNotIn("room:prim", [t["name"] for t in boardlib.status(self.conn, COPILOT)["topics"]])


class Opening(Board):
    def test_dm_rules_for_a_bridged_peer(self):
        with self.assertRaisesRegex(boardlib.BoardError, "neither"):                     # a stranger
            boardlib.open_topic(self.conn, f"dm:{CURSOR}+{COPILOT}", CURSOR, True)
        tid, name = boardlib.open_topic(self.conn, f"dm:{CURSOR}+{USER}", CURSOR, True)   # the user: always
        self.assertEqual(name, "dm:cursor#41+user:tester")
        self.assertEqual({m["agent"] for m in boardlib.members(self.conn, tid)}, {CURSOR, USER})
        self.room("room:r", CURSOR, COPILOT)                                              # now they share a topic
        _, name = boardlib.open_topic(self.conn, f"dm:{COPILOT}+{CURSOR}", CURSOR, True)
        self.assertEqual(name, f"dm:{COPILOT}+{CURSOR}")  # sorted, whichever order given
        for bad in (f"dm:{USER}+{COPILOT}", f"dm:{CURSOR}", f"dm:{CURSOR}+{CURSOR}", f"dm:{CURSOR}+{COPILOT}+{USER}",
                    f"dm:{CURSOR}+user:", f"dm:{CURSOR}+user:x y"):
            with self.subTest(bad), self.assertRaises(boardlib.BoardError):
                boardlib.open_topic(self.conn, bad, CURSOR, True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 3)   # nothing junk was created

    def test_a_closed_dm_stays_closed_for_the_peer_and_a_side_that_left_stays_out(self):
        tid, name = boardlib.open_topic(self.conn, f"dm:{CURSOR}+{USER}", CURSOR, True)
        boardlib.leave(self.conn, tid, USER)
        boardlib.post(self.conn, tid, USER, "done here", "done", to="*")
        boardlib.close_topic_as(self.conn, name, USER)
        with self.assertRaisesRegex(boardlib.BoardError, "closed"):
            boardlib.open_topic(self.conn, name, CURSOR, True)
        self.assertIsNotNone(self.conn.execute("SELECT closed_at FROM topics WHERE id=?", (tid,)).fetchone()[0])
        boardlib.open_topic(self.conn, name, USER, False)                                 # the user may reopen
        self.assertEqual([m["agent"] for m in boardlib.members(self.conn, tid)], [CURSOR])   # and is not re-added
        self.assertEqual(self.conn.execute("SELECT body FROM messages WHERE kind='system'").fetchone()[0],
                         f"reopened by {USER}")
        with self.assertRaisesRegex(boardlib.BoardError, "may not open rooms"):
            boardlib.open_topic(self.conn, "room:mine", CURSOR, True)
        with self.assertRaisesRegex(boardlib.BoardError, "may not open topics"):
            boardlib.open_topic(self.conn, "conv:mine", CURSOR, True)

    def test_a_room_is_led_by_its_opener_and_a_reopened_one_keeps_its_leader(self):
        tid, _ = boardlib.open_topic(self.conn, "room:r", PRIMARY, False)
        self.assertEqual(boardlib.members(self.conn, tid), [{"agent": PRIMARY, "role": "leader"}])
        boardlib.close_topic(self.conn, "room:r")
        boardlib.open_topic(self.conn, "room:r", USER, False)
        self.assertEqual(boardlib.members(self.conn, tid), [{"agent": PRIMARY, "role": "leader"}])
        self.assertIsNone(self.conn.execute("SELECT closed_at FROM topics WHERE name='room:r'").fetchone()[0])


class Membership(Board):
    def test_join_and_leave_follow_the_leader(self):
        tid = self.room("room:r", CURSOR)
        with self.assertRaisesRegex(boardlib.BoardError, "leader or the user"):
            boardlib.join(self.conn, tid, CURSOR, COPILOT)
        boardlib.join(self.conn, tid, USER, COPILOT, "observer")
        boardlib.join(self.conn, tid, USER, CURSOR, "leader")                            # role change
        self.assertEqual(boardlib.my_roles(self.conn, tid, CURSOR), ["@leader"])
        boardlib.join(self.conn, tid, CURSOR, PRIMARY)                                   # a leader may add
        with self.assertRaises(boardlib.BoardError):
            boardlib.leave(self.conn, tid, COPILOT, PRIMARY)  # an observer may not remove others
        boardlib.leave(self.conn, tid, COPILOT)                                           # but may leave
        boardlib.leave(self.conn, tid, CURSOR, PRIMARY)
        self.assertEqual({m["agent"] for m in boardlib.members(self.conn, tid)}, {USER, CURSOR})
        with self.assertRaisesRegex(boardlib.BoardError, "not a member"):
            boardlib.leave(self.conn, tid, COPILOT)
        with self.assertRaisesRegex(boardlib.BoardError, "never removed by an agent"):
            boardlib.leave(self.conn, tid, CURSOR, USER)  # a leader cannot evict the user
        self.assertFalse(boardlib.shares_topic(self.conn, CURSOR, COPILOT))
        self.assertTrue(boardlib.shares_topic(self.conn, CURSOR, USER))

    def test_close_needs_a_leader_and_a_fresh_summary(self):
        tid = self.room("room:r", CURSOR)
        boardlib.join(self.conn, tid, USER, COPILOT, "leader")
        for who in (CURSOR, PRIMARY):  # member, and a primary outside
            with self.subTest(who), self.assertRaisesRegex(boardlib.BoardError, "leader or the user"):
                boardlib.close_topic_as(self.conn, "room:r", who)
        with self.assertRaisesRegex(boardlib.BoardError, "summary"):
            boardlib.close_topic_as(self.conn, "room:r", COPILOT)
        boardlib.post(self.conn, tid, COPILOT, "shipped", "done", to=CURSOR)              # not to everyone: no summary
        r = boardlib.post(self.conn, tid, USER, REQUEST, "request", to="*")
        boardlib.post(self.conn, tid, COPILOT, DONE, "done", to="*", reply_to=r)                # closes r, not the room
        with self.assertRaises(boardlib.BoardError):
            boardlib.close_topic_as(self.conn, "room:r", COPILOT)
        boardlib.post(self.conn, tid, COPILOT, "did X, evidence Y, open Z", "done", to="*")
        boardlib.post(self.conn, tid, USER, REQUEST, "request", to=COPILOT)               # new work after the summary
        with self.assertRaises(boardlib.BoardError):
            boardlib.close_topic_as(self.conn, "room:r", COPILOT)
        boardlib.post(self.conn, tid, COPILOT, "did X and W, open Z", "done", to="*")
        boardlib.close_topic_as(self.conn, "room:r", COPILOT)
        with self.assertRaisesRegex(boardlib.BoardError, "not open"):
            boardlib.close_topic_as(self.conn, "room:r", USER)
        boardlib.open_topic(self.conn, "room:r", USER, False)  # reopened: the old summary is stale
        with self.assertRaises(boardlib.BoardError):
            boardlib.close_topic_as(self.conn, "room:r", COPILOT)
        boardlib.post(self.conn, tid, USER, "nothing more", "done", to="*")
        boardlib.close_topic_as(self.conn, "room:r", USER)
        boardlib.topic_id(self.conn, "conv:legacy", PRIMARY)  # no members: its creator closes
        with self.assertRaises(boardlib.BoardError):
            boardlib.close_topic_as(self.conn, "conv:legacy", CURSOR)
        boardlib.close_topic_as(self.conn, "conv:legacy", PRIMARY)
        _, dm = boardlib.open_topic(self.conn, f"dm:{CURSOR}+{USER}", USER, False)  # a dm: either side, after a summary
        boardlib.post(self.conn, boardlib.topic_id(self.conn, dm, None), CURSOR, "answered", "done", to="*")
        boardlib.close_topic_as(self.conn, dm, CURSOR)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM topics WHERE closed_at IS NULL").fetchone()[0], 0)

    def test_gc_removes_members_with_the_topic(self):
        tid = self.room("room:r", CURSOR)
        boardlib.post(self.conn, tid, USER, "bye", "done", to="*")
        boardlib.close_topic(self.conn, "room:r")
        self.conn.execute("UPDATE topics SET closed_at=closed_at-20*86400")
        self.assertEqual(boardlib.gc(self.conn, 14), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM members").fetchone()[0], 0)


class ServerAndCli(Board):
    def test_the_new_tools_through_the_server(self):
        board = _load("bridge-board")
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_AGENT": "claude"}):
            lead = board.Server()
            self.addCleanup(lead.conn.close)
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_IDENTITY": CURSOR, "AGENT_BRIDGE_HOP": "1"}):
            peer = board.Server()
            self.addCleanup(peer.conn.close)
        self.assertEqual((lead.me, lead.peer, peer.me, peer.peer), (PRIMARY, False, CURSOR, True))
        text = lambda out: out["content"][0]["text"]  # noqa: E731
        self.assertTrue(peer.call("board_open", {"name": "room:r"})["isError"])
        self.assertFalse(lead.call("board_open", {"name": "room:r"})["isError"])
        self.assertFalse(lead.call("board_join", {"topic": "room:r", "agent": CURSOR})["isError"])
        out = lead.call("board_post", {"topic": "room:r", "body": REQUEST, "kind": "request", "to": CURSOR})
        self.assertFalse(out["isError"], out)
        self.assertIn('"open_requests_to_me": 1', text(peer.call("board_status", {})))
        self.assertIn("kind=request to=cursor#41",
                      text(peer.call("board_read", {"topic": "room:r", "open_requests": True})))
        done = {"topic": "room:r", "body": DONE, "kind": "done", "reply_to": 1}
        self.assertFalse(peer.call("board_post", done)["isError"])
        self.assertIn('"open_requests_to_me": 0', text(peer.call("board_status", {})))
        self.assertTrue(peer.call("board_close", {"topic": "room:r"})["isError"])
        self.assertFalse(peer.call("board_leave", {"topic": "room:r"})["isError"])
        self.assertTrue(lead.call("board_close", {"topic": "room:r"})["isError"])                  # no summary yet
        lead.call("board_post", {"topic": "room:r", "body": "summary", "kind": "done", "to": "*"})
        self.assertFalse(lead.call("board_close", {"topic": "room:r"})["isError"])
        names = {t["name"] for t in board.TOOLS}
        self.assertTrue({"board_open", "board_join", "board_leave", "board_close"} <= names)

    def test_the_cli_as_the_user(self):
        self.assertEqual(self.cli("open", "room:cli").stdout.strip(), "room:cli")
        self.assertEqual(self.cli("join", "room:cli", CURSOR, "-r", "observer").returncode, 0)
        p = self.cli("post", "room:cli", REQUEST, "-k", "request", "--to", "@observer")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.cli("post", "room:cli", "no shape", "-k", "request", "--to", CURSOR).returncode, 1)
        out = self.cli("read", "room:cli", "--open-requests").stdout
        self.assertIn("kind=request to=@observer", out)
        self.assertEqual(self.cli("close", "room:cli").returncode, 1)                     # members, no done to=*
        self.cli("post", "room:cli", "all done", "-k", "done", "--to", "*")
        self.assertEqual(self.cli("close", "room:cli").returncode, 0)
        self.assertEqual(self.cli("leave", "room:cli", COPILOT).returncode, 1)            # not a member
        for sub in ("open", "join", "leave", "post", "read", "close"):
            self.assertEqual(self.cli(sub, "--help").returncode, 0, sub)


if __name__ == "__main__":
    unittest.main()
