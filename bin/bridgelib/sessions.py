"""Live Claude Code sessions on this machine, classified against a request — hand work over by topic, not by name.

Read-only; sends nothing. A session's name is derived from its cwd (`p3-2-defect-66`, `-2a`), so it says nothing about
what the session is doing; this module reads what it is doing:

- registry  ~/.claude/sessions/<pid>.json — name, cwd, sessionId, kind, status (idle | busy | shell), socket
- transcript ~/.claude/projects/<cwd, every non-alphanumeric char as '-'>/<sessionId>.jsonl — the prompts it received
  (typed, a peer's message body, a slash command), the closing line of each reply (Done / Not done / Needs you) and the
  queue of messages not yet taken
- claims     <dir>/pipeline/*.md whose flat frontmatter names `session: <name>` — the workspace's own owner per topic

Classes (the user's ruling of 2026-11-17, SHARED-RULES.md §3 "Handing work to a live session"):
  SAME      it owns a claim on a key of the request, or its open task (the prompts since its last Done) names one —
            anywhere in the user's own prompt, in the first line of a relayed peer message — send, even while busy
  RELATED   no key match, but its open task shares identifiers with the request — may run alongside, send
  FREE      idle, nothing queued, nothing asked since its last Done (or ever) — unrelated work may go here; one that
            handled the key before is preferred
  OCCUPIED  busy, or idle with an open task on another topic — never
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
# Ticket-shaped keys a request names itself (QC-740, KESC-10249). ASCII boundaries: a Thai letter is \w, and
# "แก้ QC-740ให้" must still yield the key.
KEY_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]+-\d+(?![A-Za-z0-9])")
TOKEN_RE = re.compile(r"[A-Za-z0-9][\w./-]{3,}[A-Za-z0-9]")
CLOSING_RE = re.compile(r"^\W*(Done|Not done|Needs you)\b")  # the last line of a reply (the user's global rule)
STRIP_RE = re.compile(r"<system-reminder>.*?</system-reminder>|</?cross-session-message[^>]*>", re.S)
# Shared identifiers before an open task counts as RELATED. Two, not one: a misroute into a busy session is the failure
# the ruling names, while a fresh worker or a question to the user is cheap.
RELATED_MIN = 2
# user records that ask nothing: command output, a `!` shell command and its output, an interrupt marker
NOT_ASKED = ("<local-command-stdout>", "<task-notification>", "<bash-input>", "<bash-stdout>", "<bash-stderr>",
             "[Request interrupted")


def tokens(text: str) -> set[str]:
    """identifier-shaped words (a digit, a separator or an inner capital): what two tasks share by topic."""
    return {t.lower() for t in TOKEN_RE.findall(text) if re.search(r"\d|[-_./]|[a-z][A-Z]", t)}


def keys_of(request: str, given: list[str]) -> list[str]:
    return list(dict.fromkeys([k.strip() for k in given if k.strip()] + KEY_RE.findall(request)))


def key_in(key: str, text: str) -> bool:
    """a bare number (740) counts only as `#740` or a slash-command argument (`/defect-fix 740`): as any word it
    would match `line 740` or `Foo.java:740`."""
    if key.isdigit():  # not after `-`: KESC-740 is another ticket; a slash command, not a path (`/Users/…`)
        return bool(re.search(rf"(?<=#){key}(?![A-Za-z0-9])", text)) or (
            bool(re.match(r"/[\w:-]+\s", text)) and key in text.split()[1:])
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])", text, re.I))


def closing(reply: str | None) -> str | None:
    m = CLOSING_RE.match(reply or "")
    return m.group(1) if m else None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ancestor_pids() -> set[int]:
    """this process and its ancestors — the caller's own session is among them when an agent runs the command."""
    pids, pid = set(), os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            ps = ["ps", "-o", "ppid=", "-p", str(pid)]
            out = subprocess.run(ps, capture_output=True, text=True, timeout=5, check=False).stdout
            pid = int(out.strip() or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            break
    return pids


def registry(self_name: str | None = None) -> tuple[list[dict], set[str]]:
    """(live interactive sessions without the caller and headless runs, the caller's own names). Headless = the
    `sdk-cli` entrypoint: a bridged run that resumes a thread (`--resume --fork-session`) is not named `bridge ·`."""
    own = set() if self_name else ancestor_pids()
    out, mine = [], {self_name} if self_name else set()
    for f in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            pid = int(rec["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        name = str(rec.get("name") or "")
        headless = str(rec.get("entrypoint") or "").startswith("sdk") or name.startswith("bridge ·")
        if rec.get("kind") != "interactive" or headless or not alive(pid):
            continue
        if name == self_name or pid in own:
            mine.add(name)
            continue
        out.append(rec)
    return out, mine


def transcript(rec: dict) -> Path | None:
    sid = str(rec.get("sessionId") or "")
    if not sid:
        return None
    path = PROJECTS_DIR / re.sub(r"[^A-Za-z0-9]", "-", str(rec.get("cwd") or "")) / f"{sid}.jsonl"
    return path if path.is_file() else next(PROJECTS_DIR.glob(f"*/{sid}.jsonl"), None)


def prompt_text(r: dict) -> str:
    """what a `type: user` record asked: a typed prompt, a peer's message body or a slash command; "" for the rest."""
    origin = r.get("origin") or {}
    if origin.get("kind") == "peer":
        return str(origin.get("body") or "")
    if origin.get("kind") not in (None, "human") or r.get("isMeta") or r.get("isCompactSummary"):
        return ""  # task notifications, local-command caveats, the summary that restates a compacted conversation
    c = (r.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return ""
        c = "\n".join(str(x.get("text") or "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    if not isinstance(c, str) or c.lstrip().startswith(NOT_ASKED):
        return ""
    cmd = re.search(r"<command-name>(.*?)</command-name>", c, re.S)
    if cmd:
        args = re.search(r"<command-args>(.*?)</command-args>", c, re.S)
        return f"{cmd.group(1)} {args.group(1) if args else ''}".strip()
    return STRIP_RE.sub("", c).strip()


def names(key: str, kind: str, text: str) -> bool:
    """does this prompt put `key` on the session's plate: anywhere in the user's own prompt, in the topic line of a
    relayed peer message — its first line that is not a bare label (`Task from the user:`, `# Handoff`); a key the
    body only cites as an incident or example is not topic ownership."""
    if kind == "prompt":
        return key_in(key, text)
    lines = (x.strip() for x in text.splitlines() if x.strip())
    return key_in(key, next((x for x in lines if KEY_RE.search(x) or not (x.endswith(":") or x.startswith("#"))), ""))


def activity(path: Path | None) -> tuple[list[tuple[str, str]], int] | None:
    """([("prompt" | "relay", text) | ("reply", the last line of a turn's last text)] in order, messages queued and
    not yet taken); None when the transcript is missing or cannot be read."""
    events: list[tuple[str, str]] = []
    pending = 0
    if path is None:
        return None
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            kind = r.get("type")
            if kind == "queue-operation":
                op = r.get("operation")
                pending = max(0, pending + (1 if op == "enqueue" else -1 if op in ("dequeue", "remove") else 0))
            elif r.get("isSidechain"):
                continue
            elif kind == "user":
                text = prompt_text(r)
                if text:  # a peer's message is a relay: its topic is its first line, the rest may cite examples
                    events.append(("relay" if (r.get("origin") or {}).get("kind") == "peer" else "prompt", text))
            elif kind == "assistant":
                for c in (r.get("message") or {}).get("content") or []:
                    lines = [x for x in str(c.get("text") or "").splitlines() if x.strip()] if isinstance(c, dict) \
                        and c.get("type") == "text" else []
                    if lines and events and events[-1][0] == "reply":
                        events[-1] = ("reply", lines[-1].strip())  # a turn writes one record per text block
                    elif lines:
                        events.append(("reply", lines[-1].strip()))
    return events, pending


def claims(dir_: str, keys: list[str]) -> list[tuple[str, str, str, set[str]]]:
    """[(file relative to dir, session it names, the key it matched, every key of that topic)] from
    <dir>/pipeline/*.md flat frontmatter."""
    out = []
    root = Path(dir_)
    for f in sorted((root / "pipeline").glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
        if not m:
            continue
        fm = {}
        for line in m.group(1).splitlines():
            if ":" in line and not line[:1].isspace():
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip("'\"")
        owner = fm.get("session", "")
        if not owner:
            continue
        # identity fields only: `related: [QC-740]` in QC-741.md must not make its owner the owner of QC-740
        values = {f.stem.lower()} | {v.strip().strip("'\"").lower() for k in ("code", "qcweb", "jira")
                                     for v in fm.get(k, "").strip("[]").split(",") if v.strip()}
        hit = next((k for k in keys if k.lower() in values), None)
        if hit:
            out.append((str(f.relative_to(root)), owner, hit, values))
    return out


def classify(rec: dict, act: tuple[list[tuple[str, str]], int] | None, keys: list[str], req_tokens: set[str],
             owned: list[tuple[str, str, str, set[str]]], known: set[str]) -> tuple[str, str]:
    name = str(rec.get("name") or "")
    mine = [c for c in owned if c[1] == name]
    if mine:
        return "SAME", f"claim {mine[0][0]} session: {name} ({mine[0][2]})"
    if act is None:
        return "OCCUPIED", "transcript missing or unreadable — what it is doing is unknown"
    events, pending = act
    # a busy session's last text is mid-turn ("Done: step 1 of 3"), not the turn's closing line
    closed = events[:-1] if rec.get("status") != "idle" and events and events[-1][0] == "reply" else events
    done_at = max((i for i, (k, t) in enumerate(closed) if k == "reply" and closing(t) == "Done"), default=-1)
    open_events = [(k, t) for k, t in events[done_at + 1:] if k != "reply"]
    open_prompts = [t for _, t in open_events]
    # only the open task: a session that closed this topic with Done and moved on is not on it any more
    for key in sorted(known):  # the request's keys and a matching claim's aliases
        if any(names(key, k, t) for k, t in open_events):
            return "SAME", f"its open task names {key}"
    current = "\n".join(open_prompts)
    shared = sorted(req_tokens & tokens(current))
    # an open task on another ticket is another topic, whatever words it shares with this request; this topic's own
    # keys and aliases can still be here, cited in a relay's body
    other_keys = [k for k in KEY_RE.findall(current) if k.lower() not in known]
    if len(shared) >= RELATED_MIN and not (keys and other_keys):
        return "RELATED", "open task shares " + ", ".join(shared[:4])
    last = next((t for k, t in reversed(events) if k == "reply"), None)
    status = str(rec.get("status") or "")
    if status != "idle":
        return "OCCUPIED", status or "status unknown"
    if pending:
        return "OCCUPIED", f"idle with {pending} message(s) queued"
    if not open_prompts:  # nothing asked since its last Done (or ever)
        return "FREE", "idle; last task closed: " + (last or "")[:80] if done_at >= 0 else "idle; no task yet"
    if closing(last):
        return "OCCUPIED", f"idle, task open: {(last or '')[:100]}"
    return "OCCUPIED", f"idle, a prompt has no Done reply yet: {(last or '(none)')[:80]}"


def _same_dir(cwd: str, dir_: str) -> bool:
    """the session can already work in dir_: the same directory, or a parent of it that is a project (not ~ or /)."""
    if not cwd:
        return False
    a, b = Path(cwd).resolve(), Path(dir_).resolve()
    return a == b or (a in b.parents and a not in (Path.home().resolve(), Path("/")))


def target(request: str, keys: list[str], dir_: str, self_name: str | None = None) -> dict:
    keys = keys_of(request, keys)
    req_tokens = tokens(request) | {k.lower() for k in keys}
    owned = claims(dir_, keys)
    known = {k.lower() for k in keys}.union(*(c[3] for c in owned))
    live_recs, mine = registry(self_name)
    rows = []
    for rec in live_recs:
        act = activity(transcript(rec))
        cls, why = classify(rec, act, keys, req_tokens, owned, known)
        seen = act is not None and any(names(k, kind, t) for k in known for kind, t in act[0] if kind != "reply")
        offered = cls == "FREE" and _same_dir(str(rec.get("cwd") or ""), dir_)
        if cls == "FREE" and not offered:
            why += f"; outside {dir_} — not offered unrelated work there"
        elif offered and seen:
            why += "; handled this topic before"
        rows.append({"name": rec.get("name"), "pid": rec.get("pid"), "cwd": rec.get("cwd"), "status": rec.get("status"),
                     "socket": rec.get("messagingSocketPath"), "updatedAt": rec.get("updatedAt") or 0,
                     "offered": offered, "seen": seen,
                     "class": cls, "evidence": why})
    live = {r["name"] for r in rows}
    yours = [c for c in owned if c[1] in mine]
    orphan = [c for c in owned if c[1] not in live and c[1] not in mine]
    by = {c: [r for r in rows if r["class"] == c] for c in ("SAME", "RELATED", "FREE")}
    claimed = [r for r in by["SAME"] if r["evidence"].startswith("claim ")]
    # a claim outranks a prompt: whoever only heard of the topic (a misrouted message) never beats the owner
    if yours:
        rec = {"action": "self", "why": f"{yours[0][0]} names you ({yours[0][1]}) — the work is yours"}
    elif len(claimed) == 1:
        rec = {"action": "send", "name": claimed[0]["name"], "why": claimed[0]["evidence"]}
    elif len(claimed) > 1:
        rec = {"action": "ask", "why": "several sessions claim this topic: " + ", ".join(r["name"] for r in claimed)}
    elif orphan:
        file, owner = orphan[0][0], orphan[0][1]
        rec = {"action": "ask", "why": f"{file} names {owner}, which is not live — resume it or reassign"}
    elif len(by["SAME"]) > 1:
        rec = {"action": "ask", "why": "several sessions on this topic: " + ", ".join(r["name"] for r in by["SAME"])}
    elif by["SAME"]:
        rec = {"action": "send", "name": by["SAME"][0]["name"], "why": by["SAME"][0]["evidence"]}
    elif len(by["RELATED"]) == 1:
        rec = {"action": "send", "name": by["RELATED"][0]["name"], "why": by["RELATED"][0]["evidence"]}
    elif by["RELATED"]:
        rec = {"action": "ask", "why": "several related sessions: " + ", ".join(r["name"] for r in by["RELATED"])}
    else:
        # unrelated work goes only to a free session already in the directory; otherwise a fresh worker
        free = sorted((r for r in by["FREE"] if r["offered"]), key=lambda r: (not r["seen"], -r["updatedAt"]))
        rec = ({"action": "send", "name": free[0]["name"], "why": free[0]["evidence"]} if free else
               {"action": "spawn", "why": "no session on this topic and none free in " + dir_})
    if rec.get("name"):
        rec["socket"] = next(r["socket"] for r in rows if r["name"] == rec["name"])
    return {"keys": keys, "dir": dir_, "recommend": rec, "sessions": rows,
            "orphan_claims": [{"file": f, "session": s, "key": k} for f, s, k, _ in orphan]}


def render(res: dict) -> str:
    rec = res["recommend"]
    head = {"send": f"target: {rec.get('name')}", "spawn": "target: spawn fresh (ask-agent / bridge-live start)",
            "ask": "target: ask user", "self": "target: yourself"}[rec["action"]]
    lines = [f"{head} — {rec['why']}", f"keys: {', '.join(res['keys']) or '-'}   dir: {res['dir']}"]
    order = {"SAME": 0, "RELATED": 1, "FREE": 2, "OCCUPIED": 3}
    for r in sorted(res["sessions"], key=lambda r: (order[r["class"]], not r["evidence"].startswith("claim "))):
        lines.append(f"  {r['class']:<9} {r['name']:<28} {r['status'] or '-':<6} {r['cwd']}")
        lines.append(f"            {r['evidence']}")
    for c in res["orphan_claims"]:
        lines.append(f"  claim     {c['file']} names {c['session']} (not live)")
    return "\n".join(lines)
