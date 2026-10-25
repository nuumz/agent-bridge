"""P4 — a peer that is not running joins a room by the turn: a `request` addressed to its family (claude | cursor |
copilot) is queued as a task in that topic, `bridge-board dispatch` starts one run, the run reads what reaches its
family and answers with reply_to — which closes the request and the task together."""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import boardlib
from bridgelib import ledger
from bridgelib.adapters import ADAPTERS, Parsed
from bridgelib.runner import BoardRun

USER, PRIMARY, CURSOR_RUN, LIVE = "user:tester", "claude@primary", "cursor:task1", "claude@live:p"
REQUEST = "shape: table file:line · claim · evidence\n\nList every caller of post()."
TABLE = "| file:line | claim | evidence |\n|---|---|---|\n| bin/boardlib.py:1 | one | read it |"


def _load():
    loader = importlib.machinery.SourceFileLoader("bridge_board_mod", str(BIN / "bridge-board"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Turns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        board_dir = home / ".agent-bridge" / "state" / "board"
        self.patches = [mock.patch.object(boardlib, "BOARD_DIR", board_dir),
                        mock.patch.object(boardlib, "DB_PATH", board_dir / "board.db"),
                        mock.patch.object(ledger, "LEDGER", board_dir / "ledger.jsonl"),   # _score_reply logs here
                        mock.patch.dict(os.environ, {"HOME": str(home), "USER": "tester"})]
        for p in self.patches:
            p.start()
        for var in ("AGENT_BRIDGE_HOP", "AGENT_BRIDGE_IDENTITY", "AGENT_BRIDGE_AGENT", "AGENT_BRIDGE_TASK"):
            os.environ.pop(var, None)
        self.conn = boardlib.connect()
        self.tid, _ = boardlib.open_topic(self.conn, "room:r", USER, False)
        boardlib.member_add(self.conn, self.tid, LIVE, "design-critic")

    def tearDown(self):
        self.conn.close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def tasks(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM tasks ORDER BY id")]

    def test_a_request_to_a_family_is_a_queued_task_with_the_request_seq(self):
        seq = boardlib.post(self.conn, self.tid, USER, f"dir: {self.tmp.name}\nmode: plan\n{REQUEST}", "request",
                            to="cursor")
        (t,) = self.tasks()
        resolved = str(Path(self.tmp.name).resolve())                 # macOS: /var/… is a link to /private/var/…
        self.assertEqual((t["assignee"], t["mode"], t["dir"], t["origin"], t["request_seq"], t["status"]),
                         ("cursor", "plan", resolved, "user", seq, "queued"))
        self.assertEqual(t["title"], "List every caller of post().")                   # the first non-header line
        self.assertIn(f"request seq {seq}", self.conn.execute("SELECT body FROM messages WHERE kind='task'").fetchone()[0])
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, CURSOR_RUN), 1)   # the run sees it as its own
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, "copilot:task9"), 0)
        self.assertEqual([x["name"] for x in boardlib.status(self.conn, CURSOR_RUN)["topics"]], ["room:r"])
        self.assertEqual(boardlib.status(self.conn, "copilot:task9")["topics"], [])     # not a member, nothing owed
        top = self.conn.execute("SELECT MAX(seq) FROM messages").fetchone()[0]
        fed = boardlib.reaching(self.conn, self.tid, CURSOR_RUN, 0, top)
        self.assertEqual([m["kind"] for m in fed], ["request"])

    def test_who_may_call_a_turn(self):
        for who in (USER, PRIMARY):
            boardlib.post(self.conn, self.tid, who, REQUEST, "request", to="copilot")
        boardlib.member_add(self.conn, self.tid, "cursor#7", "leader")
        boardlib.post(self.conn, self.tid, "cursor#7", REQUEST, "request", to="claude")   # a leader may
        with self.assertRaisesRegex(boardlib.BoardError, "leader may call"):
            boardlib.post(self.conn, self.tid, LIVE, REQUEST, "request", to="cursor")     # a member may not
        self.assertEqual([t["origin"] for t in self.tasks()], ["user", "user", "user"])   # a leader's turn runs
        with self.assertRaisesRegex(boardlib.BoardError, "mode must be one of"):
            boardlib.post(self.conn, self.tid, USER, f"mode: fly\n{REQUEST}", "request", to="cursor")
        with self.assertRaisesRegex(boardlib.BoardError, "does not exist"):
            boardlib.post(self.conn, self.tid, USER, f"dir: /no/such/dir\n{REQUEST}", "request", to="cursor")
        self.assertEqual(len(self.tasks()), 3)                                           # a refused turn queues nothing
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages WHERE kind='request'").fetchone()[0], 3)

    def test_the_runs_reply_closes_request_and_task(self):
        seq = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="cursor")
        (t,) = self.tasks()
        self.conn.execute("UPDATE tasks SET status='claimed', claimed_by=? WHERE id=?", (CURSOR_RUN, t["id"]))
        with self.assertRaises(boardlib.BoardError):                                      # shape still applies
            boardlib.post(self.conn, self.tid, CURSOR_RUN, "three callers", "done", reply_to=seq)
        boardlib.post(self.conn, self.tid, "copilot:task3", TABLE, "done", reply_to=seq)   # the wrong family: no
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, CURSOR_RUN), 1)
        self.assertEqual(self.tasks()[0]["status"], "claimed")
        boardlib.post(self.conn, self.tid, CURSOR_RUN, TABLE, "done", reply_to=seq, to=USER)
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, CURSOR_RUN), 0)
        self.assertEqual((self.tasks()[0]["status"], self.tasks()[0]["result"]), ("done", TABLE))
        self.assertIn("answered request seq", self.conn.execute(
            "SELECT body FROM messages WHERE kind='task' ORDER BY seq DESC LIMIT 1").fetchone()[0])
        seq2 = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="cursor")  # a queued turn, answered
        boardlib.post(self.conn, self.tid, "cursor#88", "missing: the repo path\nno tree here", "blocked", reply_to=seq2)
        self.assertEqual(self.tasks()[1]["status"], "failed")                             # by hand, blocked → failed
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, "cursor#88"), 0)
        seq3 = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="cursor")
        boardlib.post(self.conn, self.tid, USER, "never mind", "done", reply_to=seq3)       # the requester withdraws
        self.assertEqual(self.tasks()[2]["status"], "cancelled")                            # nothing left to dispatch
        self.assertIn("cancelled: request seq", self.conn.execute(
            "SELECT body FROM messages WHERE kind='task' ORDER BY seq DESC LIMIT 1").fetchone()[0])

    def test_a_family_whose_name_carries_a_digit_is_still_a_turn(self):
        seq = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="a2a")
        (t,) = self.tasks()
        self.assertEqual((t["assignee"], t["status"]), ("a2a", "queued"))
        self.conn.execute("UPDATE tasks SET status='claimed', claimed_by=? WHERE id=?", ("a2a:task1", t["id"]))
        self.assertTrue(boardlib.is_turn_run("a2a:task1"))
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, "a2a:task1"), 1)   # the run sees its turn
        boardlib.post(self.conn, self.tid, "a2a:task1", TABLE, "done", reply_to=seq, to=USER)
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, "a2a:task1"), 0)
        self.assertEqual((self.tasks()[0]["status"], self.tasks()[0]["result"]), ("done", TABLE))  # not cancelled

    def run_of(self, identity: str, task_id: int) -> BoardRun:
        """What ask-agent builds under dispatch: the identity and task id come from the environment."""
        self.conn.execute("UPDATE tasks SET status='claimed', claimed_by=? WHERE id=?", (identity, task_id))
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_IDENTITY": identity, "AGENT_BRIDGE_TASK": str(task_id)}):
            return BoardRun(ADAPTERS[boardlib.family(identity)], True, Path(self.tmp.name) / "run.json", 60)

    def replies(self, seq: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT author, kind, body, addressee FROM messages WHERE "
                                                   "reply_to=? ORDER BY seq", (seq,))]

    def test_the_bridge_answers_a_turn_whose_run_did_not(self):
        seq = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="cursor")     # the run answered: as is
        self.run_of(CURSOR_RUN, 1).finish(0, Parsed(TABLE))
        self.assertEqual(self.replies(seq), [{"author": CURSOR_RUN, "kind": "answer", "body": TABLE, "addressee": USER}])
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, CURSOR_RUN), 0)
        self.assertEqual((self.tasks()[0]["status"], self.tasks()[0]["result"]), ("done", TABLE))
        seq = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="cursor")     # off shape: blocked
        self.run_of("cursor:task2", 2).finish(0, Parsed("three callers, trust me"))
        (r,) = self.replies(seq)
        self.assertEqual((r["author"], r["kind"]), ("cursor:task2", "blocked"))
        self.assertIn("missing: an answer in the request's shape", r["body"])
        self.assertIn("three callers, trust me", r["body"])
        self.assertEqual(self.tasks()[1]["status"], "failed")
        seq = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="a2a")        # the run failed: blocked
        self.run_of("a2a:task3", 3).finish(1, Parsed("A2A task failed: missing: the build log", is_error=True))
        (r,) = self.replies(seq)
        self.assertEqual((r["author"], r["kind"]), ("a2a:task3", "blocked"))
        self.assertIn("missing: an answer — the run ended with exit 1", r["body"])
        self.assertIn("A2A task failed: missing: the build log", r["body"])
        self.assertEqual(self.tasks()[2]["status"], "failed")
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, "a2a:task3"), 0)
        seq = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="a2a")        # no text at all
        self.run_of("a2a:task4", 4).finish(143, None)
        (r,) = self.replies(seq)
        self.assertEqual(r["kind"], "blocked")
        self.assertIn("exit 143", r["body"])
        seq = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="a2a")        # withdrawn: nothing to say
        run = self.run_of("a2a:task5", 5)
        boardlib.post(self.conn, self.tid, USER, "never mind", "done", reply_to=seq)
        run.finish(0, Parsed(TABLE))
        self.assertEqual([r["author"] for r in self.replies(seq)], [USER])
        self.assertEqual(self.tasks()[4]["status"], "cancelled")
        seq = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="cursor")     # the run posted itself
        run = self.run_of("cursor:task6", 6)
        boardlib.post(self.conn, self.tid, "cursor:task6", TABLE, "done", reply_to=seq, to=USER)
        run.finish(0, Parsed(TABLE))
        self.assertEqual(len(self.replies(seq)), 1)
        self.assertEqual(self.tasks()[5]["status"], "done")

    def test_a_family_turn_reaches_the_dispatched_run_only(self):
        seq = boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="claude")
        top = self.conn.execute("SELECT MAX(seq) FROM messages").fetchone()[0]
        for who in (LIVE, PRIMARY, "claude#55"):                                           # in the room, or a hand run
            self.assertEqual([m["seq"] for m in boardlib.reaching(self.conn, self.tid, who, 0, top)], [])
            self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, who), 0)
        self.assertEqual([m["seq"] for m in boardlib.reaching(self.conn, self.tid, "claude:task1", 0, top)], [seq])
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, "claude:task1"), 1)
        boardlib.post(self.conn, self.tid, LIVE, TABLE, "answer", reply_to=seq)             # a live peer's reply: not it
        self.assertEqual(boardlib.open_requests_to(self.conn, self.tid, "claude:task1"), 1)
        self.assertEqual(self.tasks()[0]["status"], "queued")
        boardlib.post(self.conn, self.tid, "claude#55", TABLE, "done", reply_to=seq)         # a hand-started run: yes
        self.assertEqual(self.tasks()[0]["status"], "done")

    def test_a_hand_run_of_the_leaders_family_is_still_scored(self):
        boardlib.member_add(self.conn, self.tid, "cursor#41", "leader")
        seq = boardlib.post(self.conn, self.tid, "cursor#41", REQUEST, "request", to="cursor")
        with self.assertRaises(boardlib.BoardError):                                        # not "the same run"
            boardlib.post(self.conn, self.tid, "cursor#99", "three callers", "done", reply_to=seq)
        boardlib.post(self.conn, self.tid, "cursor#41", "never mind", "done", reply_to=seq)   # the requester itself
        self.assertEqual(self.tasks()[0]["status"], "cancelled")

    def test_a_turn_and_its_request_land_together_or_not_at_all(self):
        boardlib.member_add(self.conn, self.tid, "cursor#7", "leader")
        for _ in range(boardlib.PEER_OPEN_TASKS):
            boardlib.task_add(self.conn, self.tid, "t", "", "cursor#7", True)
        with self.assertRaisesRegex(boardlib.BoardError, "peer task limit"):
            boardlib.post(self.conn, self.tid, "cursor#7", REQUEST, "request", to="copilot")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages WHERE kind='request'").fetchone()[0], 0)
        self.assertEqual(len(self.tasks()), boardlib.PEER_OPEN_TASKS)
        for t in self.tasks():                                                # the cap counts the leader's own turns
            boardlib.task_cancel(self.conn, t["id"], USER)
        for _ in range(boardlib.PEER_OPEN_TASKS):
            boardlib.post(self.conn, self.tid, "cursor#7", REQUEST, "request", to="copilot")
        with self.assertRaisesRegex(boardlib.BoardError, "peer task limit"):
            boardlib.post(self.conn, self.tid, "cursor#7", REQUEST, "request", to="copilot")
        boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="copilot")        # the user is not capped
        self.assertEqual(len([t for t in self.tasks() if t["status"] == "queued"]), boardlib.PEER_OPEN_TASKS + 1)
        for t in self.tasks():
            if t["status"] == "queued":
                boardlib.task_cancel(self.conn, t["id"], USER)
        with self.assertRaisesRegex(boardlib.BoardError, "mode: write needs"):
            boardlib.post(self.conn, self.tid, USER, f"mode: write\n{REQUEST}", "request", to="cursor")
        boardlib.post(self.conn, self.tid, USER, f"dir: .\nmode: write\n{REQUEST}", "request", to="cursor")
        self.assertEqual(self.tasks()[-1]["dir"], str(Path.cwd().resolve()))                  # stored resolved
        self.assertEqual(boardlib.body_title("shape: list\n\nbin/x.py:241 — reentrant?\nmore"), "bin/x.py:241 — reentrant?")
        self.assertEqual(boardlib.body_title("shape: list"), "shape: list")

    def test_an_env_identity_never_claims_the_primary(self):
        env = {**os.environ, "AGENT_BRIDGE_HOP": "1", "AGENT_BRIDGE_IDENTITY": "claude@primary"}
        p = subprocess.run([sys.executable, str(BIN / "bridge-board"), "post", "room:r", REQUEST, "-k", "request",
                            "--to", "copilot"], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("leader may call", p.stderr)
        self.assertEqual(boardlib.resolve_identity(self.conn, {"AGENT_BRIDGE_IDENTITY": "user:tester",
                                                               "AGENT_BRIDGE_HOP": "1"})[0], "user@hop")
        self.assertEqual(boardlib.resolve_identity(self.conn, {"AGENT_BRIDGE_IDENTITY": "claude@primary"})[0],
                         "claude@primary")                                                  # no hop: the primary itself

    def test_dispatch_starts_the_turn_with_the_request_prompt(self):
        board = _load()
        seq = boardlib.post(self.conn, self.tid, USER, f"dir: {self.tmp.name}\n{REQUEST}", "request", to="cursor")
        o = argparse.Namespace(max_agents=2, include_peer_tasks=False)
        with mock.patch.object(board.subprocess, "Popen") as popen, mock.patch.object(board.B, "live_workers",
                                                                                        return_value=[]):
            started = board.dispatch_once(self.conn, o, ["cursor", "claude"], [], "/bin/ask-agent")
        self.assertEqual(started, ["#1→cursor"])
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:6], ["/bin/ask-agent", "cursor", "-d", str(Path(self.tmp.name).resolve()), "-m", "ask"])
        prompt = argv[-1]
        self.assertIn(f"request seq {seq} on that topic, from {USER}", prompt)
        self.assertIn(f"reply_to={seq}", prompt)
        self.assertIn('to="user:tester"', prompt)
        self.assertIn("board_read(topic, mine=true)", prompt)
        self.assertEqual(popen.call_args.kwargs["env"]["AGENT_BRIDGE_IDENTITY"], "cursor:task1")
        self.assertEqual(self.tasks()[0]["claimed_by"], "cursor:task1")
        self.assertIn("Recent messages on the topic", prompt)
        plain = board.task_prompt({"id": 2, "topic": "tasks", "title": "t", "body": "b", "request_seq": None}, "x")
        self.assertIn("board_task_done(2, <result>)", plain)
        # a remote family has no board tools and should learn nothing local: the request, its shape, nothing else
        boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="a2a")
        with mock.patch.object(board.subprocess, "Popen") as popen, mock.patch.object(board.B, "live_workers",
                                                                                        return_value=[]):
            self.assertEqual(board.dispatch_once(self.conn, o, ["a2a"], [], "/bin/ask-agent"), ["#2→a2a"])
        remote = popen.call_args.args[0][-1]
        self.assertIn(REQUEST, remote)
        self.assertIn("shape", remote)
        for local in ("board_", "room:r", "task #", USER, "Recent messages"):
            self.assertNotIn(local, remote)
        self.assertEqual(popen.call_args.kwargs["env"]["AGENT_BRIDGE_IDENTITY"], "a2a:task2")

    def test_dispatch_names_why_a_queued_turn_did_not_start(self):
        board = _load()
        boardlib.post(self.conn, self.tid, USER, REQUEST, "request", to="cursor")
        live = [{"identity": LIVE, "family": "claude", "task_id": None}]   # a live room peer counts against the cap
        o = argparse.Namespace(max_agents=1, include_peer_tasks=False)
        with mock.patch.object(board.subprocess, "Popen") as popen, \
                mock.patch.object(board.B, "live_workers", return_value=live), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            started = board.dispatch_once(self.conn, o, ["cursor"], [], "/bin/ask-agent")
        self.assertEqual(started, [])
        popen.assert_not_called()
        self.assertIn(f"task #1 and later: --max-agents 1 reached ({LIVE})", err.getvalue())
        self.assertEqual(self.tasks()[0]["status"], "queued")
        o.max_agents = 2                                                 # family not allowed: said, not silent
        with mock.patch.object(board.subprocess, "Popen"), mock.patch.object(board.B, "live_workers", return_value=[]), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            board.dispatch_once(self.conn, o, ["claude"], [], "/bin/ask-agent")
        self.assertIn("task #1 (cursor): cursor is not in --agents claude", err.getvalue())
        boardlib.post(self.conn, self.tid, USER, f"dir: {self.tmp.name}\nmode: write\n{REQUEST}", "request", to="cursor")
        with mock.patch.object(board.subprocess, "Popen") as popen, \
                mock.patch.object(board.B, "live_workers", return_value=[]), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:     # write turn, no --allow-write: said why
            board.dispatch_once(self.conn, o, ["claude"], [], "/bin/ask-agent")
        self.assertIn(f"task #2: mode=write in {Path(self.tmp.name).resolve()} — pass --allow-write", err.getvalue())
        popen.assert_not_called()

    def test_live_send_gives_a_family_turn_the_rooms_dir(self):
        loader = importlib.machinery.SourceFileLoader("bridge_live_mod", str(BIN / "bridge-live"))
        live = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(live)
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        peers = {"claude": {"room": "r", "dir": str(repo), "status": "ready"}}
        o = argparse.Namespace(room="r", message=REQUEST, kind="request", to="cursor", no_wait=False)
        with mock.patch.object(live, "room_peers", return_value=peers), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(live.send(o), 0)
        self.assertEqual(self.tasks()[-1]["dir"], str(repo.resolve()))    # not dispatch's cwd
        o.message = f"dir: {self.tmp.name}\n{REQUEST}"                     # an explicit dir: is the caller's choice
        with mock.patch.object(live, "room_peers", return_value=peers), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(live.send(o), 0)
        self.assertEqual(self.tasks()[-1]["dir"], str(Path(self.tmp.name).resolve()))
        o.message, o.to = REQUEST, "a2a"                                   # an a2a turn leaves the machine: no local path
        with mock.patch.object(live, "room_peers", return_value=peers), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(live.send(o), 0)
        last = self.conn.execute("SELECT body FROM messages WHERE kind='request' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertNotIn(str(repo), last["body"])

    def test_the_cli_and_the_mcp_server_take_the_same_path(self):
        p = subprocess.run([sys.executable, str(BIN / "bridge-board"), "post", "room:r", REQUEST, "-k", "request",
                            "--to", "copilot"], capture_output=True, text=True, env=dict(os.environ), check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        board = _load()
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_AGENT": "claude"}):
            primary = board.Server()
        out = primary.call("board_post", {"topic": "room:r", "body": REQUEST, "kind": "request", "to": "cursor"})
        self.assertFalse(out["isError"], out)
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_IDENTITY": LIVE, "AGENT_BRIDGE_HOP": "1"}):
            member = board.Server()
        out = member.call("board_post", {"topic": "room:r", "body": REQUEST, "kind": "request", "to": "cursor"})
        self.assertTrue(out["isError"])
        self.assertEqual([t["assignee"] for t in self.tasks()], ["copilot", "cursor"])
        status = primary.call("board_status", {})["content"][0]["text"]
        self.assertIn('"assignee": "copilot"', status)


if __name__ == "__main__":
    unittest.main()
