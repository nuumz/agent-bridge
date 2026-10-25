"""The A2A transport against a real server thread and a temporary board: the card is public and the rest needs a
token; message/send is one board request on the client's own topic, a turn for a family the operator allowed, that
only a dispatch runs; tasks/get reads submitted · working · completed · failed · canceled off the board; tasks/cancel
withdraws; a client sees only its own tasks; the JSON-RPC and A2A error codes; a blocking send; the `bridge-a2a send`
CLI's JSON Lines and the `a2a` adapter's reading of them; and `ask-agent a2a` end to end through the real binary."""
from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import boardlib  # noqa: E402
from bridgelib import a2a, ledger  # noqa: E402
from bridgelib.adapters import ADAPTERS  # noqa: E402

ANSWER = "The retry loop in src/retry.py:12 never backs off."


class Server(unittest.TestCase):
    """A server thread on an ephemeral loopback port, a board and an a2a state dir under a temporary HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()             # macOS: /var is a link to /private/var, and the board
        self.home, self.work = root / "home", root / "work"   # stores a turn's dir resolved
        self.work.mkdir()
        state = self.home / ".agent-bridge" / "state"
        a2a_dir = state / "a2a"
        self.patches = [mock.patch.object(boardlib, "BOARD_DIR", state / "board"),
                        mock.patch.object(boardlib, "DB_PATH", state / "board" / "board.db"),
                        mock.patch.object(ledger, "LEDGER", state / "ledger.jsonl"),
                        mock.patch.object(a2a, "A2A_DIR", a2a_dir),
                        mock.patch.object(a2a, "CLIENTS", a2a_dir / "clients.json"),
                        mock.patch.object(a2a, "SERVER_STATE", a2a_dir / "server.json"),
                        mock.patch.object(a2a, "LAST_CONTEXT", a2a_dir / "last-context.json"),
                        mock.patch.dict(os.environ, {"HOME": str(self.home), "USER": "tester"})]
        for p in self.patches:
            p.start()
        for var in list(os.environ):
            if var.startswith("AGENT_BRIDGE_"):
                os.environ.pop(var)
        self.token = a2a.add_client("alice")
        self.bob = a2a.add_client("bob")
        self.cfg = a2a.Config("test-bridge", "", ["claude", "cursor"], str(self.work), a2a.load_clients(),
                              allow_write=False, max_block=5)
        self.srv = a2a.make_server(self.cfg, "127.0.0.1", 0, quiet=True)
        self.url = self.cfg.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.conn = boardlib.connect()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.conn.close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def client(self, token: str | None = "alice") -> a2a.Client:
        return a2a.Client(self.url, self.token if token == "alice" else token, timeout=5)

    def raw(self, body: bytes, token: str | None = None, path: str = "/") -> tuple[int, object]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.url + path, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")

    def seq_of(self, task: dict) -> int:
        return int(task["id"].rsplit("/", 1)[1])

    def tid(self, topic: str = "a2a:alice") -> int:
        return self.conn.execute("SELECT id FROM topics WHERE name=?", (topic,)).fetchone()["id"]

    def reply_when_asked(self, kind: str, body: str, author: str = "claude#5", after: int = 0) -> threading.Thread:
        """A stand-in for the dispatched run: wait for the next request on alice's topic, answer it."""
        def run():
            conn = boardlib.connect()
            deadline = time.time() + 20
            while time.time() < deadline:
                row = conn.execute("SELECT m.seq, m.topic_id FROM messages m JOIN topics t ON t.id=m.topic_id WHERE "
                                   "t.name='a2a:alice' AND m.kind='request' AND m.seq>? ORDER BY m.seq LIMIT 1",
                                   (after,)).fetchone()
                if row:
                    boardlib.post(conn, row["topic_id"], author, body, kind, row["seq"], "a2a:alice")
                    break
                time.sleep(0.1)
            conn.close()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    # -- card and auth
    def test_card_is_public_and_describes_the_bridge(self):
        card = self.client(None).card()
        self.assertEqual(card["name"], "test-bridge")
        self.assertEqual(card["url"], self.url)
        self.assertEqual(card["protocolVersion"], a2a.A2A_VERSION)
        self.assertEqual(card["skills"][0]["id"], "bridge-request")
        self.assertIn("claude, cursor", card["description"])
        self.assertFalse(card["capabilities"]["streaming"])
        self.assertEqual(card["securitySchemes"]["bearer"]["scheme"], "bearer")
        with urllib.request.urlopen(self.url + "/.well-known/agent.json", timeout=5) as r:   # the older name too
            self.assertEqual(json.loads(r.read())["name"], "test-bridge")

    def test_tokens_gate_everything_but_the_card(self):
        with self.assertRaises(a2a.A2AError) as cm:
            self.client(None).get("a2a:alice/1")
        self.assertEqual(cm.exception.code, 401)
        with self.assertRaises(a2a.A2AError) as cm:
            self.client("not-a-token").get("a2a:alice/1")
        self.assertEqual(cm.exception.code, 401)
        req = urllib.request.Request(self.url + "/anything", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as cm2:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm2.exception.code, 404)
        # a client row whose token is empty or null (a hand-edited clients.json) must authenticate nobody
        a2a.CLIENTS.write_text(json.dumps({"alice": self.token, "bob": "", "eve": None}))
        clients = a2a.load_clients()
        self.assertEqual(clients, {"alice": self.token})
        for header in ("Bearer ", "Bearer", "Bearer None", "Bearer null", "Bearer  "):
            self.assertIsNone(a2a.authenticate(header, clients), header)
            self.assertIsNone(a2a.authenticate(header, {"bob": ""}), header)   # a table that slipped past the loader
        self.assertEqual(a2a.authenticate(f"Bearer {self.token}", clients), "alice")

    # -- the HTTP layer, byte by byte
    def sock(self, srv=None) -> socket.socket:
        port = (srv or self.srv).server_port
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(s.close)
        return s

    @staticmethod
    def http(s: socket.socket, request: bytes) -> tuple[int, dict[str, str], bytes]:
        """Send one request, read exactly one response (status, headers, body) — what a keep-alive client does."""
        s.sendall(request)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError(f"closed before a response: {buf!r}")
            buf += chunk
        head, body = buf.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        status = int(lines[0].split()[1])
        headers = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:])}
        want = int(headers.get("content-length", "0"))
        while len(body) < want:
            chunk = s.recv(4096)
            if not chunk:
                break
            body += chunk
        return status, headers, body

    @staticmethod
    def closed(s: socket.socket) -> bool:
        try:
            return s.recv(1) == b""
        except ConnectionResetError:
            return True
        except TimeoutError:
            return False

    def post_bytes(self, body: bytes, token: str | None, extra: str = "", length: str | None = None) -> bytes:
        auth = f"Authorization: Bearer {token}\r\n" if token else ""
        cl = f"Content-Length: {len(body) if length is None else length}\r\n"
        return (f"POST / HTTP/1.1\r\nHost: x\r\n{auth}Content-Type: application/json\r\n{cl}{extra}\r\n"
                .encode() + body)

    def test_an_error_response_closes_the_connection_so_the_body_is_never_the_next_request(self):
        smuggled = b"GET /.well-known/agent-card.json HTTP/1.1\r\nHost: x\r\n\r\n"
        s = self.sock()
        status, headers, _ = self.http(s, self.post_bytes(smuggled, token=None))
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("connection"), "close")
        self.assertTrue(self.closed(s), "the unread body must not be answered as a request of its own")
        s = self.sock()                                                        # the same with a real token: 413
        status, headers, _ = self.http(s, self.post_bytes(b"", self.token, length=str(a2a.MAX_BODY + 1)))
        self.assertEqual((status, headers.get("connection")), (413, "close"))
        self.assertTrue(self.closed(s))
        s = self.sock()                                                        # and after a parse error
        status, _, body = self.http(s, self.post_bytes(b"{not json", self.token))
        self.assertEqual((status, json.loads(body)["error"]["code"]), (200, a2a.PARSE_ERROR))
        self.assertTrue(self.closed(s))
        s = self.sock()                                                        # a good request keeps the connection
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "a2a:alice/1"}}).encode()
        status, headers, body = self.http(s, self.post_bytes(req, self.token))
        self.assertEqual((status, json.loads(body)["error"]["code"]), (200, a2a.TASK_NOT_FOUND))
        self.assertNotEqual(headers.get("connection"), "close")
        status, _, body = self.http(s, self.post_bytes(req, self.token))
        self.assertEqual(status, 200)

    def test_the_body_length_is_a_plain_decimal_and_chunked_is_refused(self):
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "a2a:alice/1"}}).encode()
        for bad in ("1_0", "-1", "+10", "0x10", "10 ", "99999999", ""):
            with self.subTest(length=bad):
                s = self.sock()
                status, headers, _ = self.http(s, self.post_bytes(req, self.token, length=bad))
                self.assertIn(status, (400, 411, 413), bad)
                self.assertEqual(headers.get("connection"), "close")
        s = self.sock()
        status, _, _ = self.http(s, self.post_bytes(b"", self.token, extra="Transfer-Encoding: chunked\r\n"))
        self.assertEqual(status, 411)
        self.assertTrue(self.closed(s))

    def test_a_connection_that_sends_nothing_is_closed_after_the_timeout(self):
        srv = a2a.make_server(self.cfg, "127.0.0.1", 0, quiet=True, conn_timeout=1)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        half = self.sock(srv)
        half.sendall(b"POST / HTTP/1.1\r\nHost: x\r\n")                       # headers never finish
        t0 = time.time()
        self.assertTrue(self.closed(half), "a half-sent request must not hold its thread forever")
        self.assertLess(time.time() - t0, 4)
        no_body = self.sock(srv)
        status, _, _ = self.http(no_body, self.post_bytes(b"", self.token, length="10"))   # body never comes
        self.assertEqual(status, 408)
        self.assertTrue(self.closed(no_body))
        # the server is still fine for the next client
        self.assertEqual(a2a.Client(f"http://127.0.0.1:{srv.server_port}", self.token, timeout=5).card()["name"],
                         "test-bridge")

    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_the_tls_handshake_runs_on_the_connections_thread(self):
        tls = Path(self.tmp.name) / "tls"
        tls.mkdir()
        cert, key = tls / "cert.pem", tls / "key.pem"
        made = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                               "-out", str(cert), "-days", "1", "-subj", "/CN=agent-bridge-test"],
                              capture_output=True, text=True, timeout=60)
        if made.returncode != 0:
            self.skipTest(f"openssl cannot make a certificate here: {made.stderr.strip()[:200]}")
        srv = a2a.make_server(self.cfg, "127.0.0.1", 0, quiet=True, conn_timeout=1, cert=str(cert), key=str(key))
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        silent = self.sock(srv)                      # a TCP connection that never starts a handshake
        plain = self.sock(srv)                       # and one that speaks HTTP to the TLS port
        plain.sendall(b"GET /.well-known/agent-card.json HTTP/1.1\r\nHost: x\r\n\r\n")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False                   # the cert is its own CA; the name is not under test
        ctx.load_verify_locations(str(cert))
        t0 = time.time()
        with urllib.request.urlopen(f"https://127.0.0.1:{srv.server_port}/.well-known/agent-card.json", timeout=3,
                                    context=ctx) as r:                     # served while the others sit there
            self.assertEqual(json.loads(r.read())["name"], "test-bridge")
        self.assertLess(time.time() - t0, 3)
        self.assertTrue(self.closed(silent), "a silent connection costs one thread for conn_timeout, no more")
        self.assertTrue(self.closed(plain))
        with self.assertRaises(a2a.A2AError):           # the client refuses a bad cert instead of sending the token
            a2a.Client(f"https://127.0.0.1:{srv.server_port}", self.token, timeout=3).card()

    def test_a_batch_holds_one_to_thirty_two_requests(self):
        one = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "a2a:alice/1"}}
        status, resp = self.raw(json.dumps([]).encode(), self.token)
        self.assertEqual((status, resp["error"]["code"]), (200, a2a.INVALID_REQUEST))
        status, resp = self.raw(json.dumps([one] * (a2a.BATCH_MAX + 1)).encode(), self.token)
        self.assertEqual((status, resp["error"]["code"]), (200, a2a.INVALID_REQUEST))
        status, resp = self.raw(json.dumps([one] * a2a.BATCH_MAX).encode(), self.token)
        self.assertEqual((status, len(resp)), (200, a2a.BATCH_MAX))

    # -- the mapping onto the board
    def test_send_becomes_a_turn_on_the_clients_own_topic(self):
        task = self.client().send("Review src/retry.py for retry bugs.", to="cursor", shape="file:line findings")
        self.assertEqual(task["status"]["state"], "submitted")
        self.assertEqual(task["contextId"], "a2a:alice")
        self.assertRegex(task["id"], r"^a2a:alice/\d+$")
        self.assertEqual(task["history"][0]["role"], "user")
        self.assertEqual(task["history"][0]["parts"][0]["text"], "Review src/retry.py for retry bugs.")
        self.assertEqual(task["metadata"]["to"], "cursor")
        tid = self.tid()
        self.assertEqual(boardlib.members(self.conn, tid), [{"agent": "a2a:alice", "role": "leader"}])
        req = self.conn.execute("SELECT author, kind, body, addressee FROM messages WHERE topic_id=? AND seq=?",
                                (tid, self.seq_of(task))).fetchone()
        self.assertEqual((req["author"], req["kind"], req["addressee"]), ("a2a:alice", "request", "cursor"))
        self.assertEqual(boardlib.header(req["body"]),
                         {"shape": "file:line findings", "mode": "ask", "dir": str(self.work),
                          "message-id": task["history"][0]["messageId"]})
        board_task = self.conn.execute("SELECT * FROM tasks WHERE topic_id=? AND request_seq=?",
                                       (tid, self.seq_of(task))).fetchone()
        self.assertEqual((board_task["assignee"], board_task["status"], board_task["origin"], board_task["dir"]),
                         ("cursor", "queued", "user", str(self.work)))
        self.assertEqual(task["metadata"]["board"]["task"], board_task["id"])
        self.assertEqual(self.client().get(task["id"])["status"]["state"], "submitted")

    def test_dispatch_and_reply_drive_the_states(self):
        task = self.client().send("Review it.")
        tid, seq = self.tid(), self.seq_of(task)
        board_task = task["metadata"]["board"]["task"]
        boardlib.task_claim(self.conn, "claude:task1", "claude", board_task)          # what dispatch does
        self.assertEqual(self.client().get(task["id"])["status"]["state"], "working")
        boardlib.post(self.conn, tid, "claude:task1", ANSWER, "answer", seq, "a2a:alice")
        done = self.client().get(task["id"])
        self.assertEqual(done["status"]["state"], "completed")
        self.assertEqual(a2a.text_of(done), ANSWER)
        self.assertEqual([m["role"] for m in done["history"]], ["user", "agent"])
        self.assertEqual(done["metadata"]["board"]["status"], "done", "the reply closed the board task too")

    def test_blocked_fails_and_cancel_withdraws(self):
        task = self.client().send("Review it.")
        boardlib.post(self.conn, self.tid(), "claude#9", "missing: the spec root; default: none", "blocked",
                      self.seq_of(task), "a2a:alice")
        failed = self.client().get(task["id"])
        self.assertEqual(failed["status"]["state"], "failed")
        self.assertEqual(a2a.text_of(failed), "missing: the spec root; default: none")
        self.assertEqual(failed["artifacts"], [])
        second = self.client().send("Review it again.")
        canceled = self.client().cancel(second["id"])
        self.assertEqual(canceled["status"]["state"], "canceled")
        self.assertEqual(canceled["metadata"]["board"]["status"], "cancelled")
        with self.assertRaises(a2a.A2AError) as cm:
            self.client().cancel(second["id"])
        self.assertEqual(cm.exception.code, a2a.NOT_CANCELABLE)
        self.assertEqual(self.client().get(second["id"])["status"]["state"], "canceled")

    def test_a_client_holds_at_most_five_open_turns(self):
        ids = [self.client().send(f"Review part {n}.")["id"] for n in range(boardlib.PEER_OPEN_TASKS)]
        with self.assertRaises(a2a.A2AError) as cm:
            self.client().send("Review part 6.")
        self.assertEqual(cm.exception.code, a2a.INVALID_PARAMS)
        self.assertIn("peer task limit", str(cm.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], boardlib.PEER_OPEN_TASKS)
        self.client().cancel(ids[0])                                           # room again once one closes
        self.assertEqual(self.client().send("Review part 6.")["status"]["state"], "submitted")
        self.assertEqual(self.client(self.bob).send("Mine.")["status"]["state"], "submitted")   # per client topic

    def test_a_client_sees_only_its_own_tasks_and_errors_carry_their_codes(self):
        task = self.client().send("Review it.")
        for call, code in ((lambda: self.client(self.bob).get(task["id"]), a2a.TASK_NOT_FOUND),
                           (lambda: self.client().get("nonsense"), a2a.TASK_NOT_FOUND),
                           (lambda: self.client().rpc("tasks/foo", {}), a2a.METHOD_NOT_FOUND),
                           (lambda: self.client().rpc("message/stream", {}), a2a.UNSUPPORTED),
                           (lambda: self.client().rpc("tasks/pushNotificationConfig/set", {}), a2a.PUSH_UNSUPPORTED),
                           (lambda: self.client().send("x", to="codex"), a2a.INVALID_PARAMS),
                           (lambda: self.client().send("x", mode="write"), a2a.INVALID_PARAMS),
                           (lambda: self.client().send("x", context="a2a:bob"), a2a.INVALID_PARAMS),
                           (lambda: self.client().rpc("message/send", {"message": {"parts": []}}), a2a.INVALID_PARAMS)):
            with self.subTest(code=code), self.assertRaises(a2a.A2AError) as cm:
                call()
            self.assertEqual(cm.exception.code, code)
        status, resp = self.raw(b"{not json", self.token)
        self.assertEqual((status, resp["error"]["code"]), (200, a2a.PARSE_ERROR))
        status, resp = self.raw(json.dumps({"method": "tasks/get"}).encode(), self.token)
        self.assertEqual(resp["error"]["code"], a2a.INVALID_REQUEST)
        status, resp = self.raw(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": task["id"]}},
                                            {"jsonrpc": "2.0", "id": 2, "method": "nope"}]).encode(), self.token)
        self.assertEqual([r.get("id") for r in resp], [1, 2])
        self.assertEqual(resp[0]["result"]["id"], task["id"])
        self.assertEqual(resp[1]["error"]["code"], a2a.METHOD_NOT_FOUND)

    def test_blocking_send_waits_for_the_reply(self):
        self.reply_when_asked("answer", ANSWER)
        task = self.client().send("Review it.", blocking=True)
        self.assertEqual(task["status"]["state"], "completed")
        self.assertEqual(a2a.text_of(task), ANSWER)

    def test_client_refuses_a_redirect_so_the_token_never_follows_one(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        hits: list[str] = []

        class Redirector(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):  # noqa: D102
                pass

        srv = HTTPServer(("127.0.0.1", 0), Redirector)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(a2a.A2AError) as cm:
                a2a.Client(f"http://127.0.0.1:{srv.server_port}", "secret", timeout=5).card()
            self.assertEqual(cm.exception.code, 302)
            self.assertEqual(hits, ["/.well-known/agent-card.json"], "one request, no second hop")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_add_client_rejects_bad_names_and_keeps_the_token_file_private(self):
        for bad in ("a b", "x/y", "", "+", "task7"):                      # task7 would be the turn run a2a:task7
            with self.subTest(bad=bad), self.assertRaises((ValueError, boardlib.BoardError)):
                a2a.add_client(bad)
        self.assertEqual(oct(a2a.CLIENTS.stat().st_mode & 0o777), "0o600")
        self.assertEqual(sorted(a2a.load_clients()), ["alice", "bob"])

    # -- the CLI client and the adapter
    def cli_env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BRIDGE_")}
        env.update({"HOME": str(self.home), "USER": "tester", a2a.TOKEN_VAR: self.token})
        env.update(extra)
        return env

    def test_send_cli_prints_json_lines_the_adapter_reads(self):
        self.reply_when_asked("answer", ANSWER)
        p = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "send", "--url", self.url, "--to", "claude",
                            "--timeout", "20", "--poll", "0.2", "--", "Review it."],
                           capture_output=True, text=True, env=self.cli_env(), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        events = [json.loads(ln) for ln in p.stdout.splitlines()]
        self.assertEqual(events[0]["type"], "card")
        self.assertEqual(events[0]["agent"], "test-bridge")
        self.assertEqual([e["state"] for e in events if e["type"] == "task"][0], "submitted")
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual((events[-1]["state"], events[-1]["text"]), ("completed", ANSWER))
        parsed = ADAPTERS["a2a"].parse(p.stdout)
        self.assertEqual((parsed.result, parsed.session, parsed.is_error), (ANSWER, "a2a:alice", False))
        self.assertIn("state=completed", parsed.extra)
        self.assertEqual(json.loads(a2a.LAST_CONTEXT.read_text())[self.url], "a2a:alice")
        # the next send with --context last continues the same context (one topic per client)
        self.reply_when_asked("blocked", "missing: the build log; default: none", after=1)
        p2 = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "send", "--url", self.url, "--context", "last",
                             "--timeout", "20", "--poll", "0.2", "--", "And the build?"],
                            capture_output=True, text=True, env=self.cli_env(), timeout=60)
        self.assertEqual(p2.returncode, 1, p2.stderr)
        self.assertIn("[a2a] failed: missing: the build log", p2.stderr)
        bad = ADAPTERS["a2a"].parse(p2.stdout)
        self.assertTrue(bad.is_error)
        self.assertIn("A2A task failed: missing: the build log", bad.result)
        self.assertEqual(ADAPTERS["a2a"].parse("").is_error, True)
        no_url = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "send", "--", "x"], capture_output=True,
                                text=True, env={k: v for k, v in self.cli_env().items() if k != a2a.URL_VAR})
        self.assertEqual(no_url.returncode, 2)

    def test_ask_agent_a2a_reaches_the_server_through_the_real_binary(self):
        self.reply_when_asked("answer", ANSWER)
        env = self.cli_env(**{a2a.URL_VAR: self.url})
        p = subprocess.run([sys.executable, str(BIN / "ask-agent"), "a2a", "--no-board", "-d", str(self.work),
                            "-t", "30", "Review it."], capture_output=True, text=True, env=env, timeout=90)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(ANSWER, p.stdout)
        self.assertIn("[bridge] agent=a2a session=a2a:alice", p.stdout)
        self.assertNotIn(self.token, p.stdout + p.stderr, "the token never appears in output or argv")
        dry = subprocess.run([sys.executable, str(BIN / "ask-agent"), "a2a", "--dry-run", "-d", str(self.work), "q"],
                             capture_output=True, text=True, env=env)
        self.assertNotIn(self.token, dry.stdout)
        self.assertEqual(json.loads(dry.stdout)["binary"], str(BIN / "bridge-a2a"))
        with_dir = subprocess.run([sys.executable, str(BIN / "ask-agent"), "a2a", "--dry-run", "-d", str(self.work),
                                   "--add-dir", str(self.home), "q"], capture_output=True, text=True, env=env)
        self.assertNotIn("--add-dir", json.loads(with_dir.stdout)["argv"], "a local path grant never leaves the machine")

    def turn_to_a2a(self, shape: str = "one line") -> tuple[int, int, int]:
        """A local room with a turn for the remote, claimed the way dispatch claims it: (topic id, request seq, task id)."""
        tid, _ = boardlib.open_topic(self.conn, "room:r", "user:tester", False)
        seq = boardlib.post(self.conn, tid, "user:tester", f"shape: {shape}\n\nReview it.", "request", to="a2a")
        task = self.conn.execute("SELECT id FROM tasks WHERE request_seq=? AND topic_id=?", (seq, tid)).fetchone()["id"]
        self.conn.execute("UPDATE tasks SET status='claimed', claimed_by=? WHERE id=?", (f"a2a:task{task}", task))
        self.conn.commit()
        return tid, seq, task

    def dispatched(self, task: int, *args: str) -> subprocess.CompletedProcess:
        env = self.cli_env(**{a2a.URL_VAR: self.url, "AGENT_BRIDGE_TASK": str(task),
                              "AGENT_BRIDGE_IDENTITY": f"a2a:task{task}"})
        return subprocess.run([sys.executable, str(BIN / "ask-agent"), "a2a", "-d", str(self.work), "-t", "30", *args,
                               "Review it."], capture_output=True, text=True, env=env, timeout=90)

    def test_a_dispatched_turn_to_a2a_closes_its_request_through_the_bridge(self):
        tid, seq, task = self.turn_to_a2a()
        self.reply_when_asked("answer", ANSWER)
        p = self.dispatched(task)
        self.assertEqual(p.returncode, 0, p.stderr)
        (reply,) = self.conn.execute("SELECT author, kind, body, addressee FROM messages WHERE topic_id=? AND reply_to=?",
                                     (tid, seq)).fetchall()
        self.assertEqual(tuple(reply), (f"a2a:task{task}", "answer", ANSWER, "user:tester"))
        self.assertEqual(boardlib.open_requests_to(self.conn, tid, f"a2a:task{task}"), 0)
        row = self.conn.execute("SELECT status, result FROM tasks WHERE id=?", (task,)).fetchone()
        self.assertEqual((row["status"], row["result"]), ("done", ANSWER))
        self.assertNotIn(self.token, p.stdout + p.stderr)

    def test_a_remote_blocked_fails_the_turn_with_a_missing_line(self):
        tid, seq, task = self.turn_to_a2a()
        self.reply_when_asked("blocked", "missing: the build log; default: none")
        p = self.dispatched(task)
        self.assertEqual(p.returncode, 1, p.stderr)
        (reply,) = self.conn.execute("SELECT author, kind, body FROM messages WHERE topic_id=? AND reply_to=?",
                                     (tid, seq)).fetchall()
        self.assertEqual((reply["author"], reply["kind"]), (f"a2a:task{task}", "blocked"))
        self.assertTrue(reply["body"].startswith("missing: "), reply["body"])
        self.assertIn("the build log", reply["body"])
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()["status"], "failed")

    def test_serve_cli_refuses_what_it_must(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BRIDGE_")} | {"HOME": str(self.home)}
        for args, text in ((["--bind", "0.0.0.0", "--port", "0"], "non-loopback"),
                           (["--peers", "bogus"], "--peers"),
                           (["--peers", "claude,a2a"], "a2a")):      # a relay: a client's text, the operator's token
            with self.subTest(args=args):
                home = tempfile.mkdtemp()                         # no clients.json there
                p = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "serve", "-d", str(self.work), *args],
                                   capture_output=True, text=True, env=env | {"HOME": home}, timeout=30)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn(text, p.stderr)


if __name__ == "__main__":
    unittest.main()
