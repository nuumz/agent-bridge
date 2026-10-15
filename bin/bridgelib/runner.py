"""Running one bridged call: process group + timeout, board registration, named threads, quota gate, narration recovery."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boardlib

from .adapters import Adapter, Parsed
from .core import (STATE, HOP_VAR, IDENTITY_VAR, QUOTA_TTL, RUNS, TASK_VAR, err_summary, quota_block, quota_clear,
                   quota_hit, quota_mark)

CONV_DIR = STATE / "conv"
ASK_RE = re.compile(r"^\s*\[\[ASK\]\]\s*(.+?)\s*$", re.M)
AGREE_RE = re.compile(r"\[\[(AGREE|DONE)\]\]")
PROTOCOL = ("\n\n(Bridge protocol: write the answer in the reply body itself, do not narrate. If you need something only "
            "the caller can provide, end with one line `[[ASK]] <question>` and stop — the caller resumes this session "
            "with the answer.)")
USER_AUTHOR = f"user:{os.environ.get('USER', 'user')}"


def protocol(conv: Conv | None) -> str:
    """With a board-backed thread the peer asks back live (board_post + board_wait); [[ASK]] stays the fallback."""
    if conv is None or conv.topic is None:
        return PROTOCOL
    return (f"\n\n(Bridge protocol: write the answer in the reply body itself, do not narrate. This thread is board topic "
            f"'{conv.topic_name}' — board_read it for history; the bridge posts your reply there for you, so do NOT "
            f"board_post your answer. If you need something only the caller can provide: board_post kind=question on that "
            f"topic, then board_wait for the answer; if nothing arrives after two waits or the wait is refused, end with "
            f"one line `[[ASK]] <question>` and stop.)")


def kill_tree(proc: subprocess.Popen, sig: int = signal.SIGTERM, grace: int = 10) -> None:
    """Signal the peer's whole process group (the CLI plus every tool/MCP child it spawned), SIGKILL after `grace` s."""
    for s, wait in ((sig, grace), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, s)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run(binary: str, argv: list[str], cwd: str, timeout: int, log: Path, env_extra: dict[str, str] | None = None,
        on_start: Callable[[int], None] | None = None) -> int:
    err = log.with_suffix(".err")
    env = {**os.environ, HOP_VAR: "1", **(env_extra or {})}
    with open(log, "w") as out, open(err, "w") as e:
        # own process group: a timeout or a signal to ask-agent must take the peer AND its children down,
        # otherwise a killed run leaves node/rg/MCP processes burning CPU (and tokens) in the background
        proc = subprocess.Popen([binary, *argv], cwd=cwd, stdout=out, stderr=e, stdin=subprocess.DEVNULL, env=env,
                                process_group=0)
        if on_start:
            on_start(proc.pid)

        def forward(signum: int, _frame: Any) -> None:
            kill_tree(proc, signum, grace=3)
            raise SystemExit(128 + signum)

        previous = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            return 143
        finally:
            for s, h in previous.items():
                signal.signal(s, h)


class BoardRun:
    """Registers one bridged run on the shared board (identity + CLI pid) and closes a dispatched task afterwards."""

    def __init__(self, agent: Adapter, enabled: bool, log: Path, timeout: int = 0):
        self.fixed = os.environ.get(IDENTITY_VAR)        # dispatch sets <agent>:task<N>; otherwise <agent>#<CLI pid> at spawn
        self.identity = self.fixed or f"{agent.name}#{os.getpid()}"
        self.task_id = int(os.environ[TASK_VAR]) if os.environ.get(TASK_VAR, "").isdigit() else None
        self.agent, self.log, self.pid = agent, log, None
        self.deadline = int(time.time()) + timeout          # board_wait inside the peer never runs past this
        self.board = boardlib.connect() if enabled else None

    @property
    def env(self) -> dict[str, str]:
        return {IDENTITY_VAR: self.identity}

    def on_start(self, pid: int) -> None:   # the MCP server inside the peer finds this row by walking its ancestor pids
        self.pid = pid
        if not self.fixed:
            self.identity = f"{self.agent.name}#{pid}"   # unique per run, also inside one dialog process
        print(f"[bridge] running {self.identity} — watch it live: bridge-watch -f {pid}   (all runs: bridge-watch)",
              file=sys.stderr)
        if self.board is not None:
            boardlib.worker_register(self.board, pid, self.identity, self.agent.name, self.task_id, str(self.log),
                                     deadline=self.deadline)

    def finish(self, status: int, parsed: Parsed | None) -> None:
        if self.board is None:
            return
        if self.pid is not None:
            boardlib.worker_unregister(self.board, self.pid)
        if self.task_id is None:
            return
        try:                                 # dispatched task: close it unless the peer already did via board_task_done
            if status != 0 or parsed is None:
                boardlib.task_done(self.board, self.identity, self.task_id,
                                   f"peer exited {status} (see {self.log.with_suffix('.err')})", "failed")
            else:
                boardlib.task_done(self.board, self.identity, self.task_id, parsed.result[:4000],
                                   "failed" if parsed.is_error else "done")
        except boardlib.BoardError:
            pass


def looks_like_narration(p: Parsed) -> bool:
    return isinstance(p.tok_out, int) and p.tok_out > 4000 and len(p.result) < 1500 and bool(p.session)


class Conv:
    """A named multi-turn thread: remembers each peer's session id and appends a readable transcript."""

    def __init__(self, name: str, board=None):
        self.name = name
        self.dir = CONV_DIR / name
        self.state_path = self.dir / "state.json"
        self.transcript = self.dir / "transcript.md"
        self.state = {"name": name, "created": dt.datetime.now().isoformat(timespec="seconds"), "sessions": {}, "turns": 0}
        if self.state_path.is_file():
            self.state = json.loads(self.state_path.read_text())
        # board mirror: every turn is also a message on topic conv:<name>, so peers can board_read the thread,
        # `bridge-board tail conv:<name>` follows it live, and the user can steer a dialog by posting there
        self.board, self.topic_name, self.topic = board, f"conv:{name}", None
        if board is not None:
            self.topic = boardlib.topic_id(board, self.topic_name, USER_AUTHOR)
            self.seen = board.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE topic_id=?", (self.topic,)).fetchone()[0]
            self.mark = self.seen                 # seq at the start of the current run (for the mirror dedupe)

    def session_for(self, peer: str) -> str | None:
        return self.state["sessions"].get(peer)

    def record(self, frm: str, to: str, text: str, session: str | None = None, peer: str | None = None,
               author: str | None = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if session and peer:                 # the session id belongs to the peer that produced the reply, never to `to`
            self.state["sessions"][peer] = session
        self.state["turns"] += 1
        n = self.state["turns"]
        if not self.transcript.is_file():
            self.transcript.write_text(f"# conversation `{self.name}`\n\n")
        with open(self.transcript, "a") as f:
            f.write(f"### {n}. {frm} → {to}\n\n{text.strip()}\n\n")
        self.state_path.write_text(json.dumps(self.state, indent=2))
        if self.topic is not None and author and not self._already_posted(author, text):
            boardlib.post(self.board, self.topic, author, text.strip(), "answer" if peer else "note")
        if self.topic is not None and not peer:  # a request was just recorded: the run starts after this seq
            self.mark = self.board.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE topic_id=?", (self.topic,)).fetchone()[0]

    def _already_posted(self, author: str, text: str) -> bool:
        """True when `author` posted this text itself during the run (a peer that ignored 'do NOT board_post')."""
        head = text.strip()[:120]
        rows = self.board.execute("SELECT body FROM messages WHERE topic_id=? AND seq>? AND author=?",
                                  (self.topic, self.mark, author)).fetchall()
        return any(r["body"].strip()[:120] == head or head in r["body"] for r in rows)

    def moderator_notes(self) -> str:
        """New user:* posts on the thread's topic since the last check — the human steering a running dialog."""
        if self.topic is None:
            return ""
        rows = self.board.execute("SELECT seq, author, body FROM messages WHERE topic_id=? AND seq>? AND author LIKE 'user:%' "
                                  "AND kind='decision' ORDER BY seq", (self.topic, self.seen)).fetchall()
        self.seen = self.board.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE topic_id=?", (self.topic,)).fetchone()[0]
        return "\n".join(f"Moderator ({r['author']}): {r['body']}" for r in rows)


def execute(agent: Adapter, o: argparse.Namespace, prompt: str, session: str | None,
            conv: Conv | None, frm: str = "caller") -> tuple[int, Parsed | None, Path | None]:
    """One bridged call: prepare workspace, run, parse, auto-recover, record. Returns (rc, parsed, log)."""
    t0 = time.time()
    rc, parsed, log = 2, None, None
    try:
        rc, parsed, log = _execute(agent, o, prompt, session, conv, frm)
        return rc, parsed, log
    finally:
        agent.finish(o)                        # release per-run setup (Cursor's temporary Write(**) deny) on every path
        try:                                   # outcome ledger — a fact per run, feeds `bridge-plan learn`
            from . import ledger
            panel = re.search(r"Panel `([^`]+)`", prompt)
            ranked = ledger.classify(prompt) if not getattr(o, "kind", "") else []
            extra = parsed.extra if parsed and parsed.extra else ""
            cost = re.search(r"cost=([\d.]+)", extra)
            turns = re.search(r"turns=(\d+)", extra)
            cache_write = re.search(r"cache_write=(\d+)", extra)
            session = parsed.session if parsed else None
            cost_session = float(cost.group(1)) if cost else None   # cumulative when the run resumed a session
            ledger.record({"type": "run", "peer": agent.name, "mode": o.mode,
                           "kind": getattr(o, "kind", "") or (ranked[0][0] if ranked else "other"),
                           "panel": panel.group(1) if panel else None, "conv": getattr(o, "conv", "") or None,
                           "rc": rc, "duration_s": round(time.time() - t0),
                           "session": session, "cost_session": cost_session,
                           "cost": ledger.run_cost(session, cost_session, t0),
                           "tokens_out": parsed.tok_out if parsed and isinstance(parsed.tok_out, int) else None,
                           "turns": int(turns.group(1)) if turns else None,
                           "cache_write": int(cache_write.group(1)) if cache_write else None,
                           "log": str(log) if log else None})
        except Exception:  # noqa: BLE001 — the ledger must never break a run
            pass


def _execute(agent: Adapter, o: argparse.Namespace, prompt: str, session: str | None,
             conv: Conv | None, frm: str = "caller") -> tuple[int, Parsed | None, Path | None]:
    o.session = session or ""
    if not o.ignore_quota:
        blocked = quota_block(agent.name)
        if blocked:
            print(f"[bridge] error: {blocked}", file=sys.stderr)
            return 7, None, None
    for n in agent.prepare(o, False):
        print(f"[bridge] {n}", file=sys.stderr)
    full_prompt = agent.preamble(o) + prompt + protocol(conv)
    binary = agent.resolve()
    if not binary:
        hint = {"copilot": "npm install -g @github/copilot  (or: brew install copilot-cli)",
                "cursor": "curl https://cursor.com/install -fsS | bash",
                "claude": "see https://claude.com/claude-code"}[agent.name]
        print(f"ask-agent: {agent.binary} not on PATH — install: {hint}", file=sys.stderr)
        return 127, None, None

    RUNS.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"   # pid: parallel runs must not share a log
    log = RUNS / f"{stamp}-{agent.name}.json"
    (RUNS / f"{stamp}-{agent.name}.prompt.txt").write_text(full_prompt + "\n")
    if conv:                                   # dialog relays (frm = the other peer) are already on the board as replies
        conv.record(frm, agent.name, prompt, author=USER_AUTHOR if frm == "caller" else None)

    board = BoardRun(agent, o.board, log, o.timeout)
    status = run(binary, agent.argv(o, full_prompt), o.dir, o.timeout, log, board.env, board.on_start)
    if status != 0:
        board.finish(status, None)
        if status == 143:
            print(f"ask-agent: killed after {o.timeout}s. From an agent shell run this in the background "
                  "(Claude Code's Bash tool caps foreground calls at 120 s / 600 s max).",
                  file=sys.stderr)
        else:
            print(f"ask-agent: {agent.binary} exited {status}. stderr:", file=sys.stderr)
        err_txt = log.with_suffix(".err").read_text(errors="replace")
        out_txt = log.read_text(errors="replace")
        quota = quota_hit(err_txt, out_txt[-4000:])
        if quota:
            quota_mark(agent.name, quota)
            print(f"[bridge] error: {agent.name} out of quota — {quota} (later runs fail fast for {QUOTA_TTL // 60} min, exit 7)",
                  file=sys.stderr)
            status = 7
        else:
            print(f"[bridge] error: {err_summary(log.with_suffix('.err'), f'exit {status}, no stderr')}", file=sys.stderr)
        print("\n".join(err_txt.splitlines()[-20:]), file=sys.stderr)
        if out_txt:
            print(out_txt[-2000:], file=sys.stderr)
        return status, None, log

    parsed = agent.parse(log.read_text(encoding="utf-8", errors="replace"))
    if parsed.is_error:
        board.finish(1, parsed)
        quota = quota_hit(parsed.result, log.with_suffix(".err").read_text(errors="replace"))
        if quota:
            quota_mark(agent.name, quota)
            print(f"[bridge] error: {agent.name} out of quota — {quota}", file=sys.stderr)
            return 7, parsed, log
        print(f"[bridge] error: {(parsed.result or '').strip().splitlines()[0][:240] if (parsed.result or '').strip() else 'peer reported an error'}",
              file=sys.stderr)
        return 1, parsed, log
    if quota_clear(agent.name):
        print(f"[bridge] {agent.name} answered — quota mark cleared", file=sys.stderr)
    if agent.recovery and not o.no_recover and looks_like_narration(parsed):
        print(f"[bridge] result looks like narration only — resuming session {parsed.session} to fetch the answer",
              file=sys.stderr)
        rprompt = ("Output the answer to the previous request now as plain markdown in the reply body itself. "
                   "Do not narrate, do not call tools.")
        rlog = RUNS / f"{stamp}-{agent.name}.recover.json"
        (RUNS / f"{stamp}-{agent.name}.recover.prompt.txt").write_text(rprompt + "\n")
        if run(binary, agent.resume_argv(o, parsed.session, rprompt), o.dir, o.timeout, rlog, board.env) != 0:
            print(f"ask-agent: recovery run failed (see {rlog.with_suffix('.err')}); resume by hand with --session "
                  f"{parsed.session}", file=sys.stderr)
            board.finish(1, parsed)
            return 1, parsed, log
        rp = agent.parse(rlog.read_text(encoding="utf-8", errors="replace"))
        rp.session = rp.session or parsed.session
        parsed, log = rp, rlog
    if conv:
        conv.record(agent.name, frm, parsed.result, parsed.session, peer=agent.name, author=board.identity)
    board.finish(0, parsed)
    return 0, parsed, log


def print_parsed(agent: Adapter, p: Parsed, log: Path | None, conv: Conv | None) -> int:
    """Print answer + [bridge] line; exit 5 when the peer asked the caller something back."""
    print(p.result)
    extra = f" {p.extra}" if p.extra else ""
    cv = f" conv={conv.name}" if conv else ""
    print(f"\n[bridge] agent={agent.name} session={p.session or '?'} tokens=in:{p.tok_in}/out:{p.tok_out}{extra}{cv} log={log}")
    m = ASK_RE.search(p.result or "")
    if m:
        how = f"ask-agent {agent.name} --conv {conv.name}" if conv else f"ask-agent {agent.name} --session {p.session}"
        print(f"[bridge] the peer asks back: {m.group(1)}\n[bridge] answer with: {how} \"<answer>\"", file=sys.stderr)
        return 5
    return 0
