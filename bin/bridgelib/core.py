"""Paths, constants shared by every peer, the quota mark, and small parsing helpers."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import signal
import sys
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

HOME = Path.home()
# Files shipped with the checkout (bin, roles, playbooks, work-skills) resolve from wherever it was cloned;
# runtime state (runs, panel, work, board, ledger, …) always lives under STATE — one git-ignored directory.
ROOT = Path(__file__).resolve().parents[2]
STATE = HOME / ".agent-bridge" / "state"
RUNS = STATE / "runs"
PANELS = STATE / "panel"
WORK = STATE / "work"
# Session cards live under panel/<name>/ or work/<name>/,
# never under conv/ or a top-level agreements/ directory. Legacy flat files may sit in agreements.archived/.
AGREEMENTS_ARCHIVED = STATE / "agreements.archived"
HOP_VAR = "AGENT_BRIDGE_HOP"

def guarded_rc(name: str, write_rc: Callable[[str, int], int], body: Callable[[], int]) -> int:
    """Run `body` with SIGTERM → exit 143; always write_rc(name, rc), including on SIGTERM.

    Agent/Bash kills of a foreground bridge call send SIGTERM; without this the waiter never sees a
    done signal. SystemExit from the handler is intercepted so `rc` is 143 before write_rc runs.
    """
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    rc = 1
    try:
        rc = body()
    except SystemExit as e:
        if isinstance(e.code, int):
            rc = e.code
        raise
    finally:
        write_rc(name, rc)
    return rc

def detach(log: Path) -> None:
    """`--detach`: fork; the parent prints `detached pid=N log=PATH` and exits 0 at once, the child keeps running in
    its own session with stdout+stderr in `log` (truncated) and stdin closed.

    Replaces the `nohup … > log 2>&1 &` line the peer-* agents used to compose by hand — the composition itself
    (redirect target, `$!`, scratch paths) was one of the three things that went wrong on 2026-09-27. Call it after
    clear_rc() so the waiter never sees a stale sentinel, and before guarded_rc() so the child installs the
    SIGTERM handler.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout.flush(); sys.stderr.flush()
    pid = os.fork()
    if pid:
        print(f"detached pid={pid} log={log}")
        sys.stdout.flush()
        os._exit(0)
    os.setsid()
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0); os.dup2(fd, 1); os.dup2(fd, 2)
    os.close(null); os.close(fd)
    sys.stdout.reconfigure(line_buffering=True); sys.stderr.reconfigure(line_buffering=True)


IDENTITY_VAR = "AGENT_BRIDGE_IDENTITY"
TASK_VAR = "AGENT_BRIDGE_TASK"
BOARD_TOOLS = "mcp__bridge_board"            # Claude Code permission rule: every tool of the bridge_board MCP server
GIT_DENY = ("git commit", "git push", "git tag")
BRIDGE_CMDS = ("ask-agent", "ask-claude", "ask-cursor", "ask-copilot")
# peers switched off on this machine (reason shown by --list and on any attempt); delete the entry to re-enable
DISABLED: dict[str, str] = {}
# A peer that reports an exhausted plan/quota is marked here for QUOTA_TTL seconds; every later run of that peer fails
# fast with exit 7 instead of paying the caller's set-up cost (a panel leader, a dialog) for a run that cannot start.
QUOTA_DIR = STATE / "quota"
QUOTA_TTL = 30 * 60
QUOTA_RE = re.compile(r"out of usage|exceeded your (?:monthly )?quota|Increase limits for faster responses|"
                      r"usage limit reached|rate.?limit(?:ed| exceeded)|insufficient[_ ]quota", re.I)
# Read-only bridged Claude: headless `-p` cannot ask for permission, so anything outside the settings allow-list is denied
# and the peer burns turns looking for another way in (measured 2026-09-26: 449 denials in 194 runs, median 33 turns vs 1).
# These are auto-approved for -m ask/plan; edits stay denied by --disallowed-tools.
CLAUDE_READ_ONLY_BASH = (
    "rg", "git log", "git show", "git grep", "git diff", "git status", "git branch", "git rev-parse", "git blame",
    "git ls-files", "git merge-base", "git worktree list", "git remote", "cat", "head", "tail", "sed -n", "wc", "ls",
    "find", "file", "echo", "cd", "pwd", "diff", "tree", "unzip -l", "unzip -p", "jar tf", "javap", "codegraph",
    "rtk read", "rtk wc", "rtk ls", "rtk grep", "rtk find", "rtk cat", "rtk head", "rtk tail",
)
# Language: agent ↔ agent exchange uses whatever is cheapest and fastest; whatever is RETURNED to the orchestrating
# agent or the user is in the language of the request itself (fallback: AGENT_BRIDGE_LANG).
USER_LANG = os.environ.get("AGENT_BRIDGE_LANG", "English")
PEER_TALK_RULE = ("This exchange is agent-to-agent: use whatever language is cheapest and fastest for you (terse English is "
                  "fine), no pleasantries, no restating the question — only what the other agent needs.")


def lang_of(text: str) -> str:
    """Language the request was written in — Thai when it carries Thai script, else English; used for the returned part."""
    return "Thai" if re.search(r"[\u0e00-\u0e7f]", text or "") else "English"


def lang_rule(request: str = "") -> str:
    lang = lang_of(request) if request else USER_LANG
    return (f"Language: write the part that is returned (the final answer, report, table or note) in {lang} — the language "
            f"the request was written in; code, identifiers, file paths, commands and quoted messages stay as they are. "
            f"Any language is fine while reasoning.")


LANG_RULE = lang_rule()      # default (user's native language) for callers that have no request text at hand

# write mode (bridge-work / ask-agent -m write): the read-only set plus build/test runners and the non-history git verbs.
# Never commit/push/tag — those stay in GIT_DENY, the user owns history.
CLAUDE_WRITE_BASH = CLAUDE_READ_ONLY_BASH + (
    "mvn", "./mvnw", "gradle", "./gradlew", "bun", "npm test", "npm run", "npx", "pnpm test", "pnpm run", "tsc",
    "pytest", "python -m pytest", "python3 -m pytest", "python3 -m unittest", "ruff", "go test", "go build", "cargo test",
    "cargo build", "make", "git add", "git restore", "git checkout --", "git stash list", "mkdir", "touch", "cp", "mv",
    # the rtk PreToolUse hook rewrites runners before the permission check (`python3 -m pytest` → `rtk pytest`), so the
    # rewritten forms must be allowed too or the peer sees "requires approval" (measured 2026-09-26)
    "rtk pytest", "rtk tsc", "rtk gradlew", "rtk cargo", "rtk go", "rtk make", "rtk ruff", "rtk npm", "rtk npx", "rtk pnpm",
    "rtk jest", "rtk vitest", "rtk mypy", "rtk test", "rtk lint", "rtk run",
)



def err_summary(err: Path, fallback: str = "") -> str:
    """First informative stderr line of a run (the peer's own message, not the bridge's dump)."""
    try:
        lines = [ln.strip() for ln in err.read_text(errors="replace").splitlines()]
    except OSError:
        return fallback
    for ln in lines:
        if ln and not ln.startswith(("[bridge]", "ask-agent:", "{", "(node:")):
            return ln[:240]
    return fallback


def quota_hit(*texts: str) -> str | None:
    """The line that says the peer's plan is exhausted, or None."""
    for t in texts:
        for ln in (t or "").splitlines():
            if QUOTA_RE.search(ln):
                return ln.strip()[:240]
    return None


def quota_mark(peer: str, line: str) -> None:
    QUOTA_DIR.mkdir(parents=True, exist_ok=True)
    (QUOTA_DIR / f"{peer}.json").write_text(json.dumps(
        {"peer": peer, "at": int(time.time()), "line": line, "until": int(time.time()) + QUOTA_TTL}))


def quota_clear(peer: str) -> bool:
    """A successful run proves the peer is back (or that the block was model-specific): drop its mark."""
    f = QUOTA_DIR / f"{peer}.json"
    if f.is_file():
        f.unlink()
        return True
    return False


def quota_block(peer: str) -> str | None:
    """Message when `peer` was marked out of quota less than QUOTA_TTL ago; stale marks are removed."""
    f = QUOTA_DIR / f"{peer}.json"
    if not f.is_file():
        return None
    try:
        d = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    left = int(d.get("until", 0)) - int(time.time())
    if left <= 0:
        f.unlink(missing_ok=True)
        return None
    since = dt.datetime.fromtimestamp(int(d.get("at", 0))).strftime("%H:%M")
    return (f"{peer} is out of quota (since {since}: {d.get('line', '')}); runs fail fast for another {left // 60} min "
            f"— `--ignore-quota` to try anyway")


def find_bin(name: str, skip_substr: tuple[str, ...] = ()) -> str | None:
    """First executable `name` on PATH whose path does not contain any of `skip_substr`."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK) and not any(s in str(p) for s in skip_substr):
            return str(p)
    return None


def last_json(raw: str) -> dict | None:
    """Whole output as JSON, else the last JSON object line (stream-safe), else None."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def sum_keys(node: Any, keys: set[str]) -> int:
    """Sum every integer under any of `keys` anywhere in a nested dict (tolerant token counting)."""
    total = 0
    if isinstance(node, dict):
        for k, v in node.items():
            if k in keys and isinstance(v, int):
                total += v
            else:
                total += sum_keys(v, keys)
    elif isinstance(node, list):
        for v in node:
            total += sum_keys(v, keys)
    return total


def version_of(binary: str | None) -> str:
    if not binary:
        return "-"
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        m = re.search(r"\d+\.\d+[\w.\-]*", out.stdout + out.stderr)
        return m.group(0) if m else (out.stdout.strip().splitlines() or ["?"])[0][:40]
    except Exception as exc:  # noqa: BLE001 — status display only
        return f"? ({type(exc).__name__})"


def session_card_form_home(form: str) -> str:
    """Which runtime tree holds the session card for a playbook form. Empty = no card (routine / direct)."""
    if form in ("panel",):
        return "panel"
    if form in ("pipeline", "fix-flow"):
        return "work"
    return ""


def session_card_path(form: str, name: str) -> Path:
    """Canonical path for a new session agreement.md (mkdir the parent before writing)."""
    home = session_card_form_home(form)
    if not home:
        raise ValueError(f"form {form!r} does not write a session card")
    root = PANELS if home == "panel" else WORK
    return root / name / "agreement.md"


def find_session_card(name: str) -> Path | None:
    """Resolve a session card by name: panel/ · work/ · then legacy flat archives."""
    for p in (PANELS / name / "agreement.md", WORK / name / "agreement.md"):
        if p.is_file():
            return p
    for root in (STATE / "agreements", AGREEMENTS_ARCHIVED):
        for candidate in (root / f"{name}.md", root / f"work-{name}.md", root / f"fix-{name}.md"):
            if candidate.is_file():
                return candidate
    return None
