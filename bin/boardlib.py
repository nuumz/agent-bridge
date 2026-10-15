"""boardlib — shared SQLite store for the agent-bridge board (messages, task queue, worker registry).

Used by `bridge-board` (MCP server + CLI + dispatcher) and `ask-agent` (worker registration, task completion).
One file `~/.agent-bridge/state/board/board.db`, WAL mode, every writer opens `BEGIN IMMEDIATE` so N concurrent
stdio servers plus the dispatcher never race on seq numbers or task claims.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from bridgelib.core import STATE

HOME = Path.home()
BOARD_DIR = STATE / "board"
DB_PATH = BOARD_DIR / "board.db"
ONLINE_S = 120            # agent counts as online when its last heartbeat is younger than this
LEASE_S = 1500            # claim lease: ask-agent default timeout (900 s) + recovery run + margin
WAIT_BUDGET = 8           # board_wait calls one server process may make before it must stop waiting
PEER_OPEN_TASKS = 5       # queued+claimed tasks a peer-origin identity may hold per topic
KINDS = ("note", "question", "answer", "decision", "task", "system")
TASK_MODES = ("ask", "plan", "write")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents  (name TEXT PRIMARY KEY, kind TEXT, pid INTEGER, last_seen INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS topics  (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, created_by TEXT,
                                    created_at INTEGER, closed_at INTEGER);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL, seq INTEGER NOT NULL,
                                    author TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, reply_to INTEGER,
                                    created_at INTEGER, UNIQUE(topic_id, seq));
CREATE TABLE IF NOT EXISTS cursors (agent TEXT, topic_id INTEGER, seq INTEGER, PRIMARY KEY(agent, topic_id));
CREATE TABLE IF NOT EXISTS tasks   (id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT,
                                    dir TEXT, mode TEXT NOT NULL DEFAULT 'ask', origin TEXT NOT NULL,
                                    requested_by TEXT, assignee TEXT NOT NULL DEFAULT 'any', priority INTEGER DEFAULT 0,
                                    status TEXT NOT NULL DEFAULT 'queued', claimed_by TEXT, claimed_at INTEGER,
                                    lease_until INTEGER, result TEXT, created_at INTEGER, updated_at INTEGER);
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


def resolve_identity(conn: sqlite3.Connection, env: dict[str, str]) -> tuple[str, bool, int | None]:
    """(identity, is_peer, run deadline epoch or None). A bridged run is recognised by an ancestor pid registered in
    `workers` (cursor-agent does not pass its env to MCP children, so env alone is not enough)."""
    rows = {r["pid"]: r for r in conn.execute("SELECT pid, identity, deadline FROM workers")}
    for pid in [os.getpid(), *ancestors(os.getpid())]:
        if pid in rows:
            return rows[pid]["identity"], True, rows[pid]["deadline"]
    if env.get("AGENT_BRIDGE_IDENTITY"):
        return env["AGENT_BRIDGE_IDENTITY"], bool(env.get("AGENT_BRIDGE_HOP")), None
    if env.get("AGENT_BRIDGE_AGENT"):
        return f"{env['AGENT_BRIDGE_AGENT']}@primary", bool(env.get("AGENT_BRIDGE_HOP")), None
    raise BoardError("no identity: set AGENT_BRIDGE_AGENT in the MCP server env (bridge-sync does this)")


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


def post(conn: sqlite3.Connection, tid: int, author: str, body: str, kind: str = "note",
         reply_to: int | None = None) -> int:
    if kind not in KINDS:
        raise BoardError(f"kind must be one of {KINDS}")
    if kind == "decision" and not author.startswith("user:"):
        raise BoardError("kind=decision is reserved for the user (author user:*); post it as note/answer")
    with tx(conn):
        conn.execute("INSERT INTO messages(topic_id, seq, author, kind, body, reply_to, created_at) "
                     "SELECT ?, COALESCE(MAX(seq),0)+1, ?, ?, ?, ?, ? FROM messages WHERE topic_id=?",
                     (tid, author, kind, body, reply_to, now(), tid))
        seq = conn.execute("SELECT MAX(seq) FROM messages WHERE topic_id=?", (tid,)).fetchone()[0]
        conn.execute("UPDATE tasks SET lease_until=? WHERE status='claimed' AND claimed_by=?", (now() + LEASE_S, author))
    return seq


def read(conn: sqlite3.Connection, tid: int, reader: str, after_seq: int | None, limit: int = 50) -> tuple[list, int]:
    """Messages after `after_seq` (explicit wins; None = after the reader's unread cursor). Advances the cursor."""
    if after_seq is None:
        row = conn.execute("SELECT seq FROM cursors WHERE agent=? AND topic_id=?", (reader, tid)).fetchone()
        after_seq = row["seq"] if row else 0
    rows = conn.execute("SELECT seq, author, kind, body, reply_to, created_at FROM messages "
                        "WHERE topic_id=? AND seq>? ORDER BY seq LIMIT ?", (tid, after_seq, limit)).fetchall()
    last = rows[-1]["seq"] if rows else after_seq
    with tx(conn):
        conn.execute("INSERT INTO cursors(agent, topic_id, seq) VALUES(?,?,?) ON CONFLICT(agent, topic_id) "
                     "DO UPDATE SET seq=MAX(cursors.seq, excluded.seq)", (reader, tid, last))
    return [dict(r) for r in rows], last


def others_after(conn: sqlite3.Connection, tid: int, me: str, after_seq: int) -> bool:
    return conn.execute("SELECT 1 FROM messages WHERE topic_id=? AND seq>? AND author<>? LIMIT 1",
                        (tid, after_seq, me)).fetchone() is not None


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
             assignee: str = "any", mode: str = "ask", workdir: str | None = None, priority: int = 0) -> int:
    if mode not in TASK_MODES:
        raise BoardError(f"mode must be one of {TASK_MODES}")
    if workdir and not Path(workdir).is_dir():
        raise BoardError(f"dir does not exist: {workdir}")
    if peer:
        n = conn.execute("SELECT COUNT(*) FROM tasks WHERE topic_id=? AND origin='peer' AND status IN ('queued','claimed')",
                         (tid,)).fetchone()[0]
        if n >= PEER_OPEN_TASKS:
            raise BoardError(f"peer task limit reached for this topic ({PEER_OPEN_TASKS} open)")
    with tx(conn):
        cur = conn.execute("INSERT INTO tasks(topic_id, title, body, dir, mode, origin, requested_by, assignee, priority, "
                           "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)",
                           (tid, title, body, workdir, mode, "peer" if peer else "user", requester, assignee, priority,
                            now(), now()))
        _log(conn, tid, f"task #{cur.lastrowid} queued by {requester} for {assignee} ({mode}): {title}")
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
    for r in conn.execute("SELECT id, name, created_by, closed_at FROM topics ORDER BY id"):
        cur = conn.execute("SELECT seq FROM cursors WHERE agent=? AND topic_id=?", (me, r["id"])).fetchone()
        last = conn.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE topic_id=?", (r["id"],)).fetchone()[0]
        topics.append({"name": r["name"], "created_by": r["created_by"], "closed": bool(r["closed_at"]),
                       "last_seq": last, "unread_for_me": last - (cur["seq"] if cur else 0)})
    tasks = [dict(r) for r in conn.execute(
        "SELECT t.id, p.name AS topic, t.title, t.assignee, t.mode, t.origin, t.status, t.claimed_by "
        "FROM tasks t JOIN topics p ON p.id=t.topic_id WHERE t.status IN ('queued','claimed') ORDER BY t.priority DESC, t.id")]
    workers = [{"identity": r["identity"], "family": r["family"], "task_id": r["task_id"],
                "running_s": t - r["started_at"]} for r in live_workers(conn)]
    return {"me": me, "online": online, "topics": topics, "open_tasks": tasks, "running_workers": workers}


def close_topic(conn: sqlite3.Connection, name: str) -> None:
    with tx(conn):
        if conn.execute("UPDATE topics SET closed_at=? WHERE name=? AND closed_at IS NULL", (now(), name)).rowcount == 0:
            raise BoardError(f"topic '{name}' is not open")


def gc(conn: sqlite3.Connection, days: int) -> int:
    """Delete topics closed more than `days` ago with everything under them; seq numbers inside open topics never move."""
    cutoff = now() - days * 86400
    with tx(conn):
        ids = [r["id"] for r in conn.execute("SELECT id FROM topics WHERE closed_at IS NOT NULL AND closed_at<?", (cutoff,))]
        for tid in ids:
            for table in ("messages", "cursors", "tasks"):
                conn.execute(f"DELETE FROM {table} WHERE topic_id=?", (tid,))
            conn.execute("DELETE FROM topics WHERE id=?", (tid,))
        conn.execute("DELETE FROM agents WHERE last_seen<?", (cutoff,))
    return len(ids)
