"""tests/fakebin — stand-ins for the peer CLIs (`claude`, `cursor-agent`, `copilot`, `codex`, and `bridge-a2a` for the
`a2a` family) that tests put first on PATH.

Each stub is the CLI's name beside this file and does two things: it records how it was called, and it replays its
CLI's machine-readable output from `fixtures/<binary>.jsonl` (the event shapes the adapters' `parse()` were verified
against). `FAKEBIN_*` variables script a failure path; nothing here ever reaches a vendor.

    FAKEBIN_LOG       append one JSON line per invocation: binary, argv, cwd, pid, the AGENT_BRIDGE_* / CLAUDE_CODE_*
                      variables the peer saw, child_pid (with FAKEBIN_SLEEP) and, for cursor-agent, the global
                      ~/.cursor/cli-config.json as it stood during the run (how the bridge's deny hold is observed)
    FAKEBIN_EXIT      exit with this code after printing (default 0)
    FAKEBIN_STDERR    print this on stderr first (a quota message, a crash line)
    FAKEBIN_STDOUT    print exactly this instead of the fixture (a partial stream, garbage, nothing at all)
    FAKEBIN_RESULT    replace the fixture's canned answer (ANSWER) with this text, e.g. one that ends with [[ASK]]
    FAKEBIN_SLEEP     seconds to sleep before answering, after spawning a `sleep` child (deaf to SIGTERM) whose pid
                      is logged — a timeout must take the whole process group down, SIGKILL included
    FAKEBIN_VERSION   what `--version` prints (default: a realistic string per CLI; `none` prints nothing)

`tests/test_adapter_conformance.py` drives every adapter through these; other tests may use them the same way.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANSWER = "The retry loop in src/retry.py:12 never backs off, and src/retry.py:30 swallows the timeout."
VERSIONS = {"claude": "2.1.275 (Claude Code)", "cursor-agent": "2025.10.17-a1b2c3d", "copilot": "1.0.88",
            "codex": "codex-cli 0.63.0", "bridge-a2a": "bridge-a2a 1.0 (A2A protocol 0.3.0, envelope v1)"}
ENV_PREFIXES = ("AGENT_BRIDGE_", "CLAUDE_CODE_", "CLAUDECODE", "CLAUDE_SESSION_")


def fixture(binary: str) -> str:
    return (HERE / "fixtures" / f"{binary}.jsonl").read_text(encoding="utf-8")


def main(binary: str) -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        version = os.environ.get("FAKEBIN_VERSION", VERSIONS[binary])
        if version != "none":
            print(version)
        return 0
    sleep = float(os.environ.get("FAKEBIN_SLEEP") or 0)
    # same process group as the stub, and deaf to SIGTERM (an MCP server or a build that ignores it): the bridge's
    # timeout must SIGKILL the group, not just signal it
    child = subprocess.Popen(["sh", "-c", f'trap "" TERM; exec sleep {int(sleep) + 60}']) if sleep else None
    record = {"binary": binary, "argv": argv, "cwd": os.getcwd(), "pid": os.getpid(),
              "child_pid": child.pid if child else None,
              "env": {k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIXES)}}
    if binary == "cursor-agent":
        cfg = Path.home() / ".cursor" / "cli-config.json"
        record["cursor_config"] = cfg.read_text(encoding="utf-8") if cfg.is_file() else None
    log = os.environ.get("FAKEBIN_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    if sleep:
        time.sleep(sleep)
    if os.environ.get("FAKEBIN_STDERR"):
        print(os.environ["FAKEBIN_STDERR"], file=sys.stderr, flush=True)
    out = os.environ.get("FAKEBIN_STDOUT")
    if out is None:
        out = fixture(binary)
        result = os.environ.get("FAKEBIN_RESULT")
        if result is not None:
            out = out.replace(ANSWER, json.dumps(result)[1:-1])     # JSON-escaped, so the stream stays valid JSONL
    sys.stdout.write(out)
    sys.stdout.flush()
    return int(os.environ.get("FAKEBIN_EXIT") or 0)
