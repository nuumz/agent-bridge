"""Codex `exec --json` parsing on the event shapes of codex-rs exec_events.rs: the answer is the agent_message text,
the thread id is what resumes, a failed turn or an error event is the peer's own message first (so the bridge error
line quotes it, and a usage-limit message there marks the quota), an incomplete stream is not a success, and edits
show up as EDITED=. The spawn / flag / failure-path checks live in tests/test_adapter_conformance.py."""
from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from bridgelib import core  # noqa: E402
from bridgelib.adapters import ADAPTERS, Codex  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fakebin" / "fixtures" / "codex.jsonl"


def stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


def opts(**kw) -> argparse.Namespace:
    base = dict(mode="ask", board=False, deny=[], model="", add_dir=[], worktree=None, session="", cont=False,
                dir="/work")
    return argparse.Namespace(**{**base, **kw})


class CodexParse(unittest.TestCase):
    def test_fixture_answer_thread_usage_and_tool_metrics(self):
        p = Codex().parse(FIXTURE.read_text())
        self.assertFalse(p.is_error)
        self.assertTrue(p.result.startswith("The retry loop in src/retry.py:12"))
        self.assertNotIn("no delay between attempts", p.result, "reasoning is not the answer")
        self.assertEqual(p.session, "0199a213-81c0-7800-8aa1-bbab2a035a53")
        self.assertEqual((p.tok_in, p.tok_out), (24763, 122))
        self.assertEqual(p.metrics, {"tool_calls": 1, "tool_failures": 0, "permission_denials": 0, "files_modified": 0})
        self.assertIn("cached=24448", p.extra)

    def test_failed_turn_puts_the_peers_message_first_and_a_usage_limit_is_a_quota(self):
        raw = stream({"type": "thread.started", "thread_id": "t1"}, {"type": "turn.started"},
                     {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Partial."}},
                     {"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again in 3 hours."}})
        p = Codex().parse(raw)
        self.assertTrue(p.is_error)
        self.assertTrue(p.result.startswith("You've hit your usage limit"))
        self.assertIn("Partial.", p.result)
        self.assertTrue(core.quota_hit(p.result))
        plain = Codex().parse(stream({"type": "error", "message": "stream disconnected"},
                                     {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 0}}))
        self.assertTrue(plain.is_error)
        self.assertEqual(plain.result, "stream disconnected")

    def test_incomplete_stream_is_not_a_success(self):
        p = Codex().parse(stream({"type": "thread.started", "thread_id": "t1"},
                                 {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Hi"}}))
        self.assertTrue(p.is_error)
        self.assertIn("incomplete", p.result.lower())
        self.assertEqual(p.session, "t1")
        self.assertEqual(Codex().parse("").is_error, True)
        self.assertEqual(Codex().parse("plain text from an older CLI").result, "plain text from an older CLI")

    def test_edits_and_failed_commands_reach_the_metrics(self):
        raw = stream({"type": "thread.started", "thread_id": "t2"},
                     {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "pytest",
                                                         "aggregated_output": "1 failed", "exit_code": 1, "status": "completed"}},
                     {"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "command": "rm -rf /",
                                                         "aggregated_output": "", "exit_code": None, "status": "declined"}},
                     {"type": "item.completed", "item": {"id": "i3", "type": "file_change", "status": "completed",
                                                         "changes": [{"path": "src/retry.py", "kind": "update"},
                                                                     {"path": "tests/test_retry.py", "kind": "add"}]}},
                     {"type": "item.completed", "item": {"id": "i4", "type": "agent_message", "text": "Done."}},
                     {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
        p = Codex().parse(raw)
        self.assertFalse(p.is_error)
        self.assertEqual(p.metrics, {"tool_calls": 2, "tool_failures": 2, "permission_denials": 1, "files_modified": 2})
        self.assertIn("EDITED=src/retry.py,tests/test_retry.py", p.extra)


class CodexArgv(unittest.TestCase):
    def test_sandbox_per_mode_resume_forms_and_the_board_rides_on_config_overrides(self):
        codex = ADAPTERS["codex"]
        self.assertFalse(codex.supports_deny)
        self.assertFalse(codex.supports_worktree)
        ask = codex.argv(opts(), "q")
        self.assertEqual(ask[:2], ["exec", "--json"])
        self.assertEqual(ask[ask.index("--sandbox") + 1], "read-only")
        self.assertEqual(ask[-2:], ["--", "q"])
        self.assertNotIn("resume", ask)
        write = codex.argv(opts(mode="write"), "q")
        self.assertEqual(write[write.index("--sandbox") + 1], "workspace-write")
        resumed = codex.argv(opts(session="abc"), "q")
        self.assertEqual(resumed[-4:], ["resume", "abc", "--", "q"])
        latest = codex.argv(opts(cont=True), "q")
        self.assertEqual(latest[-4:], ["resume", "--last", "--", "q"])
        self.assertEqual(codex.resume_argv(opts(), "abc", "q")[-4:], ["resume", "abc", "--", "q"])
        for a in (ask, resumed):                     # flags that are not global in clap stay before `resume`
            self.assertLess(a.index("--sandbox"), a.index("--") if "resume" not in a else a.index("resume"))
        board = codex.argv(opts(board=True), "q")
        overrides = [board[i + 1] for i, v in enumerate(board) if v == "-c"]
        self.assertEqual(len(overrides), 3)
        self.assertTrue(any(o.startswith("mcp_servers.bridge_board.command=\"") and o.endswith("bridge-board\"")
                            for o in overrides), overrides)
        self.assertIn('mcp_servers.bridge_board.args=["serve"]', overrides)
        self.assertIn('mcp_servers.bridge_board.env={AGENT_BRIDGE_AGENT="codex"}', overrides)
        self.assertNotIn("-c", ask)

    def test_preambles_name_the_sandbox_and_keep_git_history_the_users(self):
        codex = ADAPTERS["codex"]
        ro = codex.preamble(opts())
        self.assertIn("read-only sandbox", ro)
        self.assertIn("file:line", ro)
        rw = codex.preamble(opts(mode="write"))
        self.assertIn("never git commit/push/tag", rw)
        self.assertIn("caller's checkout", rw)
        self.assertNotIn("caller's checkout", codex.preamble(opts(mode="write", discard_ok=True)))


if __name__ == "__main__":
    unittest.main()
