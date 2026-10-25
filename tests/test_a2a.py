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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import boardlib
from bridgelib import a2a, ledger
from bridgelib.adapters import ADAPTERS

ANSWER = "The retry loop in src/retry.py:12 never backs off."


class Fake:
    """A scripted remote A2A endpoint for the client side: `on[method]` is the JSON-RPC result (a dict, an RpcError,
    or `(delay, result)`); GET /.well-known/* serves `card`; `drop` closes every connection unanswered. Records each
    request as (method or GET, path, headers)."""

    def __init__(self, test: unittest.TestCase):
        self.on: dict = {}
        self.card: object = {"name": "fake", "protocolVersion": a2a.A2A_VERSION, "url": ""}
        self.drop = False
        self.seen: list[tuple[str, str, dict]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _drop(self):
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)

            def do_GET(self):
                fake.seen.append(("GET", self.path, dict(self.headers)))
                if fake.drop:
                    return self._drop()
                self._json(fake.card)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.seen.append((body.get("method"), self.path, dict(self.headers)))
                if fake.drop:
                    return self._drop()
                r = fake.on.get(body.get("method"))
                if callable(r):
                    r = r(body)
                if isinstance(r, tuple):
                    time.sleep(r[0])
                    r = r[1]
                if isinstance(r, a2a.RpcError):
                    self._json({"jsonrpc": "2.0", "id": body.get("id"), "error": r.as_dict()})
                else:
                    self._json({"jsonrpc": "2.0", "id": body.get("id"), "result": r})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        self.card["url"] = self.url
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        test.addCleanup(self.srv.server_close)
        test.addCleanup(self.srv.shutdown)

    def methods(self) -> list[str]:
        return [m for m, _, _ in self.seen]

    @staticmethod
    def task(state: str, text: str = "", tid: str = "ctx/1") -> dict:
        t = {"id": tid, "contextId": "ctx", "kind": "task", "status": {"state": state}}
        if text:
            t["status"]["message"] = a2a.message("agent", text)
        return t


class Server(unittest.TestCase):
    """A server thread on an ephemeral loopback port, a board and an a2a state dir under a temporary HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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
        for p in self.patches:                           # cleanups run last-in first-out: the replier threads are
            p.start()                                    # joined, then the server and the board close, then the
            self.addCleanup(p.stop)                      # patches are undone — nothing ever reaches the real board
        for var in list(os.environ):
            if var.startswith("AGENT_BRIDGE_"):
                os.environ.pop(var)
        self.token = a2a.add_client("alice")
        self.bob = a2a.add_client("bob")
        self.cfg = a2a.Config("test-bridge", "", ["claude", "cursor"], str(self.work), a2a.load_clients(),
                              allow_write=False, max_block=5)
        self.srv = a2a.make_server(self.cfg, "127.0.0.1", 0, quiet=True)
        self.addCleanup(self.srv.server_close)
        self.url = self.cfg.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.conn = boardlib.connect()
        self.addCleanup(self.conn.close)
        self.stop, self.repliers = threading.Event(), []
        self.addCleanup(self.join_repliers)

    def join_repliers(self) -> None:
        self.stop.set()
        for t in self.repliers:
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "a replier thread outlived its test")

    def client(self, token: str | None = "alice") -> a2a.Client:
        return a2a.Client(self.url, self.token if token == "alice" else token, timeout=10)   # > max_block

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
        """A stand-in for the dispatched run: wait for the next request on alice's topic, answer it. The thread
        gives up when the test ends (join_repliers), so it can never post after the board patches are undone."""
        def run():
            conn = boardlib.connect()
            try:
                deadline = time.time() + 20
                while time.time() < deadline and not self.stop.is_set():
                    row = conn.execute("SELECT m.seq, m.topic_id FROM messages m JOIN topics t ON t.id=m.topic_id "
                                       "WHERE t.name='a2a:alice' AND m.kind='request' AND m.seq>? ORDER BY m.seq "
                                       "LIMIT 1", (after,)).fetchone()
                    if row:
                        boardlib.post(conn, row["topic_id"], author, body, kind, row["seq"], "a2a:alice")
                        break
                    time.sleep(0.1)
            finally:
                conn.close()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.repliers.append(t)
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

    def test_removing_a_client_revokes_its_token_without_a_restart(self):
        clients = a2a.load_clients()
        del clients["bob"]
        a2a.save_clients(clients)
        with self.assertRaises(a2a.A2AError) as cm:
            self.client(self.bob).get("a2a:bob/1")
        self.assertEqual(cm.exception.code, 401)
        bob_again = a2a.add_client("bob")                 # and a new token works on the next request, no restart
        with self.assertRaises(a2a.A2AError) as cm:
            self.client(bob_again).get("a2a:bob/1")
        self.assertEqual(cm.exception.code, a2a.TASK_NOT_FOUND)   # past the token check: there is simply no task 1

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
            def do_GET(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
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

    def test_a_stalled_or_dropped_connection_is_an_a2a_error_not_a_traceback(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)                                      # accepts, never answers: the client's socket timeout fires
        self.addCleanup(srv.close)
        with self.assertRaises(a2a.A2AError) as cm:
            a2a.Client(f"http://127.0.0.1:{srv.getsockname()[1]}", "t", timeout=0.3).card()
        self.assertIn("cannot reach", str(cm.exception))

    def test_add_client_rejects_bad_names_and_keeps_the_token_file_private(self):
        for bad in ("a b", "x/y", "", "+", "task7"):                      # task7 would be the turn run a2a:task7
            with self.subTest(bad=bad), self.assertRaises((ValueError, boardlib.BoardError)):
                a2a.add_client(bad)
        self.assertEqual(oct(a2a.CLIENTS.stat().st_mode & 0o777), "0o600")
        self.assertEqual(sorted(a2a.load_clients()), ["alice", "bob"])

    # -- the CLI client and the adapter
    def cli_env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("AGENT_BRIDGE_") and not k.lower().endswith("_proxy")}   # loopback, no proxy
        env.update({"HOME": str(self.home), "USER": "tester", a2a.TOKEN_VAR: self.token})
        env.update(extra)
        return env

    def test_send_cli_prints_json_lines_the_adapter_reads(self):
        self.reply_when_asked("answer", ANSWER)
        p = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "send", "--url", self.url, "--to", "claude",
                            "--timeout", "20", "--poll", "0.2", "--", "Review it."],
                           capture_output=True, text=True, env=self.cli_env(), timeout=60, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        events = [json.loads(ln) for ln in p.stdout.splitlines()]
        self.assertEqual(events[0]["type"], "card")
        self.assertEqual(events[0]["agent"], "test-bridge")
        self.assertEqual(next(e["state"] for e in events if e["type"] == "task"), "submitted")
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
                            capture_output=True, text=True, env=self.cli_env(), timeout=60, check=False)
        self.assertEqual(p2.returncode, 1, p2.stderr)
        self.assertIn("[a2a] failed: missing: the build log", p2.stderr)
        bad = ADAPTERS["a2a"].parse(p2.stdout)
        self.assertTrue(bad.is_error)
        self.assertIn("A2A task failed: missing: the build log", bad.result)
        self.assertEqual(ADAPTERS["a2a"].parse("").is_error, True)
        no_url = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "send", "--", "x"], capture_output=True,
                                text=True, env={k: v for k, v in self.cli_env().items() if k != a2a.URL_VAR},
                                timeout=60, check=False)
        self.assertEqual(no_url.returncode, 2)

    def test_send_refuses_a_second_hop_unless_it_is_the_a2a_run_itself(self):
        before = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        hop = self.cli_env(AGENT_BRIDGE_HOP="1", AGENT_BRIDGE_IDENTITY="cursor#4")   # a Cursor peer with a shell
        p = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "send", "--url", self.url, "--timeout", "5",
                            "--", "Relay this."], capture_output=True, text=True, env=hop, timeout=60)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("hop", p.stderr)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], before, "nothing was sent")
        self.reply_when_asked("answer", ANSWER)
        own = self.cli_env(AGENT_BRIDGE_HOP="1", AGENT_BRIDGE_IDENTITY="a2a#4")      # what ask-agent a2a sets
        p = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "send", "--url", self.url, "--timeout", "20",
                            "--poll", "0.2", "--", "Review it."], capture_output=True, text=True, env=own, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_send_cli_withdraws_the_task_when_its_timeout_passes(self):
        # nobody dispatches: the client gives up, and the request must not stay queued for a human to run later
        p = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "send", "--url", self.url, "--to", "claude",
                            "--timeout", "1", "--poll", "0.2", "--", "Nobody answers this."],
                           capture_output=True, text=True, env=self.cli_env(), timeout=60, check=False)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("[a2a] timeout", p.stderr)
        events = [json.loads(ln) for ln in p.stdout.splitlines()]
        self.assertEqual(events[-1]["state"], "timeout")
        self.assertIn("canceled", [e.get("state") for e in events if e["type"] == "task"])
        tid = events[-1]["task_id"]
        self.assertEqual(self.client().get(tid)["status"]["state"], "canceled")
        row = self.conn.execute("SELECT status FROM tasks WHERE request_seq=?", (self.seq_of({"id": tid}),)).fetchone()
        self.assertEqual(row["status"], "cancelled")

    def test_ask_agent_a2a_reaches_the_server_through_the_real_binary(self):
        self.reply_when_asked("answer", ANSWER)
        env = self.cli_env(**{a2a.URL_VAR: self.url})
        p = subprocess.run([sys.executable, str(BIN / "ask-agent"), "a2a", "--no-board", "-d", str(self.work),
                            "-t", "30", "Review it."], capture_output=True, text=True, env=env, timeout=90, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(ANSWER, p.stdout)
        self.assertIn("[bridge] agent=a2a session=a2a:alice", p.stdout)
        self.assertNotIn(self.token, p.stdout + p.stderr, "the token never appears in output or argv")
        dry = subprocess.run([sys.executable, str(BIN / "ask-agent"), "a2a", "--dry-run", "-d", str(self.work), "q"],
                             capture_output=True, text=True, env=env, timeout=60, check=False)
        self.assertNotIn(self.token, dry.stdout)
        self.assertEqual(json.loads(dry.stdout)["binary"], str(BIN / "bridge-a2a"))
        with_dir = subprocess.run([sys.executable, str(BIN / "ask-agent"), "a2a", "--dry-run", "-d", str(self.work),
                                   "--add-dir", str(self.home), "q"], capture_output=True, text=True, env=env,
                                  timeout=60)
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

    # -- the server's own hygiene
    def test_a_client_added_or_removed_while_the_server_runs_takes_effect_at_once(self):
        carol = a2a.add_client("carol")
        with self.assertRaises(a2a.A2AError) as cm:
            a2a.Client(self.url, carol, timeout=10).get("a2a:carol/1")
        self.assertEqual(cm.exception.code, a2a.TASK_NOT_FOUND, "authenticated: the table was re-read")
        clients = a2a.load_clients()
        del clients["carol"]
        a2a.save_clients(clients)
        with self.assertRaises(a2a.A2AError) as cm:
            a2a.Client(self.url, carol, timeout=10).get("a2a:carol/1")
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(oct(a2a.CLIENTS.parent.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(a2a.CLIENTS.stat().st_mode & 0o777), "0o600")

    def test_a_bad_configuration_is_refused_before_anything_is_posted(self):
        req = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
               "params": {"message": a2a.message("user", "Review it."), "configuration": "yes"}}
        _, resp = self.raw(json.dumps(req).encode(), self.token)
        self.assertEqual(resp["error"]["code"], a2a.INVALID_PARAMS)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0,
                         "a retrying client must not find a task it was told failed")

    def test_internal_errors_and_failure_notes_carry_nothing_local(self):
        with mock.patch.object(a2a, "task_view", side_effect=RuntimeError("/home/op/.agent-bridge/state/x")):
            with self.assertRaises(a2a.A2AError) as cm:
                self.client().get("a2a:alice/1")
        self.assertEqual(cm.exception.code, a2a.INTERNAL)
        self.assertNotIn("/home/op", str(cm.exception))
        self.assertIn("internal error", str(cm.exception))
        task = self.client().send("Review it.")
        board_task = task["metadata"]["board"]["task"]
        boardlib.task_claim(self.conn, "claude:task1", "claude", board_task)
        boardlib.task_done(self.conn, "claude:task1", board_task,
                           f"peer exited 1 (see {self.home}/.agent-bridge/state/runs/x.err)", "failed")
        failed = self.client().get(task["id"])
        self.assertEqual(failed["status"]["state"], "failed")
        note = a2a.text_of(failed)
        self.assertIn("peer exited 1", note)
        self.assertNotIn("(see", note)
        self.assertNotIn(str(self.home), note)

    def test_the_card_declares_bearer_auth_even_with_no_clients_and_odd_ids_get_an_answer(self):
        bare = a2a.Config("bare", "", ["claude"], str(self.work), {}, max_block=1)
        srv = a2a.make_server(bare, "127.0.0.1", 0, quiet=True)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        card = a2a.Client(f"http://127.0.0.1:{srv.server_port}", None, timeout=5).card()
        self.assertEqual(card["securitySchemes"]["bearer"]["scheme"], "bearer")
        body = json.dumps({"jsonrpc": "2.0", "id": "\ud800", "method": "tasks/get", "params": {"id": "x"}}).encode()
        status, resp = self.raw(body, self.token)                            # a lone surrogate in the id
        self.assertEqual((status, resp["error"]["code"]), (200, a2a.TASK_NOT_FOUND))

    def test_ipv6_loopback_is_a_bind_the_server_can_do(self):
        if not socket.has_ipv6:
            self.skipTest("no IPv6")
        try:
            srv = a2a.make_server(self.cfg, "::1", 0, quiet=True)
        except OSError as exc:
            self.skipTest(f"::1 not available here: {exc}")
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        with urllib.request.urlopen(f"http://[::1]:{srv.server_port}{a2a.WELL_KNOWN[0]}", timeout=5) as r:
            self.assertEqual(json.loads(r.read())["name"], "test-bridge")

    def test_a_connection_cap_answers_503_and_frees_up_again(self):
        srv = a2a.make_server(self.cfg, "127.0.0.1", 0, quiet=True, conn_timeout=1, max_connections=2)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_port}{a2a.WELL_KNOWN[0]}"
        hogs = [self.sock(srv), self.sock(srv)]                             # two silent connections hold the slots
        time.sleep(0.3)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(cm.exception.code, 503)
        for h in hogs:
            self.assertTrue(self.closed(h))                                  # the timeout frees them
        with urllib.request.urlopen(url, timeout=5) as r:
            self.assertEqual(r.status, 200)

    def test_stop_and_status_do_not_trust_a_recycled_pid(self):
        a2a.A2A_DIR.mkdir(parents=True, exist_ok=True)
        a2a.SERVER_STATE.write_text(json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:1"}))   # us, not a server
        env = self.cli_env()
        status = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "status"], capture_output=True, text=True,
                                env=env, timeout=60)
        self.assertEqual(status.returncode, 1, status.stdout)
        self.assertIn("no server running", status.stdout)
        stop = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "stop"], capture_output=True, text=True,
                              env=env, timeout=60)
        self.assertEqual(stop.returncode, 0, stop.stderr)                   # and we are still here
        self.assertFalse(a2a.SERVER_STATE.exists(), "stale state is cleaned up")
        a2a.LAST_CONTEXT.write_text("[1, 2]")
        self.assertEqual(a2a.last_context(), {})
        a2a.remember_context("http://x", "ctx-1")
        self.assertEqual(a2a.last_context(), {"http://x": "ctx-1"})
        a2a.remember_context("http://x", None)
        self.assertEqual(a2a.last_context(), {})

    # -- the client against a scripted remote
    def send_to(self, fake: Fake, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "send", "--url", fake.url, "--poll", "0.2",
                               *args, "--", "Review it."], capture_output=True, text=True,
                              env=env or self.cli_env(), timeout=60)

    def test_every_client_failure_is_an_a2a_error_and_the_card_gets_no_token(self):
        fake = Fake(self)
        fake.drop = True                                         # accepts, says nothing, closes
        with self.assertRaises(a2a.A2AError):
            a2a.Client(fake.url, "tok", timeout=5).card()
        fake.drop = False
        fake.card = ["not", "an", "object"]
        with self.assertRaises(a2a.A2AError):
            a2a.Client(fake.url, "tok", timeout=5).card()
        fake.card = {"name": "fake", "protocolVersion": a2a.A2A_VERSION, "url": fake.url}
        self.assertEqual(a2a.Client(fake.url + "/agents/x", "tok", timeout=5).card()["name"], "fake",
                         "the well-known path hangs off the origin, not the endpoint path")
        self.assertEqual(fake.seen[-1][1], a2a.WELL_KNOWN[0])
        self.assertNotIn("Authorization", fake.seen[-1][2], "the public card never sees the token")
        fake.on["tasks/get"] = "a string, not a task"
        with self.assertRaises(a2a.A2AError):
            a2a.Client(fake.url, "tok", timeout=5).get("ctx/1")
        fake.on["tasks/get"] = (3.0, Fake.task("working"))
        slow = a2a.Client(fake.url, "tok", timeout=30, deadline=time.time() + 1)
        t0 = time.time()
        with self.assertRaises(a2a.A2AError):                   # the deadline bounds every call, not the 30 s timeout
            slow.get("ctx/1")
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(fake.seen[-1][1], "/agents/x" if False else "/", "the RPC goes to the URL as given")

    def test_send_treats_a_message_result_as_the_answer_and_stops_on_input_required(self):
        fake = Fake(self)
        fake.on["message/send"] = a2a.message("agent", ANSWER)   # A2A lets a server answer with a Message, no task
        p = self.send_to(fake)
        self.assertEqual(p.returncode, 0, p.stderr)
        result = [json.loads(ln) for ln in p.stdout.splitlines()][-1]
        self.assertEqual((result["type"], result["state"], result["text"]), ("result", "completed", ANSWER))
        self.assertNotIn("tasks/get", fake.methods(), "an answer in hand is not polled for")
        fake.seen.clear()
        fake.on["message/send"] = Fake.task("input-required", "which branch?")
        p = self.send_to(fake, "--timeout", "20")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("[a2a] input-required: which branch?", p.stderr)
        self.assertLessEqual(fake.methods().count("tasks/get"), 1, "the remote waits for a person, not for time")
        self.assertEqual(ADAPTERS["a2a"].parse(p.stdout).is_error, True)

    def test_send_cancels_the_remote_task_at_the_deadline(self):
        fake = Fake(self)
        fake.on["message/send"] = Fake.task("submitted")
        fake.on["tasks/get"] = (10.0, Fake.task("working"))      # a tasks/get that never comes back in time
        fake.on["tasks/cancel"] = Fake.task("canceled")
        t0 = time.time()
        p = self.send_to(fake, "--timeout", "2")
        self.assertLess(time.time() - t0, 6, "every call is bounded by the deadline")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("tasks/cancel", fake.methods(), "a task the client gave up on is withdrawn, not left running")
        result = [json.loads(ln) for ln in p.stdout.splitlines()][-1]
        self.assertEqual((result["type"], result["state"]), ("result", "timeout"))

    def test_send_goes_on_without_a_card_and_ignores_a_proxy_for_the_url_it_was_given(self):
        fake = Fake(self)
        fake.card = None                                         # 200 with `null`: not a card
        fake.on["message/send"] = Fake.task("completed", ANSWER)
        env = self.cli_env(http_proxy="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")   # a dead proxy
        p = self.send_to(fake, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("no agent card", p.stderr)
        self.assertIn(ANSWER, p.stdout)

    def test_serve_cli_refuses_what_it_must(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BRIDGE_")} | {"HOME": str(self.home)}
        for args, text in ((["--bind", "0.0.0.0", "--port", "0"], "non-loopback"),
                           (["--peers", "bogus"], "--peers"),
                           (["--peers", "claude,a2a"], "a2a")):      # a relay: a client's text, the operator's token
            with self.subTest(args=args), tempfile.TemporaryDirectory() as home:   # no clients.json there
                p = subprocess.run([sys.executable, str(BIN / "bridge-a2a"), "serve", "-d", str(self.work), *args],
                                   capture_output=True, text=True, env=env | {"HOME": home}, timeout=30, check=False)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn(text, p.stderr)


if __name__ == "__main__":
    unittest.main()
