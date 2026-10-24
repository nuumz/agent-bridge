"""One adapter per peer CLI: common options → that tool's flags, and its output → Parsed."""
from __future__ import annotations

import argparse
import atexit
import fcntl
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .core import (BOARD_TOOLS, CURSOR_PULL, STATE, BRIDGE_CMDS, CLAUDE_READ_ONLY_BASH, CLAUDE_WORKTREE_BASH,
                   CLAUDE_WRITE_BASH, DISABLED, GIT_DENY, GIT_DISCARD, HOME, find_bin, last_json, sum_keys)

class Parsed:
    def __init__(self, result: str, session: str | None = None, tok_in: Any = "?", tok_out: Any = "?",
                 is_error: bool = False, extra: str = "", metrics: dict[str, int | float] | None = None):
        self.result, self.session, self.tok_in, self.tok_out, self.is_error, self.extra = (
            result, session, tok_in, tok_out, is_error, extra)
        self.metrics = metrics if metrics is not None else {}


class Adapter:
    name = ""
    binary = ""
    skip_bin_substr: tuple[str, ...] = ()
    supports_worktree = False
    supports_deny = False
    recovery = False          # narration-only detection + auto --resume (Cursor)
    rules_mirror = ""         # where bridge-sync mirrors ~/.claude/CLAUDE.md for this peer

    def resolve(self) -> str | None:
        return find_bin(self.binary, self.skip_bin_substr)

    def auth_hint(self) -> str:
        return ""

    def argv(self, o: argparse.Namespace, prompt: str) -> list[str]:
        raise NotImplementedError

    def resume_argv(self, o: argparse.Namespace, session: str, prompt: str) -> list[str]:
        raise NotImplementedError

    def parse(self, raw: str) -> Parsed:
        raise NotImplementedError

    def prepare(self, o: argparse.Namespace, dry: bool) -> list[str]:
        """Make the machine-wide rules reachable for this run; returns notes for the [bridge] line."""
        return []

    def preamble(self, o: argparse.Namespace) -> str:
        """Text prepended to the prompt when a rules file cannot be injected (e.g. fresh worktree)."""
        return ""

    def finish(self, o: argparse.Namespace) -> None:
        """Undo anything prepare() set up for the run; idempotent, also runs on timeout/signal via atexit."""


class Claude(Adapter):
    name, binary = "claude", "claude"
    supports_worktree = True
    supports_deny = True
    rules_mirror = "~/.claude/CLAUDE.md (source of truth)"

    def _base(self, o: argparse.Namespace) -> list[str]:
        deny = [f"Bash({c}:*)" for c in GIT_DENY] + [f"Bash({c}:*)" for c in BRIDGE_CMDS] + list(o.deny)
        # the caller's checkout: their uncommitted work stays (discard_ok is set by ask-agent for -w / bridge/<name>)
        protect = o.mode == "write" and not getattr(o, "discard_ok", False)
        if protect:
            deny += [f"Bash({c}:*)" for c in GIT_DISCARD]
        if o.board:
            # plan mode blocks every MCP tool (verified 2026-09-12): read-only runs use default mode + explicit edit denies
            mode = {"ask": "default", "plan": "default", "write": "acceptEdits"}[o.mode]
            if o.mode != "write":
                deny += ["Edit", "Write", "MultiEdit", "NotebookEdit"]
        else:
            mode = {"ask": "plan", "plan": "plan", "write": "acceptEdits"}[o.mode]
        # stream-json (+ --verbose, which print mode requires for it): one line per turn while the run is alive, so
        # `bridge-watch` can show what the peer is doing; the final `result` line carries the same fields as the
        # plain json format, so parse() is unchanged (user request 2026-09-26: see each agent while it works)
        a = ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", mode,
             "--disallowed-tools", ",".join(deny)]
        allowed = [BOARD_TOOLS] if o.board else []
        # write mode also pre-approves the build/test runners: headless claude denies any non-allowlisted Bash and a
        # denied `mvn test` turns an implement run into 30 turns of guessing (measured 2026-09-26)
        allowed += [f"Bash({c}:*)" for c in (CLAUDE_WRITE_BASH if o.mode == "write" else CLAUDE_READ_ONLY_BASH)]
        if o.mode == "write" and not protect:
            allowed += [f"Bash({c}:*)" for c in CLAUDE_WORKTREE_BASH]
        if allowed:
            a += ["--allowed-tools", ",".join(allowed)]
        if o.model:
            a += ["--model", o.model]
        for d in o.add_dir:
            a += ["--add-dir", d]
        if o.worktree is not None:
            a += ["--worktree"] + ([o.worktree] if o.worktree != "auto" else [])
        return a

    # --fork-session: a resumed run gets a session id of its own, so a peer never writes into the session it resumes
    # (with `-c` in the caller's own directory that is the caller's session). Conv stores the forked id, so a thread
    # keeps resuming its latest id as before.
    def argv(self, o, prompt):
        a = self._base(o)
        if o.session:
            a += ["--resume", o.session, "--fork-session"]
        elif o.cont:
            a += ["--continue", "--fork-session"]
        return a + ["--", prompt]

    def resume_argv(self, o, session, prompt):
        return self._base(o) + ["--resume", session, "--fork-session", "--", prompt]

    def preamble(self, o):
        if o.mode == "write":
            return ("(Headless write run: file edits are auto-approved; git commit/push/tag are denied and a denied command "
                    "cannot be approved, so use ONE simple command per Bash call — no `for` loops, no `VAR=…;` prefixes, "
                    "no `&&` chains. Pre-approved: the read-only set (rg, git log/show/diff, cat/sed -n, find, ls) plus build/"
                    "test runners (mvn, gradle, bun, npm test/run, npx, tsc, pytest, python -m pytest, ruff, go, cargo, make) "
                    "and git add. Touch only the files the task names; leave everything uncommitted."
                    + ("" if getattr(o, "discard_ok", False) else
                       " This is the caller's checkout: git restore/checkout/switch/stash/reset/clean are denied — "
                       "their uncommitted work stays as it is.") + ")\n\n")
        return ("(Headless read-only run: a denied command cannot be approved, so use ONE simple command per Bash call — "
                "no `for` loops, no `VAR=…;` prefixes, no `&&` chains, no python heredocs. Pre-approved: rg, git "
                "log/show/grep/diff/blame, cat/head/tail/sed -n, find, ls, wc, unzip -l/-p, javap, rtk read. Builds (mvn), "
                "scripts (python3) and file writes are not — do not attempt them; say what you could not check instead.)\n\n")

    def parse(self, raw):
        obj = last_json(raw)
        if obj is None:
            return Parsed(raw)
        u = obj.get("usage") or {}
        # turns × the context each turn re-sends, and what the run newly pulled into context (cache_write), drive cost;
        # tool calls (the `tool_use` blocks of the assistant events) say how much the run actually did
        return Parsed(str(obj.get("result", raw)), obj.get("session_id"), u.get("input_tokens", "?"),
                      u.get("output_tokens", "?"), bool(obj.get("is_error")),
                      f"cost={obj.get('total_cost_usd', '?')} turns={obj.get('num_turns', '?')} "
                      f"cache_write={u.get('cache_creation_input_tokens', '?')} tools={tool_calls(raw)}")


def tool_calls(raw: str) -> int:
    """Number of tool_use blocks in a stream-json transcript (0 for a plain JSON result)."""
    n = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{") or '"tool_use"' not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            n += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")
    return n


class Cursor(Adapter):
    DEFAULT_MODEL = "auto"          # cursor-agent --list-models: "auto - Auto (default)"; -M overrides per run
    name, binary = "cursor", "cursor-agent"
    supports_worktree = True
    recovery = True
    rules_mirror = "~/.cursor/rules/10-user-preferences.mdc"

    def auth_hint(self):
        try:
            cfg = json.loads((HOME / ".cursor/cli-config.json").read_text())
            email = (cfg.get("authInfo") or {}).get("email")
            model = (cfg.get("model") or {}).get("modelId", "default")
            deny = (cfg.get("permissions") or {}).get("deny") or []
            return f"login={email or 'NONE'} model={self.DEFAULT_MODEL} (app pick: {model}) deny={','.join(deny) or 'NONE'}"
        except Exception:  # noqa: BLE001
            return "no ~/.cursor/cli-config.json"

    # cursor-agent reads <repo>/.cursor/rules/*.mdc and AGENTS.md but NOT ~/.cursor/rules (verified 2026-09-12),
    # so the machine-wide mirror is linked into the workspace before each run and kept out of git via info/exclude.
    RULE_FILES = ("00-agent-bridge.mdc", "10-user-preferences.mdc")

    # Decision 2026-09-12: Cursor runs in its DEFAULT agent mode (not --mode ask) on the latest Grok. Headless default
    # mode creates files without --force (--force only gates shell), and only the GLOBAL ~/.cursor/cli-config.json deny
    # list is honoured (a project .cursor/cli.json is ignored) — verified on the real CLI. So a read-only run holds a
    # temporary deny set there: flock + pid refcount in cursor-deny.json, restored byte-identical when the last live
    # holder leaves; a `-m write` run refuses to start while the deny is held (its edits would be silently blocked).
    #
    # Decision 2026-10-13: do NOT deny Shell(*). That blocked git log/diff/blame and burned 50–140 tool calls
    # per ask run. Allowlist cannot override Shell(*) (verified 2026-09-12). Keep Write(**) + denials for the known
    # Write bypass (printf > file) and common filesystem mutators. Residual: unrestricted shell can still write via
    # python/node/redirection — ask preamble tells the peer to use read-only shell only.
    DENY_ENTRIES = (
        "Write(**)",
        "Shell(printf)", "Shell(printf *)",
        "Shell(tee)", "Shell(tee *)",
        "Shell(cp)", "Shell(cp *)",
        "Shell(mv)", "Shell(mv *)",
        "Shell(rm)", "Shell(rm *)",
        "Shell(rmdir)", "Shell(rmdir *)",
        "Shell(touch)", "Shell(touch *)",
        "Shell(mkdir)", "Shell(mkdir *)",
        "Shell(chmod)", "Shell(chmod *)",
        "Shell(chown)", "Shell(chown *)",
        "Shell(ln)", "Shell(ln *)",
        "Shell(install)", "Shell(install *)",
        "Shell(dd)", "Shell(dd *)",
        "Shell(truncate)", "Shell(truncate *)",
    )
    # a write run (`--force`: everything but the global deny) holds these instead: history stays the user's, and in
    # the caller's checkout (not discard_ok) their uncommitted work stays
    WRITE_ENTRIES = tuple(f"Shell({c}{s})" for c in GIT_DENY for s in ("", " *"))
    DISCARD_ENTRIES = tuple(f"Shell({c}{s})" for c in GIT_DISCARD for s in ("", " *"))
    DENY_STATE = STATE / "cursor-deny.json"
    DENY_LOCK = STATE / "cursor-deny.lock"
    CLI_CONFIG = HOME / ".cursor" / "cli-config.json"
    _LEGACY_SHELL_STAR = "Shell(*)"
    _held = False

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _deny_state(self, fn: Callable[[dict], Any]) -> Any:
        """Run fn(state) under the lock; sweeps dead holders first and restores the config if they were the last."""
        self.DENY_LOCK.parent.mkdir(parents=True, exist_ok=True)
        with open(self.DENY_LOCK, "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            state = {"pids": [], "added": False, "orig": ""}
            if self.DENY_STATE.is_file():
                try:
                    state.update(json.loads(self.DENY_STATE.read_text()))
                except json.JSONDecodeError:
                    pass
            state["pids"] = [q for q in state["pids"] if self._alive(q)]
            state["ask"] = [q for q in state.get("ask", []) if q in state["pids"]]
            if not state["pids"] and state["added"]:          # holders died without cleanup — put the user's file back
                self._deny_restore(state)
            result = fn(state)
            self.DENY_STATE.write_text(json.dumps(state))
            return result

    def _deny_acquire(self, entries: tuple[str, ...] | None = None, what: str = "") -> str:
        """Hold `entries` (default: the ask-mode set) in the global deny until the last holder leaves. Holds overlap:
        each run adds what its set still lacks, the union is dropped when the last pid is gone."""
        entries = entries or self.DENY_ENTRIES
        what = what or "`Write(**)` + write-capable Shell denials"

        def fn(state):
            existed = self.CLI_CONFIG.is_file()
            text = self.CLI_CONFIG.read_text() if existed else "{}"
            cfg = json.loads(text or "{}")
            deny = cfg.setdefault("permissions", {}).setdefault("deny", [])
            # Drop leftover Shell(*) from pre-2026-10-13 holds so read-only shell works; do not put it back on restore.
            if not state["pids"] and self._LEGACY_SHELL_STAR in deny:
                deny[:] = [e for e in deny if e != self._LEGACY_SHELL_STAR]
                text = json.dumps(cfg, indent=2) + "\n"
                self.CLI_CONFIG.write_text(text)
            missing = [e for e in entries if e not in deny]
            if missing:                                         # never rewrite a file that already denies these
                # cursor-agent rewrites this file itself; once it did between two holds, the byte-identical restore
                # would put the first holder's snapshot back over that edit — drop only our entries then
                edited = state["added"] and text != state["written"]
                deny.extend(missing)
                written = json.dumps(cfg, indent=2) + "\n"
                # a machine where cursor-agent is on PATH but ~/.cursor does not exist yet (a fresh install, a CI
                # runner): the hold creates the file and the last holder out removes it again (orig=None)
                self.CLI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                self.CLI_CONFIG.write_text(written)
                state.update(added=True, orig=state["orig"] if state["added"] else (text if existed else None),
                             written="" if edited else written, entries=state.get("entries", []) + missing)
            if os.getpid() not in state["pids"]:
                state["pids"].append(os.getpid())
            if "Write(**)" in entries and os.getpid() not in state.setdefault("ask", []):
                state["ask"].append(os.getpid())            # the holders a write run must not start under
            return f"cursor: {what} held (Shell(*) not denied — git/rg/cat ok; holders: {state['pids']})"
        note = self._deny_state(fn)
        if not self._held:
            self._held = True
            atexit.register(self.finish, None)
        return note

    def _deny_holders(self) -> list[int]:
        """Pids holding the ask-mode set — the one that would block a write run's edits; a write hold is not one."""
        return self._deny_state(lambda state: list(state["ask"]))

    def finish(self, o):
        if not self._held:
            return
        self._held = False

        def fn(state):
            state["pids"] = [q for q in state["pids"] if q != os.getpid()]
            state["ask"] = [q for q in state.get("ask", []) if q != os.getpid()]
            if not state["pids"] and state["added"]:
                self._deny_restore(state)
        self._deny_state(fn)

    def _deny_restore(self, state: dict) -> None:
        """Last holder out: byte-identical restore when nobody else touched the file since we wrote it; otherwise
        (cursor-agent rewrites cli-config.json itself — model choice, auth refresh) drop only the entries we added."""
        current = self.CLI_CONFIG.read_text() if self.CLI_CONFIG.is_file() else ""
        if current == state.get("written", ""):
            if state.get("orig") is None:                   # the file is the hold's own: nothing to put back
                self.CLI_CONFIG.unlink(missing_ok=True)
            else:
                self.CLI_CONFIG.write_text(state["orig"])
        else:
            try:
                cfg = json.loads(current or "{}")
                deny = (cfg.get("permissions") or {}).get("deny") or []
                keep = [e for e in deny if e not in state.get("entries", [])]
                if keep != deny:
                    cfg["permissions"]["deny"] = keep
                    self.CLI_CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
            except json.JSONDecodeError:
                pass                                            # unreadable: leave the user's file alone
        state.update(added=False, orig="", written="", entries=[])

    def prepare(self, o, dry):
        notes = []
        if o.mode == "ask" and not dry:
            notes.append(self._deny_acquire())
        elif o.mode == "write":
            holders = [q for q in self._deny_holders() if q != os.getpid()]
            if holders:
                print(f"ask-agent: cursor -m write refused — read-only agent-mode run(s) {holders} hold the global "
                      f"Write(**) deny (+ write-capable Shell denials), which would silently block this run. "
                      f"Retry when they finish.",
                      file=sys.stderr)
                raise SystemExit(6)
            if not dry:
                protect = not getattr(o, "discard_ok", False)
                notes.append(self._deny_acquire(self.WRITE_ENTRIES + (self.DISCARD_ENTRIES if protect else ()),
                                                "git commit/push/tag" + (" + restore/checkout/switch/stash/reset/clean "
                                                                         "(the caller's checkout)" if protect else "")))
        top = subprocess.run(["git", "-C", o.dir, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        root = Path(top.stdout.strip()) if top.returncode == 0 else Path(o.dir)
        rules = root / ".cursor" / "rules"
        for name in self.RULE_FILES:
            src, dst = HOME / ".cursor" / "rules" / name, rules / name
            if not src.is_file():
                continue
            if dst.is_symlink() and dst.resolve() == src.resolve():
                continue
            if dst.exists() or dst.is_symlink():
                notes.append(f"rules: {dst} exists (not the bridge link) — left alone")
                continue
            if dry:
                notes.append(f"would link {dst} -> {src}")
                continue
            rules.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)
            notes.append(f"linked {dst.relative_to(root)}")
        # `--git-path` resolves the real gitdir, so a linked worktree (.git is a file there) gets the right exclude
        gp = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"], capture_output=True, text=True)
        exclude = (root / gp.stdout.strip()).resolve() if gp.returncode == 0 and gp.stdout.strip() else root / ".git" / "info" / "exclude"
        if top.returncode == 0 and not dry:
            lines = exclude.read_text().splitlines() if exclude.is_file() else []
            want = [f".cursor/rules/{n}" for n in self.RULE_FILES if f".cursor/rules/{n}" not in lines]
            if want:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                with open(exclude, "a") as f:
                    f.write("# agent-bridge rule links (local only)\n" + "\n".join(want) + "\n")
                notes.append("git info/exclude updated")
        return notes

    def preamble(self, o):
        bits = []
        if o.mode == "ask":
            bits.append(
                "Read-only run: do not create or modify files. Shell is for read-only commands only "
                "(git log/show/diff/status/blame/grep, rg, cat, head, tail, sed -n, find, ls, wc, unzip -l|-p). "
                "Write tool is denied. Put the answer in the reply body with file:line evidence.\n\n"
            )
        if o.worktree is not None:
            bits.append(
                "Before anything else, read and follow ~/.agent-bridge/state/mirror/agent-bridge.md and "
                "~/.agent-bridge/state/mirror/user-rules.md (machine-wide rules; this worktree has no copy).\n\n"
            )
        if o.board and o.conv:          # only a --conv run has a topic to pull from; without one the protocol says [[ASK]]
            bits.append(CURSOR_PULL + "\n\n")
        return "".join(bits)

    def _base(self, o):
        # stream-json: one `assistant` line per turn, so narration and the answer stay separate chunks
        # (the `result` field of the plain json format glues every chunk together with no separator)
        a = ["-p", "--output-format", "stream-json", "--trust", "--workspace", o.dir]
        # ask = default agent mode (no --mode) with Write(**)+write-capable Shell denials — see prepare()
        a += {"ask": [], "plan": ["--mode", "plan"], "write": ["--force"]}[o.mode]
        if o.board:
            a += ["--approve-mcps"]                       # headless cursor-agent loads no MCP server without this
        # always pass a model: without --model cursor-agent takes the app's last pick from cli-config.json (grok-4.6 on
        # 2026-09-26, which ran out of usage) — the bridge starts on `auto` unless -M says otherwise
        a += ["--model", o.model or self.DEFAULT_MODEL]
        for d in o.add_dir:
            a += ["--add-dir", d]
        return a

    def argv(self, o, prompt):
        a = self._base(o)
        if o.worktree is not None:
            a += ["--worktree"] + ([o.worktree] if o.worktree != "auto" else [])
        if o.session:
            a += ["--resume", o.session]
        elif o.cont:
            a += ["--continue"]
        return a + ["--", prompt]

    def resume_argv(self, o, session, prompt):
        return self._base(o) + ["--resume", session, "--", prompt]

    def parse(self, raw):
        obj = last_json(raw)
        if obj is None:
            return Parsed(raw)
        chunks = []
        for line in raw.splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and d.get("type") == "assistant":
                chunks += [c.get("text", "") for c in (d.get("message") or {}).get("content") or [] if c.get("type") == "text"]
        result = "\n\n".join(c.strip() for c in chunks if c.strip()) or str(obj.get("result", raw) or "")
        u = obj.get("usage") or {}
        return Parsed(result, obj.get("session_id"), u.get("inputTokens", "?"), u.get("outputTokens", "?"),
                      bool(obj.get("is_error")))



class Copilot(Adapter):
    """GitHub Copilot CLI (`npm i -g @github/copilot`). The VS Code shim of the same name is skipped:
    it is interactive (offers to install) and would hang a headless run."""
    name, binary = "copilot", "copilot"
    skip_bin_substr = ("github.copilot-chat/copilotCli",)
    supports_deny = True
    rules_mirror = "~/.copilot/instructions/agent-bridge-user-rules.instructions.md"

    def auth_hint(self):
        try:
            cfg = json.loads(re.sub(r"^\s*//.*$", "", (HOME / ".copilot/config.json").read_text(), flags=re.M))
            return f"login={(cfg.get('lastLoggedInUser') or {}).get('login', 'NONE')}"
        except Exception:  # noqa: BLE001
            return "no ~/.copilot/config.json"

    def preamble(self, o):
        cwd = Path(o.dir).resolve()
        root = next((p for p in (cwd, *cwd.parents) if (p / ".git").exists()), None)
        parents = [cwd]
        while parents[-1] != (root or cwd):
            parents.append(parents[-1].parent)
        instructions = [
            p / name for p in reversed(parents)
            for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md")
            if (p / name).is_file()
        ]
        roots = [cwd, *((cwd / Path(d).expanduser()).resolve() for d in o.add_dir)]
        outside = " (outside file access — rerun with -d <repo root> or --add-dir to let the CLI read it)"
        inventory = "\n".join(
            f"- {p}" + ("" if any(p.is_relative_to(r) for r in roots) else outside) for p in instructions
        ) or "- no repo-level instruction file found"
        access = ", ".join(str(r) for r in roots)
        mode = (
            "Read-only: shell and file writes are denied, including builds/tests. Use native glob, rg and view tools; "
            "do not attempt bash even for git status/diff. If the task requires shell evidence, report that limit."
            if o.mode != "write" else
            "Write: edit only the task's allowed file set, run its verification command, and report the actual output. "
            "Do not stage, commit, push or tag. An analysis alone is not an implementation."
        )
        return (
            f"(Bridge workspace bootstrap\nWorking directory: {cwd}\n"
            f"Repository root: {root or '(not a git checkout)'}\n"
            f"File access roots (not a grant beyond CLI permissions): {access}\n"
            f"Existing instruction files to check before work:\n{inventory}\n"
            "Copilot discovers personal instructions and skills itself; also follow applicable path-scoped repo rules. "
            "This inventory is not proof they loaded. Do not create or copy instructions, or build a knowledge graph "
            "just to prepare the workspace unless the task requests it.\n"
            f"{mode}\n"
            "Discover paths before reading them; do not guess a workspace layout. A denied tool/path cannot be approved "
            "in this headless run: do not retry it or bypass it. Continue with authorized evidence; if essential evidence "
            "is outside these roots, end with [[ASK]] naming the needed path and why. "
            "Use the task's requested output format, cite file:line for findings, distinguish facts from hypotheses, "
            "and name unverified checks. Do not delegate a task you can finish directly.)\n\n"
        )

    def _base(self, o):
        # JSONL (`--output-format json`, CLI 1.0.88): `assistant.message` events carry the answer, the final `result`
        # event the session id + usage — before 2026-09-26 the bridge used `-s` plain text and Copilot runs had no
        # session to resume and no duration/cost in the run logs
        a = ["--no-ask-user", "--output-format", "json"]
        if o.mode == "write":
            a += ["--allow-all-tools"]
            for c in GIT_DENY + (() if getattr(o, "discard_ok", False) else GIT_DISCARD):
                a += [f"--deny-tool=shell({c})", f"--deny-tool=shell({c}:*)"]
        else:
            a += ["--deny-tool=write", "--deny-tool=shell"]
        for t in o.deny:
            a += [f"--deny-tool={t}"]
        if o.model:
            a += [f"--model={o.model}"]
        for d in o.add_dir:
            a += [f"--add-dir={d}"]
        return a

    def argv(self, o, prompt):
        a = self._base(o)
        if o.session:
            a += [f"--resume={o.session}"]
        elif o.cont:
            a += ["--continue"]
        return a + ["-p", prompt]

    def resume_argv(self, o, session, prompt):
        return self._base(o) + [f"--resume={session}", "-p", prompt]

    def parse(self, raw):
        events = []
        for line in raw.splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and d.get("type"):
                events.append(d)
        if not events:                                   # older CLI or `-s` output: plain text
            return Parsed(raw.strip(), is_error=not bool(raw.strip()))
        final = [e for e in events if e.get("type") == "assistant.message"]
        answer = [(e.get("data") or {}).get("content", "") for e in final if (e.get("data") or {}).get("phase", "final_answer") == "final_answer"]
        text = "\n\n".join(t.strip() for t in answer if t and t.strip()) or \
            "\n\n".join(t.strip() for t in ((e.get("data") or {}).get("content", "") for e in final) if t and t.strip())
        res = next((e for e in reversed(events) if e.get("type") == "result"), {})
        u = res.get("usage") or {}
        edited = ((u.get("codeChanges") or {}).get("filesModified")) or []
        failures = [e.get("data") or {} for e in events
                    if e.get("type") == "tool.execution_complete" and (e.get("data") or {}).get("success") is False]
        metrics = {
            "tool_calls": sum(e.get("type") == "tool.execution_start" for e in events),
            "tool_failures": len(failures),
            "permission_denials": sum((e.get("error") or {}).get("code") == "denied" for e in failures),
        }
        for source, target in (("premiumRequests", "premium_requests"), ("totalApiDurationMs", "api_duration_ms")):
            if isinstance(u.get(source), (int, float)) and not isinstance(u[source], bool):
                metrics[target] = u[source]
        if isinstance((u.get("codeChanges") or {}).get("filesModified"), list):
            metrics["files_modified"] = len(edited)
        extra = f"premium={u.get('premiumRequests', '?')} api={u.get('totalApiDurationMs', '?')}ms"
        extra += f" tool_failures={metrics['tool_failures']} permission_denials={metrics['permission_denials']}"
        if edited:
            extra += f" EDITED={','.join(edited)}"
        is_error = not res or res.get("exitCode", 0) not in (0, None) or not text
        if not res:
            text = "Copilot stream incomplete: no terminal result event.\n\n" + text
        # the JSONL stream carries no token counts — premiumRequests is billing units, kept in `extra`, never tok_out
        return Parsed(text or "(copilot returned no assistant message)", res.get("sessionId"), "?", "?", is_error,
                      extra, metrics)


ADAPTERS: dict[str, Adapter] = {a.name: a for a in (Claude(), Cursor(), Copilot())}
