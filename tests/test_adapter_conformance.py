"""Adapter conformance kit: every peer adapter in ADAPTERS goes through the same checks against the stub CLIs in
tests/fakebin/ — the flags it must emit per mode, in its own syntax (EXPECT), and the whole run through `ask-agent`:
spawn and the [bridge] line, argv identical to --dry-run, the peer's environment, parse of the fixture, a crash, a
quota message (exit 7 + fail-fast + --ignore-quota), a timeout that kills the process group, an ask-back (exit 5),
session resume, --add-dir / -M pass-through, the version probe. A new family passes by adding its stub + fixture under
tests/fakebin/ and one row in EXPECT; a renamed flag fails here, not in a run log."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
FAKEBIN = ROOT / "tests" / "fakebin"
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(FAKEBIN))

import _fakebin  # noqa: E402
from bridgelib import core  # noqa: E402
from bridgelib.adapters import ADAPTERS  # noqa: E402

PROMPT = "Review src/retry.py for retry bugs."


class Within(tuple):
    """(flag, entry): `entry` is one of the comma-joined values that follows `flag` — how Claude lists tools."""


def present(argv: list[str], item) -> bool:
    if isinstance(item, Within):
        flag, entry = item
        return flag in argv and entry in argv[argv.index(flag) + 1].split(",")
    if isinstance(item, tuple):                            # a contiguous subsequence of argv
        n = len(item)
        return any(tuple(argv[i:i + n]) == item for i in range(len(argv) - n + 1))
    return item in argv                                    # one whole argument


# What each family's CLI must be told, in that CLI's own syntax. `modes`: per bridge mode what must be present and
# what must not (board off); `board_ask`: the same for -m ask with the board on; `git_deny(cmd)`: how the CLI is
# told to deny a git command (None: the family has no deny flag — then `hold` says which entries of its own config
# each mode must hold, as the stub observes them during the run, or `no_commit` names the mechanism, e.g. a sandbox);
# `quota`: a line the CLI prints when the plan is exhausted (must match core.QUOTA_RE); `prompt_flag`: the argument
# right before the prompt.
EXPECT = {
    "claude": dict(
        output=("--output-format", "stream-json"), prompt_flag="--",
        modes={"ask": dict(has=[("--permission-mode", "plan")], not_=[("--permission-mode", "acceptEdits")]),
               "plan": dict(has=[("--permission-mode", "plan")], not_=[("--permission-mode", "acceptEdits")]),
               "write": dict(has=[("--permission-mode", "acceptEdits")], not_=[("--permission-mode", "plan")])},
        board_ask=dict(has=[("--permission-mode", "default"), Within(("--allowed-tools", core.BOARD_TOOLS)),
                            Within(("--disallowed-tools", "Edit")), Within(("--disallowed-tools", "Write"))],
                       not_=[("--permission-mode", "acceptEdits")]),
        git_deny=lambda c: Within(("--disallowed-tools", f"Bash({c}:*)")), hold=None,
        add_dir=lambda d: ("--add-dir", d), model=lambda m: ("--model", m),
        resume=lambda s: ("--resume", s), cont="--continue",
        quota="Claude AI usage limit reached|1761350400",
    ),
    "cursor": dict(
        output=("--output-format", "stream-json"), prompt_flag="--",
        modes={"ask": dict(has=[], not_=["--force", "--mode"]),
               "plan": dict(has=[("--mode", "plan")], not_=["--force"]),
               "write": dict(has=["--force"], not_=["--mode"])},
        board_ask=dict(has=["--approve-mcps"], not_=["--force"]),
        git_deny=None, hold={"ask": ["Write(**)"], "write": [f"Shell({c})" for c in core.GIT_DENY]},
        add_dir=lambda d: ("--add-dir", d), model=lambda m: ("--model", m),
        resume=lambda s: ("--resume", s), cont="--continue",
        quota="You're out of usage. Upgrade your plan or wait for the limit to reset.",
    ),
    "copilot": dict(
        output=("--output-format", "json"), prompt_flag="-p",
        modes={"ask": dict(has=["--deny-tool=write", "--deny-tool=shell"], not_=["--allow-all-tools"]),
               "plan": dict(has=["--deny-tool=write", "--deny-tool=shell"], not_=["--allow-all-tools"]),
               "write": dict(has=["--allow-all-tools"], not_=["--deny-tool=write", "--deny-tool=shell"])},
        board_ask=dict(has=["--deny-tool=write"], not_=["--allow-all-tools"]),
        git_deny=lambda c: f"--deny-tool=shell({c})", hold=None,
        add_dir=lambda d: f"--add-dir={d}", model=lambda m: f"--model={m}",
        resume=lambda s: f"--resume={s}", cont="--continue",
        quota="You have exceeded your monthly quota of premium requests.",
    ),
    "codex": dict(
        output=("exec", "--json"), prompt_flag="--",
        modes={"ask": dict(has=[("--sandbox", "read-only"), "--skip-git-repo-check"],
                           not_=[("--sandbox", "workspace-write"), "--dangerously-bypass-approvals-and-sandbox"]),
               "plan": dict(has=[("--sandbox", "read-only")], not_=[("--sandbox", "workspace-write")]),
               "write": dict(has=[("--sandbox", "workspace-write")],
                             not_=[("--sandbox", "read-only"), "--dangerously-bypass-approvals-and-sandbox"])},
        board_ask=dict(has=[("--sandbox", "read-only"), ("-c", 'mcp_servers.bridge_board.args=["serve"]')],
                       not_=[("--sandbox", "workspace-write")]),
        git_deny=None, hold=None,
        no_commit="no deny flag: the workspace-write sandbox keeps .git/ read-only, so a commit cannot land; a dirty "
                  "checkout the bridge does not own is refused (exit 6, tests/test_codex.py)",
        add_dir=None, model=lambda m: ("--model", m),     # an extra root would be writable under workspace-write
        resume=lambda s: ("resume", s), cont=("resume", "--last"),
        quota="You've hit your usage limit. Try again in 3 hours.",
    ),
    "a2a": dict(
        output="send", prompt_flag="--",
        modes={"ask": dict(has=[("--mode", "ask")], not_=[("--mode", "write")]),
               "plan": dict(has=[("--mode", "plan")], not_=[("--mode", "write")]),
               "write": dict(has=[("--mode", "write")], not_=[("--mode", "ask")])},
        board_ask=dict(has=[("--mode", "ask")], not_=[("--mode", "write")]),
        git_deny=None, hold=None,
        no_commit="the remote agent's own rails: the bridge sends text, a mode and a shape, never a credential, a "
                  "path grant or a command",
        add_dir=None, model=lambda m: ("--model", m),
        resume=lambda s: ("--context", s), cont=("--context", "last"),
        quota="[a2a] failed: You've hit your usage limit. Try again in 3 hours.",
    ),
}


def per_family(fn):
    """Run `fn(self, name, agent, expect)` once per registered family, each as its own subTest."""
    def test(self):
        for name, agent in ADAPTERS.items():
            with self.subTest(family=name):
                fn(self, name, agent, EXPECT[name])
    test.__name__, test.__doc__ = fn.__name__, fn.__doc__
    return test


def alive(pid: int) -> bool:
    """A process that still runs — a zombie (dead, not yet reaped by a container's init) does not count."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


class Conformance(unittest.TestCase):
    """Every check runs once per family with the stubs first on PATH and a temporary HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.work, self.extra = root / "home", root / "work", root / "spec"
        for d in (self.home, self.work, self.extra):
            d.mkdir()
        self.bindir = root / "fakebin"
        shutil.copytree(FAKEBIN, self.bindir)              # a copy: pycache and chmod stay out of the checkout
        for name in _fakebin.VERSIONS:
            (self.bindir / name).chmod(0o755)
        self.log = root / "calls.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BRIDGE_")}
        env.update({"PATH": f"{self.bindir}{os.pathsep}{env.get('PATH', '')}", "HOME": str(self.home),
                    "USER": "tester", "FAKEBIN_LOG": str(self.log)})
        env.update(extra)
        return env

    def ask(self, *args: str, timeout: int = 60, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(BIN / "ask-agent"), *args], capture_output=True, text=True,
                              env=self.env(**extra), cwd=str(self.work), timeout=timeout)

    def dry(self, family: str, *args: str) -> list[str]:
        p = self.ask(family, "--dry-run", "-d", str(self.work), *args, PROMPT)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["argv"]

    def calls(self) -> list[dict]:
        return [json.loads(ln) for ln in self.log.read_text().splitlines()] if self.log.is_file() else []

    def list_row(self, name: str, **extra: str) -> str:
        listing = self.ask("--list", **extra)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        return next(ln for ln in listing.stdout.splitlines() if ln.startswith(f"{name} "))

    # -- the table itself
    def test_every_adapter_has_a_stub_a_fixture_and_a_row_in_expect(self):
        self.assertEqual(set(EXPECT), set(ADAPTERS), "one EXPECT row per registered family, no stale rows")
        for name, agent in ADAPTERS.items():
            with self.subTest(family=name):
                self.assertTrue((FAKEBIN / agent.binary).is_file(), f"tests/fakebin/{agent.binary} is missing")
                self.assertTrue((FAKEBIN / "fixtures" / f"{agent.binary}.jsonl").is_file(), "fixture missing")
                self.assertIn(agent.binary, _fakebin.VERSIONS)
                self.assertTrue(core.QUOTA_RE.search(EXPECT[name]["quota"]), "quota line must match core.QUOTA_RE")

    # -- argv per mode (board off)
    @per_family
    def test_modes_read_only_flags_and_write_flags(self, name, agent, x):
        for mode, rule in x["modes"].items():
            argv = self.dry(name, "-m", mode, "--no-board")
            self.assertTrue(present(argv, x["output"]), (mode, argv))
            for item in rule["has"]:
                self.assertTrue(present(argv, item), (mode, item, argv))
            for item in rule["not_"]:
                self.assertFalse(present(argv, item), (mode, item, argv))
            self.assertEqual(argv[-2], x["prompt_flag"], "the prompt is the last argument, after its flag")
            self.assertIn(PROMPT, argv[-1])
            self.assertIn("Bridge protocol", argv[-1], "the protocol suffix rides on the prompt")
            if agent.supports_deny and mode == "write":
                for cmd in core.GIT_DENY:               # history stays the user's
                    self.assertTrue(present(argv, x["git_deny"](cmd)), (cmd, argv))
                for cmd in core.GIT_DISCARD:            # the caller's checkout: their uncommitted work stays
                    self.assertTrue(present(argv, x["git_deny"](cmd)), (cmd, argv))
                for cmd in core.BRIDGE_CMDS:            # one hop: no bridge command, local or over the network
                    self.assertTrue(present(argv, x["git_deny"](cmd)), (cmd, argv))
            if not agent.supports_deny:
                self.assertIsNone(x["git_deny"], "a family without a deny flag declares a hold or a sandbox instead")
                self.assertTrue(x["hold"] or x.get("no_commit"),
                                "…and the row says how a commit is prevented: `hold` (config entries the stub "
                                "observes) or `no_commit` (the mechanism, e.g. a sandbox)")

    @per_family
    def test_board_on_ask_keeps_the_run_read_only_and_reaches_the_board(self, name, agent, x):
        argv = self.dry(name, "-m", "ask")
        for item in x["board_ask"]["has"]:
            self.assertTrue(present(argv, item), (item, argv))
        for item in x["board_ask"]["not_"]:
            self.assertFalse(present(argv, item), (item, argv))

    @per_family
    def test_add_dir_model_and_session_pass_through(self, name, agent, x):
        argv = self.dry(name, "--no-board", "--add-dir", str(self.extra), "-M", "model-x", "--session", "sess-1")
        if x["add_dir"] is None:                             # no path grant for this peer, asked for or not
            self.assertNotIn(str(self.extra), " ".join(argv), argv)
        else:
            self.assertTrue(present(argv, x["add_dir"](str(self.extra))), argv)
        self.assertTrue(present(argv, x["model"]("model-x")), argv)
        self.assertTrue(present(argv, x["resume"]("sess-1")), argv)
        self.assertFalse(present(argv, x["cont"]), argv)
        latest = self.dry(name, "--no-board", "-c")
        self.assertTrue(present(latest, x["cont"]), latest)
        self.assertFalse(present(latest, x["resume"]("sess-1")), latest)
        self.assertNotIn(str(self.extra), " ".join(latest), "no grant that was not asked for")

    # -- parse
    @per_family
    def test_parse_reads_the_fixture(self, name, agent, x):
        parsed = agent.parse(_fakebin.fixture(agent.binary))
        self.assertFalse(parsed.is_error, parsed.result)
        self.assertIn(_fakebin.ANSWER, parsed.result)
        self.assertTrue(parsed.session, "the session id is what --session resumes")
        for tok in (parsed.tok_in, parsed.tok_out):
            self.assertTrue(tok == "?" or (isinstance(tok, int) and tok >= 0), tok)

    # -- the whole run
    @per_family
    def test_run_answers_with_the_bridge_line_and_the_dry_run_argv(self, name, agent, x):
        expected = self.dry(name)
        before = len(self.calls())
        p = self.ask(name, "-d", str(self.work), PROMPT, AGENT_BRIDGE_A2A_TOKEN="s3cret-token")
        self.assertEqual(p.returncode, 0, p.stderr)
        parsed = agent.parse(_fakebin.fixture(agent.binary))
        self.assertIn(parsed.result, p.stdout)
        self.assertIn(f"[bridge] agent={name} session={parsed.session} tokens=in:{parsed.tok_in}/out:{parsed.tok_out}",
                      p.stdout)
        self.assertEqual(len(self.calls()), before + 1, "one spawn per run")
        call = self.calls()[before]
        self.assertEqual(call["argv"], expected, "the run sends exactly what --dry-run showed")
        self.assertEqual(call["cwd"], str(self.work.resolve()))
        self.assertIn(core.HOP_VAR, call["env"], "the peer is told it is one hop in")
        self.assertRegex(call["env"].get(core.IDENTITY_VAR, ""), rf"^{name}#\d+$")
        self.assertFalse([k for k in call["env"] if k.startswith(core.PRIVATE_ENV_PREFIXES)],
                         "the caller's session identity never reaches the peer")
        self.assertEqual("AGENT_BRIDGE_A2A_TOKEN" in call["env"], name == "a2a",
                         "the A2A token reaches the a2a run and no other peer (a prompt-injected peer could print it)")
        self.assertNotIn("s3cret-token", p.stdout + p.stderr)

    @per_family
    def test_config_hold_during_the_run_and_released_after(self, name, agent, x):
        if not x["hold"]:
            self.skipTest("this family denies through flags, not a config hold")
        cfg = self.home / ".cursor" / "cli-config.json"
        for mode, entries in x["hold"].items():
            before = len(self.calls())
            p = self.ask(name, "-m", mode, "--no-board", "-d", str(self.work), PROMPT)
            self.assertEqual(p.returncode, 0, p.stderr)
            seen = json.loads(self.calls()[before]["cursor_config"] or "{}")
            deny = (seen.get("permissions") or {}).get("deny") or []
            for e in entries:
                self.assertIn(e, deny, (mode, deny))
            self.assertFalse(cfg.exists(), "a config file the hold created on a fresh HOME is removed again")

    @per_family
    def test_crash_exit_code_and_the_peers_own_first_stderr_line(self, name, agent, x):
        p = self.ask(name, "--no-board", "-d", str(self.work), PROMPT, FAKEBIN_EXIT="1", FAKEBIN_STDOUT="",
                     FAKEBIN_STDERR="Error: config.json is not valid JSON\n    at parse (cli.js:10)")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("[bridge] error: Error: config.json is not valid JSON", p.stderr)

    @per_family
    def test_quota_exit_7_fail_fast_and_ignore_quota(self, name, agent, x):
        p = self.ask(name, "--no-board", "-d", str(self.work), PROMPT, FAKEBIN_EXIT="1", FAKEBIN_STDOUT="",
                     FAKEBIN_STDERR=x["quota"])
        self.assertEqual(p.returncode, 7, p.stderr)
        self.assertIn(f"[bridge] error: {name} out of quota", p.stderr)
        n = len(self.calls())
        fast = self.ask(name, "--no-board", "-d", str(self.work), PROMPT)
        self.assertEqual(fast.returncode, 7, fast.stderr)
        self.assertEqual(len(self.calls()), n, "a marked peer is not spawned")
        self.assertIn("[bridge] error:", fast.stderr)
        self.assertIn("⚠", self.list_row(name))
        again = self.ask(name, "--no-board", "--ignore-quota", "-d", str(self.work), PROMPT)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn(f"[bridge] {name} answered — quota mark cleared", again.stderr)

    @per_family
    def test_timeout_kills_the_peer_and_its_children(self, name, agent, x):
        before = len(self.calls())
        p = self.ask(name, "--no-board", "-t", "1", "-d", str(self.work), PROMPT, FAKEBIN_SLEEP="20", timeout=30)
        self.assertEqual(p.returncode, 143, p.stderr)
        self.assertIn("killed after 1s", p.stderr)
        call = self.calls()[before]
        self.assertFalse(alive(call["pid"]), "the peer must be gone")
        self.assertFalse(alive(call["child_pid"]), "the peer's child must die with it (process group)")

    @per_family
    def test_ask_back_exits_5_with_how_to_answer(self, name, agent, x):
        p = self.ask(name, "--no-board", "-d", str(self.work), PROMPT,
                     FAKEBIN_RESULT="One fact is missing.\n[[ASK]] which environment showed the timeout?")
        self.assertEqual(p.returncode, 5, p.stderr)
        self.assertIn("[bridge] the peer asks back: which environment showed the timeout?", p.stderr)
        self.assertIn(f"ask-agent {name} --session {agent.parse(_fakebin.fixture(agent.binary)).session}", p.stderr)

    @per_family
    def test_list_probes_the_binary_and_its_version(self, name, agent, x):
        row = self.list_row(name)
        self.assertIn(str(self.bindir / agent.binary), row)
        version = re.search(r"\d+\.\d+[\w.\-]*", _fakebin.VERSIONS[agent.binary]).group(0)   # what the probe extracts
        self.assertIn(version, row)
        self.assertIn("unknown version", self.list_row(name, FAKEBIN_VERSION="none"))


if __name__ == "__main__":
    unittest.main()
