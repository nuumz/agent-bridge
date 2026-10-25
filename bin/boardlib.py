"""boardlib — shared SQLite store for the agent-bridge board (messages, task queue, worker registry).

Used by `bridge-board` (MCP server + CLI + dispatcher) and `ask-agent` (worker registration, task completion).
One file `~/.agent-bridge/state/board/board.db`, WAL mode, every writer opens `BEGIN IMMEDIATE` so N concurrent
stdio servers plus the dispatcher never race on seq numbers or task claims.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from bridgelib import evidence, ledger
from bridgelib.core import STATE, hop_active

HOME = Path.home()
BOARD_DIR = STATE / "board"
DB_PATH = BOARD_DIR / "board.db"
ONLINE_S = 120            # agent counts as online when its last heartbeat is younger than this
LEASE_S = 1500            # claim lease: ask-agent default timeout (900 s) + recovery run + margin
WAIT_BUDGET = 8           # board_wait calls one server process may make before it must stop waiting
PEER_OPEN_TASKS = 5       # queued+claimed tasks a peer-origin identity may hold per topic
KINDS = ("note", "question", "answer", "decision", "task", "system", "request", "handoff", "done", "blocked")
CLOSING_KINDS = ("answer", "done", "blocked")   # a request stays open until one of these replies to it (OPEN_REQUEST)
ROLES = ("leader", "member", "observer")
TASK_MODES = ("ask", "plan", "write")
FAMILIES = ("claude", "cursor", "copilot", "codex", "a2a")   # a request addressed to a family is a turn: a task, run by dispatch

# `addressee` is the column behind the `to` argument (TO is an SQLite keyword): NULL = broadcast, like every message
# posted before the column existed; otherwise `*`, an agent identity, or `@<role>` of the topic's members.
SCHEMA = """
CREATE TABLE IF NOT EXISTS agents  (name TEXT PRIMARY KEY, kind TEXT, pid INTEGER, last_seen INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS topics  (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, created_by TEXT,
                                    created_at INTEGER, closed_at INTEGER);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL, seq INTEGER NOT NULL,
                                    author TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, reply_to INTEGER,
                                    created_at INTEGER, addressee TEXT, UNIQUE(topic_id, seq));
CREATE TABLE IF NOT EXISTS cursors (agent TEXT, topic_id INTEGER, seq INTEGER, PRIMARY KEY(agent, topic_id));
CREATE TABLE IF NOT EXISTS members (topic_id INTEGER NOT NULL, agent TEXT NOT NULL, role TEXT NOT NULL,
                                    joined_at INTEGER, left_at INTEGER, PRIMARY KEY(topic_id, agent));
CREATE TABLE IF NOT EXISTS tasks   (id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT,
                                    dir TEXT, mode TEXT NOT NULL DEFAULT 'ask', origin TEXT NOT NULL,
                                    requested_by TEXT, assignee TEXT NOT NULL DEFAULT 'any', priority INTEGER DEFAULT 0,
                                    status TEXT NOT NULL DEFAULT 'queued', claimed_by TEXT, claimed_at INTEGER,
                                    lease_until INTEGER, result TEXT, created_at INTEGER, updated_at INTEGER,
                                    request_seq INTEGER);
CREATE TABLE IF NOT EXISTS workers (pid INTEGER PRIMARY KEY, driver_pid INTEGER, identity TEXT, family TEXT,
                                    task_id INTEGER, peer INTEGER DEFAULT 1, started_at INTEGER, log TEXT,
                                    deadline INTEGER);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, priority, id);
"""


def now() -> int:
    return int(time.time())


def connect() -> sqlite3.Connection:
    BOARD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=5)   # autocommit; writers use tx() below
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    if "deadline" not in {r[1] for r in conn.execute("PRAGMA table_info(workers)")}:   # boards created before 2026-09-12
        conn.execute("ALTER TABLE workers ADD COLUMN deadline INTEGER")
    if "addressee" not in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}:  # boards before 2026-10-24
        conn.execute("ALTER TABLE messages ADD COLUMN addressee TEXT")
    if "request_seq" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:   # boards before 2026-10-25
        conn.execute("ALTER TABLE tasks ADD COLUMN request_seq INTEGER")
    return conn


@contextlib.contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


class BoardError(Exception):
    """A business-rule refusal; the MCP layer reports it as isError, never as a JSON-RPC error."""


# ----------------------------------------------------------------------------- processes / identity
def proc_cmd(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def proc_alive(pid: int, hint: str) -> bool:
    """pid exists AND its command line still carries `hint` (defeats pid reuse)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return hint in proc_cmd(pid)


def ancestors(pid: int, depth: int = 8) -> list[int]:
    out = []
    for _ in range(depth):
        try:
            ppid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout)
        except (ValueError, OSError):
            break
        if ppid <= 1:
            break
        out.append(ppid)
        pid = ppid
    return out


def bridged_identity(conn: sqlite3.Connection) -> tuple[str, int | None] | None:
    """(identity, run deadline) of the bridged run this process belongs to, found by an ancestor pid registered in
    `workers` — the one signal a peer cannot drop (its env it can scrub). None outside any bridged run.
    Only live rows count: a run killed before it unregistered leaves its row, and its pid gets reused."""
    rows = {r["pid"]: r for r in live_workers(conn)}
    for pid in [os.getpid(), *ancestors(os.getpid())]:
        if pid in rows:
            return rows[pid]["identity"], rows[pid]["deadline"]
    return None


def resolve_identity(conn: sqlite3.Connection, env: dict[str, str]) -> tuple[str, bool, int | None]:
    """(identity, is_peer, run deadline epoch or None). A bridged run is recognised by an ancestor pid registered in
    `workers` (cursor-agent does not pass its env to MCP children, so env alone is not enough)."""
    found = bridged_identity(conn)
    if found:
        return found[0], True, found[1]
    if env.get("AGENT_BRIDGE_IDENTITY"):
        return hop_identity(env["AGENT_BRIDGE_IDENTITY"], env), hop_active(env), None
    if env.get("AGENT_BRIDGE_AGENT"):           # under the hop marker the claim to be the primary is downgraded
        return hop_identity(f"{env['AGENT_BRIDGE_AGENT']}@primary", env), hop_active(env), None
    raise BoardError("no identity: set AGENT_BRIDGE_AGENT in the MCP server env (bridge-sync does this)")


def hop_identity(claimed: str, env: dict[str, str]) -> str:
    """An identity taken from the environment of a bridged run: never the user's and never the primary's — those
    carry rights (decisions, turns, --as) that an env variable must not grant. Such a claim becomes `<family>@hop`."""
    if hop_active(env) and (claimed.startswith("user:") or claimed.endswith("@primary")):
        return f"{family(claimed) or env.get('AGENT_BRIDGE_AGENT', 'peer')}@hop"
    return claimed


def heartbeat(conn: sqlite3.Connection, name: str, kind: str, note: str = "") -> None:
    with tx(conn):
        conn.execute("INSERT INTO agents(name, kind, pid, last_seen, note) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, pid=excluded.pid, "
                     "last_seen=excluded.last_seen, note=excluded.note", (name, kind, os.getpid(), now(), note))


# ----------------------------------------------------------------------------- workers (bridged runs)
def worker_register(conn: sqlite3.Connection, pid: int, identity: str, family: str, task_id: int | None,
                    log: str, peer: bool = True, deadline: int | None = None) -> None:
    with tx(conn):
        conn.execute("INSERT OR REPLACE INTO workers(pid, driver_pid, identity, family, task_id, peer, started_at, log, deadline) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (pid, os.getpid(), identity, family, task_id, int(peer), now(), log, deadline))


def worker_unregister(conn: sqlite3.Connection, pid: int) -> None:
    with tx(conn):
        conn.execute("DELETE FROM workers WHERE pid=?", (pid,))


def live_workers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rows whose pid is alive; dead rows are reaped and their task (if any, still claimed) marked failed."""
    alive, dead = [], []
    for r in conn.execute("SELECT * FROM workers"):
        (alive if proc_alive(r["pid"], r["family"]) else dead).append(r)   # argv[0] of every CLI carries its family name
    if dead:
        with tx(conn):
            for r in dead:
                conn.execute("DELETE FROM workers WHERE pid=?", (r["pid"],))
                if r["task_id"]:
                    _finish(conn, r["task_id"], r["identity"], "worker vanished before reporting", "failed", force=True)
    return alive


# ----------------------------------------------------------------------------- topics / messages
def topic_id(conn: sqlite3.Connection, name: str, creator: str | None) -> int:
    row = conn.execute("SELECT id FROM topics WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    if creator is None:
        raise BoardError(f"topic '{name}' does not exist (a bridged peer may not open topics)")
    with tx(conn):
        conn.execute("INSERT OR IGNORE INTO topics(name, created_by, created_at) VALUES(?,?,?)", (name, creator, now()))
    return conn.execute("SELECT id FROM topics WHERE name=?", (name,)).fetchone()["id"]


def header(body: str) -> dict[str, str]:
    """`key: value` lines at the top of a body, up to the first blank or non-header line — how a request carries
    owner / due / shape."""
    out: dict[str, str] = {}
    for ln in body.splitlines():
        key, sep, value = ln.partition(":")
        if not sep or not key.strip() or " " in key.strip():
            break
        out[key.strip().lower()] = value.strip()
    return out


IDENTITY = re.compile(r"[^\s+@*][^\s+]*")    # claude@primary, cursor#41, claude:task3, user:x — no blanks, no `+`
ROLE_RE = re.compile(r"[a-z][a-z0-9-]*")      # leader | member | observer, or a panel/live persona like root-cause
NO_VALUE = ("", "-", "none", "n/a", "na", "?")


def identity(name: str, what: str = "agent") -> str:
    if not IDENTITY.fullmatch(name) or name == "user:":
        raise BoardError(f"{what} must be an agent identity like claude@primary or cursor#41 (got {name!r})")
    return name


def user_identity() -> str:
    """The human at this machine, as the CLI and the cards address them."""
    return f"user:{os.environ.get('USER', 'user')}"


def role_name(role: str) -> str:
    if not ROLE_RE.fullmatch(role or ""):
        raise BoardError(f"a role is a lowercase slug like leader, member, observer or root-cause (got {role!r})")
    return role


def addressee(to: str | None) -> str | None:
    """Normalise a `to` argument: None/'' = broadcast; else `*`, `@<role>` or an agent identity."""
    if not to:
        return None
    if to == "*":
        return to
    if to.startswith("@"):
        return "@" + role_name(to[1:])
    return identity(to, "to")


def post(conn: sqlite3.Connection, tid: int, author: str, body: str, kind: str = "note",
         reply_to: int | None = None, to: str | None = None) -> int:
    if kind not in KINDS:
        raise BoardError(f"kind must be one of {KINDS}")
    if kind == "decision" and not author.startswith("user:"):
        raise BoardError("kind=decision is reserved for the user (author user:*); post it as note/answer")
    to = addressee(to)
    if kind == "request":
        if to is None:
            raise BoardError("kind=request needs to=<agent|@role|*>: a request without an owner is a note")
        if not header(body).get("shape"):
            raise BoardError("kind=request needs a `shape: <what the answer must look like>` header line")
    if kind == "blocked" and header(body).get("missing", "").strip().lower() in NO_VALUE:
        raise BoardError("kind=blocked needs a `missing: <what you lack>` header line — if what you lack is a "
                         "decision or a fact only the user has, send a card (board_ask_user) instead")
    turn = kind == "request" and to in FAMILIES       # a turn for a peer that is not running: queued, run by dispatch
    if turn:
        if not may_call_turn(conn, tid, author):
            raise BoardError(f"only the user, the primary or the topic's leader may call a {to} turn (each one is a "
                             f"full run) — ask the leader to post the request")
        head = header(body)
        turn_mode, turn_dir = head.get("mode", "ask"), head.get("dir") or None
        if turn_mode == "write" and not turn_dir:
            raise BoardError("mode: write needs a `dir:` line — dispatch runs write turns only inside --allow-write dirs")
        if turn_dir:
            turn_dir = str(Path(turn_dir).expanduser().resolve())
        # a peer leader still pays into the peer task cap; checked here so the request never lands without its task
        _task_checks(conn, tid, not may_spend(author), turn_mode, turn_dir, author)
    target = None
    if reply_to is not None:
        target = conn.execute("SELECT seq, author, kind, body, addressee FROM messages WHERE topic_id=? AND seq=?",
                              (tid, reply_to)).fetchone()
        if target is None:
            raise BoardError(f"reply_to={reply_to} is not a message of this topic")
    # an agent's reply to an agent's still-open request is scored; the requester withdrawing (a later run of the same
    # ephemeral `<family>#<pid>` thread included), a human answering, and replies to a card (its shape names the kind
    # of answer, the human writes it freely) are not
    if (target is not None and target["kind"] == "request" and kind in ("answer", "done", "blocked")
            and not (author == target["author"] or (target["addressee"] not in FAMILIES
                                                     and same_run(author, target["author"])))
            and not author.startswith("user:")
            and not (target["addressee"] or "").startswith("user:")
            and conn.execute(f"SELECT 1 FROM messages m WHERE m.topic_id=? AND m.seq=? AND {OPEN_REQUEST}",
                             (tid, target["seq"])).fetchone() is not None):
        _score_reply(conn, tid, author, kind, body, target)
    with tx(conn):
        conn.execute("INSERT INTO messages(topic_id, seq, author, kind, body, reply_to, created_at, addressee) "
                     "SELECT ?, COALESCE(MAX(seq),0)+1, ?, ?, ?, ?, ?, ? FROM messages WHERE topic_id=?",
                     (tid, author, kind, body, reply_to, now(), to, tid))
        seq = conn.execute("SELECT MAX(seq) FROM messages WHERE topic_id=?", (tid,)).fetchone()[0]
        conn.execute("UPDATE tasks SET lease_until=? WHERE status='claimed' AND claimed_by=?", (now() + LEASE_S, author))
        if target is not None and target["kind"] == "request" and target["addressee"] in FAMILIES:
            _settle_turn(conn, tid, target, author, kind, body)
        if turn:
            # a turn from a leader is user-sanctioned (the leader was appointed by the user or the primary), so the
            # task is origin=user and dispatch starts it without --include-peer-tasks
            _task_insert(conn, tid, body_title(body), body, author, False, to, turn_mode, turn_dir, 0, seq)
    return seq


def may_spend(me: str) -> bool:
    """The identities that are not a bridged peer: the human and the primary session."""
    return me.startswith("user:") or me.endswith("@primary")


def may_call_turn(conn: sqlite3.Connection, tid: int, me: str) -> bool:
    """Who may spend a peer run from the board: the user, the primary session, or the topic's leader."""
    return may_spend(me) or is_leader(conn, tid, me)


def is_turn_run(me: str) -> bool:
    """`<family>:task<N>` — the run dispatch started for a turn; the one identity a family-addressed request reaches.
    A family name may carry digits after its first letter (`a2a`)."""
    return re.fullmatch(r"[a-z][a-z0-9]*:task\d+", me) is not None


def body_title(body: str) -> str:
    """The first line of a body after its `key: value` header block, for a task title."""
    lines = body.splitlines()
    n = len(header(body))
    rest = [ln.strip() for ln in lines[n:] if ln.strip()]
    return (rest[0] if rest else (lines[0].strip() if lines else "request"))[:120]


def _settle_turn(conn: sqlite3.Connection, tid: int, request: sqlite3.Row, author: str, kind: str, body: str) -> None:
    """A reply to a family turn: from that family's dispatched (or hand-started `<fam>#pid`) run it finishes the task
    (failed when it is `blocked`); from anyone else that closes the request (the requester withdrawing, the user) it
    cancels the task so dispatch never spends a run on a settled turn."""
    fam = request["addressee"]
    by_family = family(author) == fam and ("#" in author or is_turn_run(author)) and author != request["author"]
    if by_family and kind in CLOSING_KINDS:
        status = "failed" if kind == "blocked" else "done"
        row = conn.execute("UPDATE tasks SET status=?, result=?, updated_at=?, lease_until=NULL WHERE topic_id=? AND "
                           "request_seq=? AND status IN ('queued','claimed') RETURNING id",
                           (status, body[:400], now(), tid, request["seq"])).fetchone()
        if row:
            _log(conn, tid, f"task #{row['id']} {status} by {author}: answered request seq {request['seq']}")
        return
    still_open = conn.execute(f"SELECT 1 FROM messages m WHERE m.topic_id=? AND m.seq=? AND {OPEN_REQUEST}",
                              (tid, request["seq"])).fetchone() is not None
    if not still_open:
        row = conn.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE topic_id=? AND request_seq=? AND "
                           "status IN ('queued','claimed') RETURNING id", (now(), tid, request["seq"])).fetchone()
        if row:
            _log(conn, tid, f"task #{row['id']} cancelled: request seq {request['seq']} was closed by {author}")


def _score_reply(conn: sqlite3.Connection, tid: int, author: str, kind: str, body: str, request: sqlite3.Row) -> None:
    """A reply to a request is held to the request's `shape:`: an answer/done in another shape is refused (that
    refusal is the one automatic re-request — it quotes the shape back), and the miss is a ledger row, as is every
    `blocked`, so `bridge-plan learn --team` can tell who ships, who stalls and who misses the mark."""
    shape = header(request["body"]).get("shape")
    name = conn.execute("SELECT name FROM topics WHERE id=?", (tid,)).fetchone()[0]
    row = {"peer": author, "topic": name, "request_seq": request["seq"], "requested_by": request["author"]}
    if kind == "blocked":
        _ledger({"type": "blocked", **row, "missing": header(body).get("missing", "")[:200]})
        return
    if evidence.shape_met(shape, body) is False:
        digest = hashlib.sha1(body.strip().encode()).hexdigest()[:12]
        if not _logged("shape_miss", row, digest):          # the same wrong body retried is one miss, not three
            _ledger({"type": "shape_miss", **row, "shape": shape, "body_hash": digest})
        raise BoardError(f"incomplete: the request asks for `{shape}` — reply in that shape, or post blocked with a "
                         f"`missing:` line saying what stops you")


def family(identity: str) -> str:
    """cursor#41, claude@live:p, copilot:task3 → cursor, claude, copilot."""
    return re.split(r"[#@:]", identity or "")[0]


def same_run(a: str, b: str) -> bool:
    """The same agent, or two runs of one `<family>#<pid>` thread (each ask-agent run is a fresh pid)."""
    return a == b or ("#" in a and "#" in b and family(a) == family(b))


def _logged(kind: str, row: dict, digest: str) -> bool:
    try:
        return any(r.get("type") == kind and r.get("body_hash") == digest and r.get("topic") == row["topic"]
                   and r.get("request_seq") == row["request_seq"] and family(r.get("peer", "")) == family(row["peer"])
                   for r in ledger.rows())
    except Exception:  # noqa: BLE001
        return False


def _ledger(row: dict) -> None:
    try:
        ledger.record(row)
    except Exception:  # noqa: BLE001 — the ledger never breaks a post
        pass


def _for_me(me: str, roles: list[str]) -> tuple[str, tuple]:
    """SQL over alias `m`: a message that reaches `me` — broadcast (NULL or *), addressed to me or to one of my
    @roles, or a reply to a message of mine in the same topic. The board's task-log rows are bookkeeping, not mail."""
    marks = ",".join("?" * len(roles))
    sql = ("(m.kind<>'task' AND (m.addressee IS NULL OR m.addressee='*' OR m.addressee=? OR m.addressee=? "
           + (f"OR m.addressee IN ({marks}) " if roles else "")
           + "OR m.reply_to IN (SELECT seq FROM messages WHERE topic_id=m.topic_id AND author=?)))")
    fam = family(me) if is_turn_run(me) else None   # a family turn reaches the run dispatched for it, nobody else
    return sql, (me, fam, *roles, me)


# a request is closed only by an answer/done/blocked from its addressee (the agent, a member holding the @role, any
# user:* for a card to the user, or anyone when it was for everyone) or from the requester — a third party's reply
# does not take it off the owner's list; the user's `decision` closes a card only (on an agent's request it steers)
OPEN_REQUEST = ("m.kind='request' AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.topic_id=m.topic_id "
                "AND r.reply_to=m.seq AND (r.kind IN ('answer','done','blocked') OR (r.kind='decision' AND "
                "m.addressee LIKE 'user:%')) AND (m.addressee='*' OR r.author=m.addressee OR r.author=m.author "
                "OR (instr(m.author,'#')>0 AND instr(r.author,'#')>0 AND substr(m.author,1,instr(m.author,'#'))="
                "substr(r.author,1,instr(r.author,'#'))) "     # a later run of the requester's own `<family>#<pid>` thread
                "OR (m.addressee IN (" + ",".join(f"'{f}'" for f in FAMILIES) + ") AND (r.author LIKE m.addressee||'#%' OR "
                "r.author LIKE m.addressee||':task%')) "      # a dispatched or hand-started run of that family
                "OR (m.addressee LIKE 'user:%' AND r.author LIKE 'user:%') OR (m.addressee LIKE '@%' AND r.author IN "
                "(SELECT agent FROM members b WHERE b.topic_id=m.topic_id AND b.role=substr(m.addressee, 2) "
                "AND b.left_at IS NULL))))")
MSG_COLS = 'm.seq, m.author, m.kind, m.body, m.reply_to, m.created_at, m.addressee AS "to"'


def read(conn: sqlite3.Connection, tid: int, reader: str, after_seq: int | None, limit: int = 50,
         mine: bool = False, open_requests: bool = False, roles: list[str] | None = None) -> tuple[list, int]:
    """Messages after `after_seq` (explicit wins; None = after the reader's unread cursor, or from the start for
    open_requests). A plain or `mine` read advances the cursor (what `mine` skips was never for the reader);
    `open_requests` is a view and leaves it alone."""
    filtered = open_requests
    if after_seq is None:
        row = conn.execute("SELECT seq FROM cursors WHERE agent=? AND topic_id=?", (reader, tid)).fetchone()
        after_seq = 0 if open_requests else (row["seq"] if row else 0)
    where, args = ["m.topic_id=?", "m.seq>?"], [tid, after_seq]
    if mine:
        sql, a = _for_me(reader, roles or [])
        where.append(sql)
        args.extend(a)
    if open_requests:
        where.append(OPEN_REQUEST)
    rows = conn.execute(f"SELECT {MSG_COLS} FROM messages m WHERE {' AND '.join(where)} ORDER BY m.seq LIMIT ?",
                        (*args, limit)).fetchall()
    last = rows[-1]["seq"] if rows else after_seq
    if not filtered:
        with tx(conn):
            conn.execute("INSERT INTO cursors(agent, topic_id, seq) VALUES(?,?,?) ON CONFLICT(agent, topic_id) "
                         "DO UPDATE SET seq=MAX(cursors.seq, excluded.seq)", (reader, tid, last))
    return [dict(r) for r in rows], last


def others_after(conn: sqlite3.Connection, tid: int, me: str, after_seq: int, mention: bool = False,
                 roles: list[str] | None = None) -> bool:
    """Someone else posted after `after_seq`; with `mention`, only a post that reaches me (see _for_me) counts."""
    sql, args = _for_me(me, roles or []) if mention else ("1", ())
    return conn.execute(f"SELECT 1 FROM messages m WHERE m.topic_id=? AND m.seq>? AND m.author<>? AND {sql} LIMIT 1",
                        (tid, after_seq, me, *args)).fetchone() is not None


def reaching(conn: sqlite3.Connection, tid: int, me: str, after_seq: int, upto: int,
             roles: list[str] | None = None) -> list[dict]:
    """Other agents' posts in (after_seq, upto] that reach `me` (see _for_me), oldest first; no cursor is moved — the
    feed loop of a live peer, which keeps its own high-water mark."""
    sql, args = _for_me(me, roles or [])
    rows = conn.execute(f"SELECT {MSG_COLS} FROM messages m WHERE m.topic_id=? AND m.seq>? AND m.seq<=? "
                        f"AND m.author<>? AND {sql} ORDER BY m.seq", (tid, after_seq, upto, me, *args)).fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------- cards to the user
def card(kind: str, rows: list[tuple[str, str, str, str]], due: str | None = None) -> str:
    """The one shape in which an agent asks the human: `decision` (a choice only they own) or `fact` (information
    nobody else has). Header lines a parser reads, then the table question · options · recommendation · if undecided.
    The user answers with kind=decision (a decision) or kind=answer (a fact), reply_to the card's seq."""
    if kind not in ("decision", "fact"):
        raise BoardError("a card is a decision or a fact")
    if not rows:
        raise BoardError("a card needs at least one question")
    cell = lambda s: " ".join((s or "-").split()).replace("|", "/")  # noqa: E731 — one line per cell, no pipes
    head = f"card: {kind}\nshape: {kind}\n" + (f"due: {cell(due)}\n" if due else "")   # one line: no forged header
    table = "| question | options | recommendation | if undecided |\n|---|---|---|---|\n" + "\n".join(
        f"| {cell(q)} | {cell(opt)} | {cell(rec)} | {cell(und)} |" for q, opt, rec, und in rows)
    return head + "\n" + table


def card_questions(body: str) -> list[str]:
    """The question cells of a card body — what a one-line view shows instead of the header and table scaffolding."""
    rows = [ln for ln in body.splitlines() if ln.startswith("|") and not re.match(r"^\|\s*:?-{3,}", ln)]
    return [ln.strip("| ").split("|")[0].strip() for ln in rows[1:]]


def user_cards(conn: sqlite3.Connection) -> list[dict]:
    """Every open request addressed to the user across open topics, oldest first — the one list a human answers."""
    rows = conn.execute(f"SELECT t.name AS topic, {MSG_COLS} FROM messages m JOIN topics t ON t.id=m.topic_id "
                        f"WHERE t.closed_at IS NULL AND m.addressee LIKE 'user:%' AND {OPEN_REQUEST} "
                        "ORDER BY m.created_at, m.id").fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------- members / dm / room
def member_add(conn: sqlite3.Connection, tid: int, agent: str, role: str = "member") -> None:
    role_name(role)
    with tx(conn):
        conn.execute("INSERT INTO members(topic_id, agent, role, joined_at, left_at) VALUES(?,?,?,?,NULL) "
                     "ON CONFLICT(topic_id, agent) DO UPDATE SET role=excluded.role, joined_at=excluded.joined_at, "
                     "left_at=NULL", (tid, agent, role, now()))


def member_remove(conn: sqlite3.Connection, tid: int, agent: str) -> bool:
    with tx(conn):
        n = conn.execute("UPDATE members SET left_at=? WHERE topic_id=? AND agent=? AND left_at IS NULL",
                         (now(), tid, agent)).rowcount
    return bool(n)


def members(conn: sqlite3.Connection, tid: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT agent, role FROM members WHERE topic_id=? AND left_at IS NULL "
                                          "ORDER BY role, agent", (tid,))]


def my_roles(conn: sqlite3.Connection, tid: int, me: str) -> list[str]:
    """The `@<role>` mentions that reach `me` in this topic (empty when I am not a member)."""
    return [f"@{m['role']}" for m in members(conn, tid) if m["agent"] == me]


def is_leader(conn: sqlite3.Connection, tid: int, me: str) -> bool:
    return "@leader" in my_roles(conn, tid, me)


def shares_topic(conn: sqlite3.Connection, a: str, b: str) -> bool:
    return conn.execute("SELECT 1 FROM members x JOIN members y ON y.topic_id=x.topic_id WHERE x.agent=? AND y.agent=? "
                        "AND x.left_at IS NULL AND y.left_at IS NULL LIMIT 1", (a, b)).fetchone() is not None


def dm_name(a: str, b: str) -> str:
    """`dm:<a>+<b>` in sorted order — `+` because identities themselves carry `:` (user:x, claude:task3)."""
    return "dm:" + "+".join(sorted((a, b)))


def _reopen_as(conn: sqlite3.Connection, name: str, me: str, peer: bool) -> None:
    """A closed dm/room comes back only for the user or the primary, and the reopening is on the record so the
    close gate asks for a fresh summary."""
    if conn.execute("SELECT 1 FROM topics WHERE name=? AND closed_at IS NOT NULL", (name,)).fetchone() is None:
        return
    if peer:
        raise BoardError(f"topic '{name}' is closed — only the user or the primary may reopen it")
    reopen_topic(conn, name)
    post(conn, topic_id(conn, name, me), me, f"reopened by {me}", "system")


def open_topic(conn: sqlite3.Connection, name: str, me: str, peer: bool) -> tuple[int, str]:
    """Open (or reopen) a topic as `me`. `dm:<a>+<b>`: I must be one side; a bridged peer's other side must be the
    user or an agent it already shares a topic with. `room:<name>`: the user or the primary only; the opener leads.
    Any other name keeps the old rule (a bridged peer may not open topics). Returns (topic id, canonical name)."""
    if name.startswith("dm:"):
        pair = name[3:].split("+")
        if len(pair) != 2 or pair[0] == pair[1]:
            raise BoardError("a dm topic is dm:<agent>+<agent> (two different identities)")
        for side in pair:
            identity(side, "each side of a dm")
        if me not in pair:
            raise BoardError("a dm must have you on one side")
        other = pair[1] if pair[0] == me else pair[0]
        if peer and not (other.startswith("user:") or shares_topic(conn, me, other)):
            raise BoardError(f"a bridged peer may open a dm only with the user or with an agent it shares a topic with "
                             f"({other} is neither)")
        name = dm_name(*pair)
        _reopen_as(conn, name, me, peer)
        tid = topic_id(conn, name, me)
        ever = {r[0] for r in conn.execute("SELECT agent FROM members WHERE topic_id=?", (tid,))}
        for agent in pair:
            if agent not in ever:                       # a side that left stays out until it joins again itself
                member_add(conn, tid, agent, "member")
        return tid, name
    if name.startswith("room:"):
        if peer:
            raise BoardError("a bridged peer may not open rooms — ask the leader or the user to open one")
        _reopen_as(conn, name, me, peer)
        tid = topic_id(conn, name, me)
        if not members(conn, tid):                      # a reopened room keeps its leader
            member_add(conn, tid, me, "leader")
        return tid, name
    return topic_id(conn, name, None if peer else me), name


def _may_manage(conn: sqlite3.Connection, tid: int, me: str) -> bool:
    return me.startswith("user:") or is_leader(conn, tid, me)


def join(conn: sqlite3.Connection, tid: int, me: str, agent: str, role: str = "member") -> None:
    if not _may_manage(conn, tid, me):
        raise BoardError("only the topic's leader or the user may add members or set roles")
    member_add(conn, tid, identity(agent), role)


def leave(conn: sqlite3.Connection, tid: int, me: str, agent: str | None = None) -> None:
    agent = agent or me
    if agent != me and not _may_manage(conn, tid, me):
        raise BoardError("only the topic's leader or the user may remove another member")
    if agent != me and agent.startswith("user:") and not me.startswith("user:"):
        raise BoardError("the user leaves a topic by their own choice, never removed by an agent")
    if not member_remove(conn, tid, agent):
        raise BoardError(f"{agent} is not a member of this topic")


# ----------------------------------------------------------------------------- tasks
def sweep(conn: sqlite3.Connection) -> None:
    """Expired leases with no live worker go back to the queue; dead workers fail their task (via live_workers)."""
    busy = {r["task_id"] for r in live_workers(conn) if r["task_id"]}
    with tx(conn):
        for r in conn.execute("SELECT id, topic_id, claimed_by FROM tasks WHERE status='claimed' AND lease_until<?",
                              (now(),)).fetchall():
            if r["id"] in busy:
                continue
            conn.execute("UPDATE tasks SET status='queued', claimed_by=NULL, claimed_at=NULL, lease_until=NULL, "
                         "updated_at=? WHERE id=?", (now(), r["id"]))
            _log(conn, r["topic_id"], f"task #{r['id']} lease expired for {r['claimed_by']} — back in the queue")


def _log(conn: sqlite3.Connection, tid: int, text: str) -> None:
    conn.execute("INSERT INTO messages(topic_id, seq, author, kind, body, created_at) "
                 "SELECT ?, COALESCE(MAX(seq),0)+1, 'board', 'task', ?, ? FROM messages WHERE topic_id=?",
                 (tid, text, now(), tid))


def task_add(conn: sqlite3.Connection, tid: int, title: str, body: str, requester: str, peer: bool,
             assignee: str = "any", mode: str = "ask", workdir: str | None = None, priority: int = 0,
             request_seq: int | None = None) -> int:
    _task_checks(conn, tid, peer, mode, workdir, requester)
    with tx(conn):
        return _task_insert(conn, tid, title, body, requester, peer, assignee, mode, workdir, priority, request_seq)


def _task_checks(conn: sqlite3.Connection, tid: int, peer: bool, mode: str, workdir: str | None, requester: str) -> None:
    if mode not in TASK_MODES:
        raise BoardError(f"mode must be one of {TASK_MODES}")
    if workdir and not Path(workdir).is_dir():
        raise BoardError(f"dir does not exist: {workdir}")
    if peer:
        # the topic's peer-origin tasks plus the requester's own open turns: a peer leader's or an A2A client's turn
        # is origin=user (dispatch runs it without --include-peer-tasks) but still one of its five
        n = conn.execute("SELECT COUNT(*) FROM tasks WHERE topic_id=? AND (origin='peer' OR requested_by=?) "
                         "AND status IN ('queued','claimed')", (tid, requester)).fetchone()[0]
        if n >= PEER_OPEN_TASKS:
            raise BoardError(f"peer task limit reached for this topic ({PEER_OPEN_TASKS} open)")


def _task_insert(conn: sqlite3.Connection, tid: int, title: str, body: str, requester: str, peer: bool, assignee: str,
                 mode: str, workdir: str | None, priority: int, request_seq: int | None) -> int:
    """The insert alone — inside the caller's transaction (post() queues a turn with its request in one commit)."""
    cur = conn.execute("INSERT INTO tasks(topic_id, title, body, dir, mode, origin, requested_by, assignee, priority, "
                       "status, created_at, updated_at, request_seq) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?)",
                       (tid, title, body, workdir, mode, "peer" if peer else "user", requester, assignee, priority,
                        now(), now(), request_seq))
    _log(conn, tid, f"task #{cur.lastrowid} queued by {requester} for {assignee} ({mode}): {title}"
                    + (f" — request seq {request_seq}" if request_seq else ""))
    return cur.lastrowid


def task_claim(conn: sqlite3.Connection, me: str, family: str, task_id: int | None = None) -> dict:
    sweep(conn)
    with tx(conn):
        held = conn.execute("SELECT id FROM tasks WHERE status='claimed' AND claimed_by=?", (me,)).fetchone()
        if held:
            raise BoardError(f"you already hold task #{held['id']} — finish it (board_task_done) before claiming another")
        pick = ("SELECT id FROM tasks WHERE status='queued' AND assignee IN (?, 'any') AND id=?" if task_id else
                "SELECT id FROM tasks WHERE status='queued' AND assignee IN (?, 'any') ORDER BY priority DESC, id ASC LIMIT 1")
        args = (family, task_id) if task_id else (family,)
        row = conn.execute(f"UPDATE tasks SET status='claimed', claimed_by=?, claimed_at=?, lease_until=?, updated_at=? "
                           f"WHERE id=({pick}) RETURNING *", (me, now(), now() + LEASE_S, now(), *args)).fetchone()
        if row is None:
            raise BoardError("nothing to claim" + (f" (task #{task_id} is not queued for you)" if task_id else ""))
        _log(conn, row["topic_id"], f"task #{row['id']} claimed by {me}")
    return dict(row)


def _finish(conn, task_id: int, me: str, result: str, status: str, force: bool = False) -> sqlite3.Row:
    cond = "" if force else " AND claimed_by=?"
    row = conn.execute(f"UPDATE tasks SET status=?, result=?, updated_at=?, lease_until=NULL "
                       f"WHERE id=? AND status='claimed'{cond} RETURNING id, topic_id, title",
                       (status, result, now(), task_id, *(() if force else (me,)))).fetchone()
    if row:
        _log(conn, row["topic_id"], f"task #{task_id} {status} by {me}: {result[:400]}")
    return row


def task_done(conn: sqlite3.Connection, me: str, task_id: int, result: str, status: str = "done") -> dict:
    if status not in ("done", "failed"):
        raise BoardError("status must be done or failed")
    with tx(conn):
        row = _finish(conn, task_id, me, result, status)
    if row is None:
        raise BoardError(f"task #{task_id} is not claimed by you (or already closed)")
    return dict(row)


def task_cancel(conn: sqlite3.Connection, task_id: int, who: str) -> None:
    with tx(conn):
        row = conn.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=? AND status IN ('queued','claimed') "
                           "RETURNING topic_id", (now(), task_id)).fetchone()
        if row is None:
            raise BoardError(f"task #{task_id} is not open")
        _log(conn, row["topic_id"], f"task #{task_id} cancelled by {who}")


# ----------------------------------------------------------------------------- status / gc
def status(conn: sqlite3.Connection, me: str) -> dict:
    sweep(conn)
    t = now()
    online = [dict(r) for r in conn.execute("SELECT name, kind, last_seen FROM agents WHERE last_seen>? ORDER BY name",
                                            (t - ONLINE_S,))]
    topics = []
    for r in conn.execute("SELECT id, name, created_by FROM topics WHERE closed_at IS NULL ORDER BY id"):
        mem = members(conn, r["id"])
        owed = open_requests_to(conn, r["id"], me)
        if mem and me not in {m["agent"] for m in mem} and not me.startswith("user:") and not owed:
            continue            # members only — the user, who runs the board, sees all; so does a run a turn is owed by
        cur = conn.execute("SELECT seq FROM cursors WHERE agent=? AND topic_id=?", (me, r["id"])).fetchone()
        last = conn.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE topic_id=?", (r["id"],)).fetchone()[0]
        topics.append({"name": r["name"], "created_by": r["created_by"], "members": mem, "last_seq": last,
                       "unread_for_me": last - (cur["seq"] if cur else 0), "open_requests_to_me": owed})
    tasks = [dict(r) for r in conn.execute(
        "SELECT t.id, p.name AS topic, t.title, t.assignee, t.mode, t.origin, t.status, t.claimed_by "
        "FROM tasks t JOIN topics p ON p.id=t.topic_id WHERE t.status IN ('queued','claimed') ORDER BY t.priority DESC, t.id")]
    workers = [{"identity": r["identity"], "family": r["family"], "task_id": r["task_id"],
                "running_s": t - r["started_at"]} for r in live_workers(conn)]
    return {"me": me, "online": online, "topics": topics, "open_tasks": tasks, "running_workers": workers,
            "open_requests_to_me": sum(x["open_requests_to_me"] for x in topics)}


def open_requests_to(conn: sqlite3.Connection, tid: int, me: str) -> int:
    """Requests by others in this topic, addressed to me / one of my roles / everyone, with no closing reply yet."""
    roles = my_roles(conn, tid, me)
    marks = ",".join("?" * len(roles))
    by_role = f"OR m.addressee IN ({marks})" if roles else ""
    fam = family(me) if is_turn_run(me) else None
    return conn.execute(f"SELECT COUNT(*) FROM messages m WHERE m.topic_id=? AND m.author<>? AND {OPEN_REQUEST} "
                        f"AND (m.addressee='*' OR m.addressee=? OR m.addressee=? {by_role})",
                        (tid, me, me, fam, *roles)).fetchone()[0]


def close_topic_as(conn: sqlite3.Connection, name: str, me: str) -> None:
    """Close as `me`: the user, the topic's leader, either side of a dm, or the creator of a topic that has no members
    (the pre-members shape). A topic with members closes only after a summary — a `done` to everyone that answers
    nothing in particular (reply_to NULL) and comes after the last request and the last reopening — so the board
    itself records what the team finished."""
    row = conn.execute("SELECT id, name, created_by FROM topics WHERE name=? AND closed_at IS NULL", (name,)).fetchone()
    if row is None:
        raise BoardError(f"topic '{name}' is not open")
    tid, mem = row["id"], members(conn, row["id"])
    mine = me in {m["agent"] for m in mem}
    if not (_may_manage(conn, tid, me) or (name.startswith("dm:") and mine) or (not mem and row["created_by"] == me)):
        raise BoardError("only the topic's leader or the user may close it (either side closes a dm)")
    if mem:
        floor = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM messages WHERE topic_id=? AND (kind='request' "
                             "OR (kind='system' AND body LIKE 'reopened by %'))", (tid,)).fetchone()[0]
        summary = conn.execute("SELECT 1 FROM messages WHERE topic_id=? AND kind='done' AND addressee='*' "
                               "AND reply_to IS NULL AND seq>?", (tid, floor)).fetchone()
        if summary is None:
            raise BoardError("post a summary first (kind=done to=*, not a reply, after the last request: what was "
                             "done, the evidence, what is still open)")
    close_topic(conn, name)


def close_topic(conn: sqlite3.Connection, name: str, missing_ok: bool = False) -> bool:
    """Close an open topic; False (or BoardError without missing_ok) when there is no open topic of that name."""
    with tx(conn):
        closed = conn.execute("UPDATE topics SET closed_at=? WHERE name=? AND closed_at IS NULL",
                              (now(), name)).rowcount
    if not closed and not missing_ok:
        raise BoardError(f"topic '{name}' is not open")
    return bool(closed)


def reopen_topic(conn: sqlite3.Connection, name: str) -> None:
    """A room a peer rejoins after its last member left: the backlog is still there until gc, so reopen it."""
    with tx(conn):
        conn.execute("UPDATE topics SET closed_at=NULL WHERE name=?", (name,))


def gc(conn: sqlite3.Connection, days: int) -> int:
    """Delete topics closed more than `days` ago with everything under them; seq numbers inside open topics never move."""
    cutoff = now() - days * 86400
    with tx(conn):
        ids = [r["id"] for r in conn.execute("SELECT id FROM topics WHERE closed_at IS NOT NULL AND closed_at<?", (cutoff,))]
        for tid in ids:
            for table in ("messages", "cursors", "tasks", "members"):
                conn.execute(f"DELETE FROM {table} WHERE topic_id=?", (tid,))
            conn.execute("DELETE FROM topics WHERE id=?", (tid,))
        conn.execute("DELETE FROM agents WHERE last_seen<?", (cutoff,))
    return len(ids)
