"""Outcome ledger: one JSON line per bridged run or panel, so playbooks can be tuned from what actually happened.

Rows are appended by `ask-agent` (every run) and `bridge-panel` (every panel, including aborts); `bridge-plan learn`
aggregates them per kind and `bridge-plan learn --backfill` rebuilds rows from the run logs and panel dirs on disk.
A run row is a fact about a run — never a verdict about its quality; `peer review:` citations in the docs and
`type: verdict` rows (`bridge-plan mark`: findings kept / rejected after the caller verified them) supply that.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .core import ROOT, RUNS, STATE

LEDGER = STATE / "ledger.jsonl"
PLAYBOOKS = ROOT / "playbooks"


def load_playbooks() -> dict[str, dict]:
    books: dict[str, dict] = {}
    if not PLAYBOOKS.is_dir():
        return books
    for f in sorted(PLAYBOOKS.glob("*.md")):
        text = f.read_text()
        m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
        meta: dict = {}
        body = text
        if m:
            body = m.group(2).strip()
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] == '"':
                        v = v[1:-1]
                    meta[k.strip()] = v
        meta.setdefault("name", f.stem)
        meta["body"] = body
        meta["path"] = str(f)
        books[meta["name"]] = meta
    return books


def classify(text: str, books: dict[str, dict] | None = None) -> list[tuple[str, int]]:
    """Playbooks whose trigger pattern hits the text, best first (hit count, then playbook order)."""
    books = books if books is not None else load_playbooks()
    scored = []
    for name, b in books.items():
        pat = b.get("triggers")
        if not pat:
            continue
        try:
            hits = list(re.finditer(pat, text, re.I))
        except re.error:
            hits = []
        if hits:
            scored.append((name, len(hits), hits[0].start()))
    # more hits first; on a tie the playbook whose trigger appears earliest wins — the leading noun names the job
    # ("review MR !123 for defects" is a review, not a root-cause hunt)
    return [(n, c) for n, c, _ in sorted(scored, key=lambda x: (-x[1], x[2]))]


def record(row: dict) -> None:
    row = {"ts": int(time.time()), **row}
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def rows() -> list[dict]:
    if not LEDGER.is_file():
        return []
    out = []
    for line in LEDGER.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _end(r: dict) -> float:
    """When a run row's run finished: live rows are written at the end; backfilled rows carry the start (log name)."""
    return r["ts"] + (r.get("duration_s") or 0) if r.get("backfilled") else r["ts"]


def run_cost(session: str | None, reported: float | None, started: float, prior: list[dict] | None = None) -> float | None:
    """This run's own cost. Claude's `total_cost_usd` is cumulative over a resumed session, so subtract the session
    total the run resumed from: the largest total of a run in that session that had finished before this one started
    (parallel resumes of one session all start from that same total — measured 2026-10-04). A fork has a new id."""
    if reported is None or not session:
        return reported
    base = max((r["cost_session"] for r in (rows() if prior is None else prior)
                if r.get("session") == session and r.get("cost_session") is not None
                and _end(r) <= started and r["cost_session"] <= reported), default=0.0)
    return round(reported - base, 6)


def _last_json(path: Path) -> dict:
    try:
        for line in reversed(path.read_text(errors="replace").splitlines()):
            if line.startswith("{"):
                d = json.loads(line)
                if isinstance(d, dict):
                    return d
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def backfill() -> tuple[int, int]:
    """Rebuild the ledger from ~/.agent-bridge/state/runs and panel/ — used once to seed learning from history."""
    books = load_playbooks()
    out: list[dict] = []
    # The process exit status is not on disk; a live row (or a backfilled row that inherited it) is its only source.
    live_rc = {r["log"]: r["rc"] for r in rows()
               if r.get("type") == "run" and r.get("log") and "rc" in r
               and (not r.get("backfilled") or r.get("rc_source") == "live")}
    panel_kind: dict[str, str] = {}
    for d in sorted((STATE / "panel").iterdir()) if (STATE / "panel").is_dir() else []:
        for p in sorted(RUNS.glob("*-claude.prompt.txt")):
            t = p.read_text(errors="replace")
            if f"Panel `{d.name}`" in t and "Problem from the user" in t:
                ranked = classify(t.split("Problem from the user:", 1)[1][:2000], books)
                panel_kind[d.name] = ranked[0][0] if ranked else "other"
                break
    for p in sorted(RUNS.glob("*.prompt.txt")):
        m = re.match(r"(\d{8})-(\d{6})(?:-\d+)?-(claude|cursor|copilot)(\.recover)?\.prompt", p.name)
        if not m or m.group(4):
            continue
        prompt = p.read_text(errors="replace")
        if m.group(3) == "copilot":
            # Capability/instruction metadata is not the task and must not match playbook triggers.
            prompt = re.sub(r"^\(Bridge workspace bootstrap\n.*?\)\n\n", "", prompt, count=1, flags=re.S)
        panel = re.search(r"Panel `([^`]+)`", prompt)
        log = p.with_name(p.name.replace(".prompt.txt", ".json"))
        d = _last_json(log) if log.is_file() else {}
        parsed = None
        if m.group(3) == "copilot" and log.is_file():
            from .adapters import ADAPTERS
            parsed = ADAPTERS["copilot"].parse(log.read_text(errors="replace"))
        usage = d.get("usage") or {}
        err = log.with_suffix(".err")
        err_txt = err.read_text(errors="replace") if err.is_file() else ""
        ts = int(time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")))
        ranked = classify(prompt, books)
        rc = (7 if re.search(r"out of usage|monthly quota", err_txt) else
              (int(parsed.is_error) if parsed else (1 if d.get("is_error") else (0 if d else None))))
        if str(log) in live_rc:
            rc = live_rc[str(log)]
        out.append({"ts": ts, "type": "run", "peer": m.group(3),
                    "kind": panel_kind.get(panel.group(1), "other") if panel else (ranked[0][0] if ranked else "other"),
                    "panel": panel.group(1) if panel else None,
                    "role": (re.search(r"you are `([^`]+)`", prompt) or [None, None])[1],
                    "rc": rc, **({"rc_source": "live"} if str(log) in live_rc else {}),
                    "duration_s": round((d.get("duration_ms") or usage.get("sessionDurationMs") or 0) / 1000),
                    "session": parsed.session if parsed else d.get("session_id"),
                    "cost_session": d.get("total_cost_usd"),
                    "tokens_out": usage.get("output_tokens") or usage.get("outputTokens"),
                    "turns": d.get("num_turns"), "cache_write": usage.get("cache_creation_input_tokens"),
                    "log": str(log), "backfilled": True, **(parsed.metrics if parsed else {})})
    done: list[dict] = []
    for r in sorted(out, key=lambda r: r["ts"]):             # backfilled ts = start, so this is start order
        r["cost"] = run_cost(r["session"], r["cost_session"], r["ts"], done)
        done.append(r)
    for d in sorted((STATE / "panel").iterdir()) if (STATE / "panel").is_dir() else []:
        if not d.is_dir():
            continue
        fnd = d / "findings.json"
        roles = list(json.loads(fnd.read_text())) if fnd.is_file() else []
        st = json.loads((d / "status.json").read_text()) if (d / "status.json").is_file() else {}
        rep = d / "report.md"
        head = rep.read_text(errors="replace").splitlines()[0] if rep.is_file() else ""
        when = re.search(r"— (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", head)
        ts = int(time.mktime(time.strptime(when.group(1), "%Y-%m-%d %H:%M"))) if when else int(d.stat().st_mtime)
        prob = ""
        for p in sorted(RUNS.glob("*-claude.prompt.txt")):
            t = p.read_text(errors="replace")
            if f"Panel `{d.name}`" in t and "Problem from the user" in t:
                prob = t.split("Problem from the user:", 1)[1][:2000]
                break
        ranked = classify(prob, books)
        out.append({"ts": ts, "type": "panel", "name": d.name, "kind": ranked[0][0] if ranked else "other",
                    "roles": roles, "answered": st.get("answered", {}), "fallbacks": list(st.get("fallbacks", {})),
                    "failed": list(st.get("failed", {})), "rc": 0 if rep.is_file() else 4,
                    "fix": (d / "patch.md").is_file(), "backfilled": True})
    # work / fix / verdict rows have no source on disk to rebuild from (a verdict is a human count) — carry them over
    out += [r for r in rows() if r.get("type") not in ("run", "panel")]
    out.sort(key=lambda r: r.get("ts", 0))
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out))
    return sum(1 for r in out if r["type"] == "run"), sum(1 for r in out if r["type"] == "panel")
