"""A2A transport — the bridge reachable over HTTP as one Agent2Agent (A2A, JSON-RPC 2.0) agent, and a client that
sends a task to any A2A agent. Standard library only; docs/PROTOCOL.md §10.4 is the spec of the mapping.

Inbound (`bridge-a2a serve`): an authenticated client `<name>` (bearer token from state/a2a/clients.json) owns one
board topic `a2a:<name>` and is its leader. `message/send` becomes one board `request` on that topic, addressed to a
family the operator allowed (`--peers`, the egress gate) — a *turn*, queued as a task that only a human's
`bridge-board dispatch` starts; `tasks/get` reads the task's state off the board (submitted · working · completed ·
failed · canceled) and the closing reply is the result; `tasks/cancel` is the requester withdrawing. A client sees
only its own topic, never chooses a directory (the operator's `--dir` is the workspace), and may ask for `write`
only when the operator started the server with `--allow-write`.

Outbound (`bridge-a2a send`, the `a2a` adapter behind `ask-agent a2a`): one `message/send`, then `tasks/get` until a
terminal state, printed as JSON Lines (`card` · `task` per state change · `result`) so the adapter's parse() and a
human read the same stream. The token travels in the environment (AGENT_BRIDGE_A2A_TOKEN), never on a command line.
"""
from __future__ import annotations

import hmac
import http.client
import json
import os
import re
import secrets
import ssl
import stat
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import boardlib

from .core import STATE

A2A_VERSION = "0.3.0"           # the Agent2Agent protocol version the card announces
BRIDGE_VERSION = "1.0"          # this transport's own version (the envelope stays v1: an A2A task is one request)
A2A_DIR = STATE / "a2a"
CLIENTS = A2A_DIR / "clients.json"            # {"<client name>": "<bearer token>"} — mode 0600
SERVER_STATE = A2A_DIR / "server.json"        # the running server: pid, url, families, workdir
LAST_CONTEXT = A2A_DIR / "last-context.json"  # {url: contextId} — what `send --context last` resumes
WELL_KNOWN = ("/.well-known/agent-card.json", "/.well-known/agent.json")   # A2A 0.3 name, then the older one
TERMINAL = ("completed", "canceled", "failed", "rejected")
MAX_BODY = 1 << 20
BATCH_MAX = 32                                   # JSON-RPC requests per batch: one 1 MiB batch is not ten thousand sends
CONN_TIMEOUT = 30                                # seconds a connection may stay silent (a half-sent request, an idle keep-alive)
CONTENT_LENGTH = re.compile(r"[0-9]{1,7}")       # a plain decimal: int() would also take "1_0", "+1", " 1"
URL_VAR, TOKEN_VAR = "AGENT_BRIDGE_A2A_URL", "AGENT_BRIDGE_A2A_TOKEN"
# JSON-RPC 2.0 and A2A error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, NOT_CANCELABLE, PUSH_UNSUPPORTED, UNSUPPORTED = -32001, -32002, -32003, -32004


class RpcError(Exception):
    """A JSON-RPC / A2A error the server answers with (code, message)."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    def as_dict(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


class A2AError(Exception):
    """What the client raises: an HTTP failure, an unreachable host, or the server's JSON-RPC error."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------------- shapes
def topic_for(client: str) -> str:
    """The client's identity and its topic are the same string: a2a:<name> (boardlib.IDENTITY allows it)."""
    return f"a2a:{client}"


def task_id(topic: str, seq: int) -> str:
    return f"{topic}/{seq}"


def split_task_id(tid: Any) -> tuple[str, int]:
    topic, _, seq = str(tid or "").rpartition("/")
    if not topic or not seq.isdigit():
        raise RpcError(TASK_NOT_FOUND, f"no such task: {tid!r}")
    return topic, int(seq)


def iso(ts: float | None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts else time.time()))


def text_parts(message: Any) -> str:
    """The text of an A2A message: its text parts joined (A2A 0.3 says `kind`, older clients `type`)."""
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or []
    return "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)
                     and (p.get("kind") or p.get("type")) == "text" and p.get("text"))


def message(role: str, text: str, message_id: str | None = None, task: str | None = None,
            context: str | None = None) -> dict:
    m: dict = {"kind": "message", "role": role, "parts": [{"kind": "text", "text": text}],
               "messageId": message_id or secrets.token_hex(8)}
    if task:
        m["taskId"] = task
    if context:
        m["contextId"] = context
    return m


def strip_headers(body: str) -> str:
    """The body after its `key: value` header block (boardlib.header reads the block; this returns the rest)."""
    lines = body.splitlines()
    n = len(boardlib.header(body))
    rest = lines[n:]
    while rest and not rest[0].strip():
        rest.pop(0)
    return "\n".join(rest)


def agent_card(name: str, url: str, families: list[str], description: str | None = None,
               auth: bool = True) -> dict:
    """The A2A agent card: what this bridge is, where it listens, and the one skill it offers."""
    card: dict = {
        "name": name,
        "description": description or (
            "agent-bridge: a task sent here becomes a request on the shared board of the coding agents on this "
            "machine, addressed to one of its peer families; a human dispatches it and the peer's reply is the result. "
            f"Families reachable through this card: {', '.join(families)}. Read-only by default."),
        "url": url,
        "version": BRIDGE_VERSION,
        "protocolVersion": A2A_VERSION,
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{
            "id": "bridge-request",
            "name": "Ask a peer coding agent on this machine",
            "description": ("One request → one peer run, read-only unless the operator allows writes. Message "
                            "metadata: `to` (one of the families above), `mode` (ask | plan | write), `shape` (what "
                            "the answer must look like: table, file:line, json, diff, decision, command). The task "
                            "waits in `submitted` until a human dispatches it; poll tasks/get."),
            "tags": ["code-review", "second-opinion", "agent-bridge"] + list(families),
            "examples": ["Review src/payment/ for retry bugs. Answer as file:line findings."],
        }],
    }
    if auth:
        card["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer"}}
        card["security"] = [{"bearer": []}]
    return card


# ----------------------------------------------------------------------------- clients (tokens)
CLIENT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")   # a2a:<name> stays a board identity; no '/' (task ids)
TURN_NAME = re.compile(r"task\d+")                        # a2a:task<N> is the dispatched run of a turn, never a client


def load_clients(path: Path | None = None) -> dict[str, str]:
    # the path is looked up at call time (not a default bound at import), so a test's patched CLIENTS is honoured
    try:
        data = json.loads((path or CLIENTS).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # only a non-empty string is a token: an empty or null value (a hand-edited file) must authenticate nobody
    return {str(k): v for k, v in data.items() if isinstance(v, str) and v.strip()}


def save_clients(clients: dict[str, str], path: Path | None = None) -> None:
    path = path or CLIENTS
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clients, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


def add_client(name: str, path: Path | None = None) -> str:
    """A new bearer token for `name` — a lowercase slug (letters, digits, `.`, `_`, `-`), so that a2a:<name> is a
    valid board identity and `<topic>/<seq>` task ids stay unambiguous — and never `task<N>`, the identity of a
    dispatched turn run (`a2a:task<N>`), which would let a client answer as the bridge's own run."""
    if not CLIENT_NAME.fullmatch(name or ""):
        raise ValueError(f"a client name is a lowercase slug like laptop or bob.example (got {name!r})")
    if TURN_NAME.fullmatch(name):
        raise ValueError(f"{name!r} is the name of a dispatched turn run (a2a:{name}); pick another")
    boardlib.identity(topic_for(name), "client name")
    clients = load_clients(path)
    token = secrets.token_urlsafe(32)
    clients[name] = token
    save_clients(clients, path)
    return token


def authenticate(header: str | None, clients: dict[str, str]) -> str | None:
    """The client name a `Authorization: Bearer <token>` header proves, or None."""
    if not header or not header.lower().startswith("bearer "):
        return None
    given = header[7:].strip()
    if not given:
        return None
    for name, token in clients.items():
        if token and hmac.compare_digest(given.encode(), token.encode()):
            return name
    return None


# ----------------------------------------------------------------------------- server: board mapping
class Config:
    def __init__(self, name: str, url: str, families: list[str], workdir: str, clients: dict[str, str],
                 allow_write: bool = False, max_block: int = 60):
        self.name, self.url, self.families, self.workdir = name, url, list(families), str(workdir)
        self.clients, self.allow_write, self.max_block = dict(clients), allow_write, max_block


def ensure_topic(conn, client: str) -> int:
    """The client's topic, created on first use with the client as its leader (so it may call turns there)."""
    me = topic_for(client)
    tid = boardlib.topic_id(conn, me, me)
    if conn.execute("SELECT closed_at FROM topics WHERE id=?", (tid,)).fetchone()["closed_at"] is not None:
        boardlib.reopen_topic(conn, me)
    if me not in {m["agent"] for m in boardlib.members(conn, tid)}:
        boardlib.member_add(conn, tid, me, "leader")
    boardlib.heartbeat(conn, me, "remote", "a2a client")
    return tid


def submit(conn, cfg: Config, client: str, params: Any) -> dict:
    """message/send → one board request (a turn for the family `to` names)."""
    params = params if isinstance(params, dict) else {}
    msg = params.get("message")
    if not isinstance(msg, dict):
        raise RpcError(INVALID_PARAMS, "params.message is required")
    text = text_parts(msg).strip()
    if not text:
        raise RpcError(INVALID_PARAMS, "the message has no text part")
    meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    to = str(meta.get("to") or cfg.families[0])
    if to not in cfg.families:
        raise RpcError(INVALID_PARAMS, f"metadata.to must be one of {cfg.families}")
    mode = str(meta.get("mode") or "ask")
    if mode not in ("ask", "plan", "write") or (mode == "write" and not cfg.allow_write):
        raise RpcError(INVALID_PARAMS, "metadata.mode must be ask or plan" + (" or write" if cfg.allow_write else
                                                                              " (this server does not accept write)"))
    shape = " ".join(str(meta.get("shape") or "text").split()) or "text"
    topic = topic_for(client)
    context = msg.get("contextId") or params.get("contextId")
    if context and context != topic:
        raise RpcError(INVALID_PARAMS, f"contextId must be {topic!r}: one context per client")
    head = f"shape: {shape}\nmode: {mode}\ndir: {cfg.workdir}\n"
    if msg.get("messageId"):
        head += f"message-id: {' '.join(str(msg['messageId']).split())}\n"
    tid = ensure_topic(conn, client)
    try:
        seq = boardlib.post(conn, tid, topic, head + "\n" + text, "request", None, to)
    except boardlib.BoardError as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from exc
    view = task_view(conn, client, topic, seq)
    if (params.get("configuration") or {}).get("blocking"):
        deadline = time.time() + cfg.max_block
        while view["status"]["state"] not in TERMINAL and time.time() < deadline:
            time.sleep(0.5)
            view = task_view(conn, client, topic, seq)
    return view


def task_view(conn, client: str, topic: str, seq: int) -> dict:
    """The A2A Task for request `seq` on the client's topic, read off the board: the request is the task, the board
    task's status says submitted/working, the closing reply says completed/failed, a withdrawal says canceled."""
    me = topic_for(client)
    if topic != me:
        raise RpcError(TASK_NOT_FOUND, f"no such task: {task_id(topic, seq)}")
    trow = conn.execute("SELECT id FROM topics WHERE name=?", (topic,)).fetchone()
    req = trow and conn.execute("SELECT seq, author, body, created_at, addressee FROM messages WHERE topic_id=? AND "
                                "seq=? AND kind='request' AND author=?", (trow["id"], seq, me)).fetchone()
    if not req:
        raise RpcError(TASK_NOT_FOUND, f"no such task: {task_id(topic, seq)}")
    tid = trow["id"]
    head = boardlib.header(req["body"])
    fam = req["addressee"]
    task = conn.execute("SELECT id, status, result, updated_at FROM tasks WHERE topic_id=? AND request_seq=?",
                        (tid, seq)).fetchone()
    replies = conn.execute("SELECT seq, author, kind, body, created_at FROM messages WHERE topic_id=? AND reply_to=? "
                           "AND kind IN ('answer','done','blocked') ORDER BY seq", (tid, seq)).fetchall()
    closer = next((r for r in replies if r["author"] == me or (boardlib.family(r["author"]) == fam and
                                                                ("#" in r["author"] or boardlib.is_turn_run(r["author"])))),
                  None)
    result_text, note, ts = "", "", req["created_at"]
    if closer is not None and closer["author"] == me:
        state = "canceled"
        ts = closer["created_at"]
    elif closer is not None:
        state = "completed" if closer["kind"] in ("answer", "done") else "failed"
        result_text, ts = closer["body"], closer["created_at"]
    elif task is not None:
        state = {"queued": "submitted", "claimed": "working", "done": "completed", "failed": "failed",
                 "cancelled": "canceled"}.get(task["status"], "unknown")
        if task["status"] in ("done", "failed"):
            result_text = task["result"] or ""
        if task["updated_at"]:
            ts = task["updated_at"]
    else:
        state = "submitted"
    if state == "failed":
        note, result_text = result_text or "the peer could not answer", ""
    tid_str = task_id(topic, seq)
    status: dict = {"state": state, "timestamp": iso(ts)}
    if note:
        status["message"] = message("agent", note, task=tid_str, context=topic)
    history = [message("user", strip_headers(req["body"]), head.get("message-id"), tid_str, topic)]
    if closer is not None and closer["author"] != me:
        history.append(message("agent", closer["body"], task=tid_str, context=topic))
    return {"id": tid_str, "contextId": topic, "kind": "task", "status": status,
            "artifacts": ([{"artifactId": f"{tid_str}/reply", "name": "reply",
                            "parts": [{"kind": "text", "text": result_text}]}] if result_text else []),
            "history": history,
            "metadata": {"to": fam, "mode": head.get("mode"), "shape": head.get("shape"),
                         "board": {"topic": topic, "seq": seq, "task": task["id"] if task else None,
                                   "status": task["status"] if task else None}}}


def cancel(conn, client: str, topic: str, seq: int) -> dict:
    view = task_view(conn, client, topic, seq)
    if view["status"]["state"] in TERMINAL:
        raise RpcError(NOT_CANCELABLE, f"task is already {view['status']['state']}")
    tid = conn.execute("SELECT id FROM topics WHERE name=?", (topic,)).fetchone()["id"]
    # the requester withdrawing closes its request and cancels the queued task (boardlib._settle_turn)
    boardlib.post(conn, tid, topic_for(client), "withdrawn by the client (tasks/cancel)", "done", seq, None)
    return task_view(conn, client, topic, seq)


def dispatch(cfg: Config, client: str, method: str, params: Any) -> Any:
    conn = boardlib.connect()
    try:
        if method == "message/send":
            return submit(conn, cfg, client, params)
        if method == "tasks/get":
            topic, seq = split_task_id((params or {}).get("id") if isinstance(params, dict) else None)
            return task_view(conn, client, topic, seq)
        if method == "tasks/cancel":
            topic, seq = split_task_id((params or {}).get("id") if isinstance(params, dict) else None)
            return cancel(conn, client, topic, seq)
        if method in ("message/stream", "tasks/resubscribe"):
            raise RpcError(UNSUPPORTED, "streaming is not supported: use message/send, then tasks/get")
        if method.startswith("tasks/pushNotificationConfig"):
            raise RpcError(PUSH_UNSUPPORTED, "push notifications are not supported")
        raise RpcError(METHOD_NOT_FOUND, f"unknown method {method!r}")
    finally:
        conn.close()


def handle(cfg: Config, client: str, req: Any) -> dict:
    """One JSON-RPC request → one response object (errors included; the transport status stays 200)."""
    rid = req.get("id") if isinstance(req, dict) else None
    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
        return {"jsonrpc": "2.0", "id": rid, "error": RpcError(INVALID_REQUEST, "not a JSON-RPC 2.0 request").as_dict()}
    try:
        return {"jsonrpc": "2.0", "id": rid, "result": dispatch(cfg, client, req["method"], req.get("params"))}
    except RpcError as exc:
        return {"jsonrpc": "2.0", "id": rid, "error": exc.as_dict()}
    except Exception as exc:  # noqa: BLE001 — never a traceback to a remote client
        return {"jsonrpc": "2.0", "id": rid, "error": RpcError(INTERNAL, f"{type(exc).__name__}: {exc}").as_dict()}


class Handler(BaseHTTPRequestHandler):
    server_version = f"agent-bridge-a2a/{BRIDGE_VERSION}"
    protocol_version = "HTTP/1.1"

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    def setup(self) -> None:
        # one silent connection must not hold a thread and a file descriptor forever: the socket times out, and
        # BaseHTTPRequestHandler turns a timeout on the request line into a close
        self.timeout = getattr(self.server, "conn_timeout", CONN_TIMEOUT)
        super().setup()
        if isinstance(self.connection, ssl.SSLSocket):
            # the TLS handshake runs here, on this connection's thread and under its timeout — never on the accept
            # loop, where one client that never sends a ClientHello would stall every other connection
            self.connection.do_handshake()

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    def _send(self, status: int, obj: Any, headers: dict[str, str] | None = None, close: bool = False) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if status >= 400 or close:
            # an error leaves the body unread (401, 413) or half-trusted (a parse error): on a persistent connection
            # those bytes would be parsed as the next request, so the connection ends here
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] in WELL_KNOWN:          # the card is public; everything else needs a token
            self._send(200, agent_card(self.cfg.name, self.cfg.url, self.cfg.families, auth=bool(load_clients())))
            return
        self._send(404, {"error": "not found", "agent_card": WELL_KNOWN[0]})

    def do_POST(self) -> None:
        # the token table is read per request: `bridge-a2a client add|remove` takes effect without a restart
        client = authenticate(self.headers.get("Authorization"), load_clients())
        if client is None:
            self._send(401, {"error": "unauthorized: Authorization: Bearer <token> (bridge-a2a client add <name>)"},
                       {"WWW-Authenticate": "Bearer"})
            return
        raw_len = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") or raw_len is None:
            self._send(411, {"error": "a body needs Content-Length; chunked transfer is not accepted"})
            return
        if not CONTENT_LENGTH.fullmatch(raw_len):
            self._send(400, {"error": "Content-Length must be a plain decimal"})
            return
        length = int(raw_len)
        if length > MAX_BODY:
            self._send(413, {"error": f"body must be 0..{MAX_BODY} bytes"})
            return
        try:
            body = self.rfile.read(length)
        except TimeoutError:
            self._send(408, {"error": "the body did not arrive in time"})
            return
        try:
            req = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(200, {"jsonrpc": "2.0", "id": None, "error": RpcError(PARSE_ERROR, "parse error").as_dict()},
                       close=True)
            return
        if isinstance(req, list):
            if not 1 <= len(req) <= BATCH_MAX:
                self._send(200, {"jsonrpc": "2.0", "id": None, "error": RpcError(
                    INVALID_REQUEST, f"a batch holds 1..{BATCH_MAX} requests").as_dict()})
                return
            self._send(200, [handle(self.cfg, client, r) for r in req])
            return
        self._send(200, handle(self.cfg, client, req))


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """A failed TLS handshake, a timed-out silent connection, a client gone mid-response: one line, no
        traceback (socketserver's default). Anything that is not an OSError is a bug and keeps its traceback."""
        exc = sys.exc_info()[1]
        if not isinstance(exc, OSError):
            super().handle_error(request, client_address)
        elif not getattr(self, "quiet", False):
            print(f"[a2a] {client_address[0]}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def make_server(cfg: Config, bind: str, port: int, quiet: bool = False, conn_timeout: float = CONN_TIMEOUT,
                cert: str | None = None, key: str | None = None) -> ThreadingHTTPServer:
    srv = _Server((bind, port), Handler)
    srv.cfg = cfg  # type: ignore[attr-defined]
    srv.quiet = quiet  # type: ignore[attr-defined]
    srv.conn_timeout = conn_timeout  # type: ignore[attr-defined]
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        # do_handshake_on_connect=False: accept() hands back an un-handshaken socket and Handler.setup finishes it
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    return srv


# ----------------------------------------------------------------------------- client
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the bearer token to whatever host the remote names: refuse it (HTTPError 3xx)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Client:
    def __init__(self, url: str, token: str | None = None, timeout: float = 30):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def _http(self, path: str, data: dict | None = None) -> Any:
        headers = {"Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url + path, data=body, headers=headers, method="POST" if data else "GET")
        try:
            with _OPENER.open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise A2AError(f"{self.url}: unauthorized — set {TOKEN_VAR} to a token the server knows", 401) from exc
            if 300 <= exc.code < 400:
                raise A2AError(f"{self.url}{path}: redirect ({exc.code}) refused — the token stays with the URL you "
                               f"named; point {URL_VAR} at the final address", exc.code) from exc
            raise A2AError(f"{self.url}{path}: HTTP {exc.code} {exc.reason}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise A2AError(f"cannot reach {self.url}: {exc.reason}") from exc
        except (OSError, http.client.HTTPException) as exc:   # socket timeout, reset, half-closed: still "unreachable"
            raise A2AError(f"cannot reach {self.url}: {exc.__class__.__name__}: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise A2AError(f"{self.url}{path}: not JSON ({exc})") from exc

    def card(self) -> dict:
        last: A2AError | None = None
        for path in WELL_KNOWN:
            try:
                return self._http(path)
            except A2AError as exc:
                if exc.code != 404:          # unreachable, refused, redirected: that error, with its code, is the answer
                    raise
                last = exc
        raise A2AError(f"no agent card at {self.url}{WELL_KNOWN[0]} ({last})", 404)

    def rpc(self, method: str, params: dict) -> Any:
        resp = self._http("/", {"jsonrpc": "2.0", "id": secrets.token_hex(8), "method": method, "params": params})
        if not isinstance(resp, dict):
            raise A2AError(f"{method}: malformed response")
        if "error" in resp:
            err = resp["error"] or {}
            raise A2AError(f"{method}: {err.get('message', 'error')} (code {err.get('code')})", err.get("code"))
        return resp.get("result")

    def send(self, text: str, to: str | None = None, mode: str | None = None, shape: str | None = None,
             context: str | None = None, model: str | None = None, add_dirs: list[str] | None = None,
             blocking: bool = False) -> dict:
        meta = {k: v for k, v in {"to": to, "mode": mode, "shape": shape, "model": model,
                                  "add_dirs": add_dirs or None}.items() if v}
        msg = message("user", text)
        if meta:
            msg["metadata"] = meta
        if context:
            msg["contextId"] = context
        return self.rpc("message/send", {"message": msg,
                                         "configuration": {"blocking": blocking, "acceptedOutputModes": ["text/plain"]}})

    def get(self, tid: str) -> dict:
        return self.rpc("tasks/get", {"id": tid})

    def cancel(self, tid: str) -> dict:
        return self.rpc("tasks/cancel", {"id": tid})

    def wait(self, tid: str, deadline: float, poll: float = 2.0):
        """Yield the task on every state change until a terminal state or the deadline (the last yield may be
        non-terminal: the caller reads the state)."""
        last = None
        while True:
            task = self.get(tid)
            state = (task.get("status") or {}).get("state")
            if state != last:
                last = state
                yield task
            if state in TERMINAL or time.time() >= deadline:
                return
            time.sleep(min(poll, max(0.0, deadline - time.time())))


def text_of(task: dict) -> str:
    """The result text of a task: its artifacts' text parts, else its status message."""
    texts = [p.get("text", "") for a in task.get("artifacts") or [] for p in a.get("parts") or []
             if isinstance(p, dict) and (p.get("kind") or p.get("type")) == "text"]
    text = "\n\n".join(t for t in texts if t)
    return text or text_parts((task.get("status") or {}).get("message"))
