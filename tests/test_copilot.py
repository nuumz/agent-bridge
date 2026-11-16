"""Copilot JSONL fixtures use the event shapes emitted by real headless runs."""
from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import boardlib  # noqa: E402
from bridgelib import adapters, ledger, runner  # noqa: E402
from bridgelib.adapters import Copilot  # noqa: E402


def stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


def fixture() -> str:
    return stream(
        {"type": "tool.execution_start", "data": {"toolCallId": "1", "toolName": "bash"}},
        {"type": "tool.execution_complete", "data": {
            "toolCallId": "1", "success": False,
            "error": {"code": "denied", "message": "Permission to run this tool was denied: shell"}}},
        {"type": "tool.execution_start", "data": {"toolCallId": "2", "toolName": "view"}},
        {"type": "tool.execution_complete", "data": {
            "toolCallId": "2", "success": False,
            "error": {"code": "failure", "message": "Path does not exist"}}},
        {"type": "tool.execution_start", "data": {"toolCallId": "3", "toolName": "view"}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "3", "success": True}},
        {"type": "assistant.message", "data": {"phase": "commentary", "content": "Reading"}},
        {"type": "assistant.message", "data": {"phase": "final_answer", "content": "Verified finding at src/a.py:4"}},
        {"type": "result", "sessionId": "copilot-session", "usage": {
            "premiumRequests": 1, "totalApiDurationMs": 18000, "sessionDurationMs": 21000,
            "codeChanges": {"filesModified": []}}},
    )


class CopilotRunSurface(unittest.TestCase):
    """What a Copilot run carries besides the prompt: the board, the MCP servers, the usage file."""

    def opts(self, **kw):
        return argparse.Namespace(**{"mode": "ask", "deny": [], "model": "", "add_dir": [], "session": "",
                                     "cont": False, "board": False, "kind": "", **kw})

    def test_the_board_rides_on_the_run_and_is_allowed_by_name(self):
        for mode in ("ask", "plan", "write"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(adapters, "HOME", Path(d)):                 # not the real ~/.copilot
                argv = Copilot().argv(self.opts(mode=mode, board=True), "q")
                config = json.loads(argv[argv.index("--additional-mcp-config") + 1])["mcpServers"]["bridge_board"]
                self.assertEqual(config["args"], ["serve"])
                self.assertTrue(config["command"].endswith("bin/bridge-board"))
                self.assertEqual(config["env"], {"AGENT_BRIDGE_AGENT": "copilot", "AGENT_BRIDGE_HOP": "1"})
                self.assertIn("--allow-tool=bridge_board", argv)
        argv = Copilot().argv(self.opts(board=False), "q")
        self.assertNotIn("--additional-mcp-config", argv)
        self.assertNotIn("--allow-tool=bridge_board", argv)

    def test_no_mcp_server_but_the_board_loads(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(adapters, "HOME", Path(d)):
            (Path(d) / ".copilot").mkdir()
            (Path(d) / ".copilot/mcp-config.json").write_text(json.dumps({"mcpServers": {
                "chrome-bot-mcp": {}, "microsoft/markitdown": {}, "bridge_board": {}}}))
            argv = Copilot().argv(self.opts(mode="write", board=True), "q")
            self.assertIn("--disable-builtin-mcps", argv)
            self.assertIn("--disable-mcp-server=chrome-bot-mcp", argv)
            self.assertIn("--disable-mcp-server=microsoft/markitdown", argv)
            self.assertNotIn("--disable-mcp-server=bridge_board", argv)
            argv = Copilot().argv(self.opts(board=False), "q")            # --no-board: the user's own entry goes too
            self.assertIn("--disable-mcp-server=bridge_board", argv)
            (Path(d) / ".copilot/mcp-config.json").write_text("{not json")       # unreadable: the built-ins still go
            argv = Copilot().argv(self.opts(), "q")
            self.assertIn("--disable-builtin-mcps", argv)
            self.assertFalse([a for a in argv if a.startswith("--disable-mcp-server")])

    def test_usage_file_gives_the_sessions_tokens_and_the_model_that_ran(self):
        argv = Copilot().argv(self.opts(usage_file="/r/x.usage.json"), "q")
        self.assertIn("--usage-output-file=/r/x.usage.json", argv)
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "run-copilot.json"
            parsed = Copilot().parse(fixture())
            Copilot().read_usage(parsed, log)                       # no usage file: nothing changes
            self.assertNotIn("model", parsed.metrics)
            log.with_suffix(".usage.json").write_text(json.dumps({"modelMetrics": {}}))
            Copilot().read_usage(parsed, log)                       # no count: unknown, not zero
            self.assertNotIn("tokens_session_in", parsed.metrics)
            log.with_suffix(".usage.json").write_text(json.dumps({"modelMetrics": {
                "a": {"usage": {}}, "b": {"usage": {"inputTokens": True}}}}))
            Copilot().read_usage(parsed, log)                       # a model with no count, a bool: neither counts
            self.assertNotIn("tokens_session_in", parsed.metrics)
            # the shape --usage-output-file wrote on 1.0.112 (trimmed to the fields read)
            log.with_suffix(".usage.json").write_text(json.dumps({"currentModel": "gpt-6-luna", "modelMetrics": {
                "gpt-6-luna": {"usage": {"inputTokens": 58184, "outputTokens": 41}},
                "gpt-6.1-sol": {"usage": {"inputTokens": 100, "outputTokens": 9}}}}))
            Copilot().read_usage(parsed, log)
            self.assertEqual(parsed.metrics["tokens_session_in"], 58284)
            self.assertEqual(parsed.metrics["tokens_session_out"], 50)
            self.assertEqual(parsed.metrics["model"], "gpt-6-luna")
            self.assertIn("model=gpt-6-luna", parsed.extra)

    def test_read_only_preamble_steers_git_off_dash_c_and_off_graphify(self):
        text = Copilot().preamble(argparse.Namespace(dir=".", add_dir=[], mode="ask"))
        self.assertIn("never `git -C`", text)
        self.assertIn("do not load the graphify skill", text)

    def test_a_remote_family_gets_the_plain_protocol_on_a_board_thread(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "run.json"
            agent = mock.Mock(remote=True)
            agent.name, agent.env_keep = "a2a", ()
            agent.preamble.return_value = ""
            agent.prepare.return_value = []
            agent.resolve.return_value = None                          # stop right after the prompt is built
            conv = mock.Mock(topic=1, topic_name="conv:t")
            seen = []
            quiet = contextlib.redirect_stderr(io.StringIO())             # the not-on-PATH line
            with quiet, mock.patch.object(runner, "protocol", side_effect=lambda c, r: seen.append(r) or ""):
                runner._execute(agent, argparse.Namespace(ignore_quota=True, depth="standard", dir=d), "q", None, conv)
            self.assertEqual(seen, [True])
            agent.remote = False
            with contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(runner, "protocol", side_effect=lambda c, r: seen.append(r) or ""):
                runner._execute(agent, argparse.Namespace(ignore_quota=True, depth="standard", dir=d), "q", None, conv)
            self.assertEqual(seen, [True, False])
        board_thread = mock.Mock(topic=1, topic_name="conv:t")
        self.assertEqual(runner.protocol(board_thread, True), runner.PROTOCOL)
        self.assertIn("board_read", runner.protocol(board_thread))


class CopilotRuntime(unittest.TestCase):
    def opts(self, **kw):
        return argparse.Namespace(**{
            "mode": "ask", "deny": [], "model": "", "add_dir": [], "session": "", "cont": False,
            "board": False, "timeout": 5, "ignore_quota": True, "no_recover": True,
            "conv": "", "kind": "review-change", **kw})

    def test_modes_preserve_denies_and_explicit_directory_grants_on_resume(self):
        agent = Copilot()
        for mode in ("ask", "plan", "write"):
            with self.subTest(mode=mode):
                o = self.opts(mode=mode, add_dir=["/extra"], deny=["SomeMCP"])
                for argv in (agent.argv(o, "q"), agent.resume_argv(o, "session", "q")):
                    self.assertIn("--add-dir=/extra", argv)
                    self.assertIn("--deny-tool=SomeMCP", argv)
                    self.assertNotIn("--allow-all-paths", argv)
                    if mode == "write":
                        self.assertIn("--allow-all-tools", argv)
                        self.assertIn("--deny-tool=shell(git commit)", argv)
                    else:
                        self.assertIn("--deny-tool=write", argv)
                        # read-only git runs (the CLI's own read-only check), history verbs never do
                        self.assertNotIn("--deny-tool=shell", argv)
                        self.assertIn("--deny-tool=shell(git commit)", argv)
                        self.assertIn("--deny-tool=shell(git push:*)", argv)
                        # no -C deny either: the CLI refuses `git -C` even for a read, and the preamble says so
                        self.assertNotIn("--deny-tool=shell(git -C)", argv)
                        self.assertFalse([t for t in argv if t.startswith("--allow-tool")], argv)
                        self.assertNotIn("--allow-all-tools", argv)

    def test_recovers_from_tool_errors_without_calling_them_successful_tools(self):
        parsed = Copilot().parse(fixture())
        self.assertFalse(parsed.is_error)
        self.assertEqual(parsed.result, "Verified finding at src/a.py:4")
        self.assertEqual(parsed.session, "copilot-session")
        self.assertEqual(parsed.metrics, {
            "premium_requests": 1, "api_duration_ms": 18000, "files_modified": 0,
            "tool_calls": 3, "tool_failures": 2, "permission_denials": 1})
        self.assertIn("tool_failures=2", parsed.extra)
        self.assertIn("permission_denials=1", parsed.extra)

    def test_workspace_bootstrap_names_actual_root_and_existing_rules_without_granting_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / ".git").write_text("gitdir: /not-needed-for-this-test\n")
            (root / "AGENTS.md").write_text("Use the existing test runner.\n")
            cwd = root / "src" / "nested"
            cwd.mkdir(parents=True)
            (cwd / "CLAUDE.md").write_text("Keep changes local.\n")
            agent = Copilot()
            prompt = agent.preamble(self.opts(dir=str(cwd), add_dir=["../../docs"]))
            self.assertIn(f"Working directory: {cwd}", prompt)
            self.assertIn(f"Repository root: {root}", prompt)
            self.assertIn(str(root / "AGENTS.md"), prompt)
            self.assertIn(str(cwd / "CLAUDE.md"), prompt)
            self.assertNotIn(str(root / "GEMINI.md"), prompt)
            self.assertIn(str(root / "docs"), prompt)
            self.assertIn("file writes are denied. The shell runs read-only commands only", prompt)
            self.assertIn("[[ASK]]", prompt)
            argv = agent.argv(self.opts(), "q")
            self.assertNotIn("--trust-folder", argv)

    def test_bootstrap_flags_rules_the_cli_cannot_read_from_a_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("rules\n")
            cwd = root / "pkg"
            cwd.mkdir()
            (cwd / "CLAUDE.md").write_text("local\n")
            prompt = Copilot().preamble(self.opts(dir=str(cwd)))
            granted = Copilot().preamble(self.opts(dir=str(cwd), add_dir=[".."]))
        line = next(ln for ln in prompt.splitlines() if ln.startswith(f"- {root / 'AGENTS.md'}"))
        self.assertIn("outside file access", line)
        local = next(ln for ln in prompt.splitlines() if ln.startswith(f"- {cwd / 'CLAUDE.md'}"))
        self.assertNotIn("outside file access", local)
        self.assertNotIn("outside file access", granted)

    def test_non_repo_workspace_is_not_reported_as_a_git_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Copilot().preamble(self.opts(dir=tmp))
        self.assertIn("Repository root: (not a git checkout)", prompt)
        self.assertIn("no repo-level instruction file found", prompt)

    def test_partial_jsonl_is_not_a_success_even_with_an_assistant_message(self):
        for raw in (
            stream({"type": "assistant.message", "data": {"content": "Still investigating"}}),
            stream({"type": "tool.execution_start", "data": {"toolName": "view"}}),
        ):
            with self.subTest(raw=raw):
                parsed = Copilot().parse(raw)
                self.assertTrue(parsed.is_error)
                self.assertIn("incomplete", parsed.result.lower())

    def test_new_session_is_named_after_the_task_not_the_bootstrap(self):
        agent = Copilot()
        with tempfile.TemporaryDirectory() as tmp:
            prompt = agent.preamble(self.opts(dir=tmp)) + "\n  Review src/a.py for races\nmore detail"
            self.assertIn("--name=bridge · Review src/a.py for races", agent.argv(self.opts(), prompt))
            self.assertIn("--name=bridge · panel-x-critic: Review src/a.py for races",
                          agent.argv(self.opts(conv="panel-x-critic"), prompt))
            long = agent.argv(self.opts(), "x" * 200)
            self.assertEqual(len(next(a for a in long if a.startswith("--name="))), len("--name=") + 80)
        for o in (self.opts(session="abc"), self.opts(cont=True)):
            self.assertFalse(any(a.startswith("--name") for a in agent.argv(o, "q")))
        self.assertFalse(any(a.startswith("--name") for a in agent.resume_argv(self.opts(), "abc", "q")))

    def test_live_run_persists_metrics_and_warns_without_discarding_the_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / "copilot"
            cli.write_text(f"#!{sys.executable}\nprint({fixture()!r})\n")
            cli.chmod(0o755)
            agent = Copilot()
            out = io.StringIO()
            with mock.patch.object(agent, "resolve", return_value=str(cli)), \
                    mock.patch.object(runner, "RUNS", root / "runs"), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"), \
                    mock.patch.object(boardlib, "BOARD_DIR", root / "board"), \
                    mock.patch.object(boardlib, "DB_PATH", root / "board" / "board.db"), \
                    contextlib.redirect_stderr(out):
                rc, parsed, log = runner.execute(agent, self.opts(dir=tmp), "Review src/a.py", None, None)
                rows = ledger.rows()
            self.assertEqual(rc, 0)
            self.assertEqual(parsed.result, "Verified finding at src/a.py:4")
            self.assertEqual(rows[0]["tool_failures"], 2)
            self.assertEqual(rows[0]["permission_denials"], 1)
            self.assertEqual(rows[0]["premium_requests"], 1)
            self.assertIn("[bridge] warning:", out.getvalue())
            self.assertIn(str(log), out.getvalue())
            saved = log.with_name(log.stem + ".prompt.txt").read_text()
            self.assertIn(f"Working directory: {root.resolve()}", saved)
            self.assertIn("Review src/a.py", saved)

    def test_failed_process_keeps_partial_tool_metrics_in_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / "copilot"
            raw = "\n".join(fixture().splitlines()[:2])
            cli.write_text(f"#!{sys.executable}\nprint({raw!r})\nraise SystemExit(1)\n")
            cli.chmod(0o755)
            agent = Copilot()
            with mock.patch.object(agent, "resolve", return_value=str(cli)), \
                    mock.patch.object(runner, "RUNS", root / "runs"), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"), \
                    mock.patch.object(boardlib, "BOARD_DIR", root / "board"), \
                    mock.patch.object(boardlib, "DB_PATH", root / "board" / "board.db"), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc, parsed, log = runner.execute(agent, self.opts(dir=tmp), "Review src/a.py", None, None)
                row = ledger.rows()[0]
            self.assertEqual(rc, 1)
            self.assertTrue(parsed.is_error)
            self.assertEqual(row["tool_calls"], 1)
            self.assertEqual(row["permission_denials"], 1)
            self.assertNotIn("premium_requests", row)

    def test_a_run_that_raises_after_writing_its_prompt_stays_one_row_after_backfill(self):
        # the ledger row names the log _execute chose: before 2026-11-16 it had none, so a later backfill rebuilt the
        # saved prompt into a second row of the same run
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = Copilot()
            with mock.patch.object(agent, "resolve", return_value=sys.executable), \
                    mock.patch.object(runner, "run", side_effect=OSError("launch failed")), \
                    mock.patch.object(runner, "RUNS", root / "runs"), \
                    mock.patch.object(ledger, "STATE", root), \
                    mock.patch.object(ledger, "RUNS", root / "runs"), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"), \
                    mock.patch.object(boardlib, "BOARD_DIR", root / "board"), \
                    mock.patch.object(boardlib, "DB_PATH", root / "board" / "board.db"), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(OSError):
                    runner.execute(agent, self.opts(dir=tmp), "Review src/a.py", None, None)
                live = ledger.rows()
                ledger.backfill()
                after = ledger.rows()
            self.assertEqual(len(live), 1)
            self.assertEqual([(r["type"], r["rc"], r["log"]) for r in after], [("run", 2, live[0]["log"])])
            self.assertTrue(Path(live[0]["log"].replace(".json", ".prompt.txt")).is_file())


class CopilotLearning(unittest.TestCase):
    def test_usage_checkpoints_give_the_sessions_ai_credits(self):
        raw = stream({"type": "session.usage_checkpoint", "data": {"totalNanoAiu": 4000000000}},
                     {"type": "assistant.message", "data": {"content": "Done."}},
                     {"type": "session.usage_checkpoint", "data": {"totalNanoAiu": 9935744000}},
                     {"type": "result", "sessionId": "s", "exitCode": 0, "usage": {"premiumRequests": 1}})
        self.assertEqual(Copilot().parse(raw).metrics["ai_credits_session"], 9.935744)   # the running total, last
        self.assertNotIn("ai_credits_session", Copilot().parse(fixture()).metrics)        # no checkpoint, no figure

    def test_backfill_reads_the_ask_back_from_the_saved_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            (runs / "20261022-120000-1-copilot.prompt.txt").write_text("Review")
            (runs / "20261022-120000-1-copilot.json").write_text(stream(
                {"type": "assistant.message",
                 "data": {"content": "One fact is missing.\n[[ASK]] Allow access to /x/a.diff"}},
                {"type": "result", "sessionId": "s", "exitCode": 0, "usage": {}}))
            with mock.patch.object(ledger, "STATE", root), \
                    mock.patch.object(ledger, "RUNS", runs), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"):
                ledger.backfill()
                row = ledger.rows()[0]
            self.assertEqual((row["asked_back"], row["ask"]), (True, "Allow access to /x/a.diff"))

    def test_backfill_keeps_rows_whose_trace_is_gone_and_what_only_the_live_row_knew(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            (runs / "20261022-120000-1-copilot.prompt.txt").write_text("Review")
            log = runs / "20261022-120000-1-copilot.json"
            log.write_text(fixture())
            gone = str(runs / "20261001-120000-1-claude.json")          # `bridge-panel gc` took this log
            with mock.patch.object(ledger, "STATE", root), \
                    mock.patch.object(ledger, "RUNS", runs), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"):
                ledger.record({"ts": 1, "type": "run", "peer": "claude", "rc": 0, "log": gone,
                               "citations_ok": 3, "citations_total": 4})
                ledger.record({"ts": 2, "type": "panel", "name": "gone-panel", "rc": 0})
                ledger.record({"ts": 3, "type": "run", "peer": "copilot", "rc": 0, "log": str(log),
                               "citations_rev": 2, "dir": "/w"})                 # no log holds these two
                self.assertEqual(ledger.backfill(), (2, 1))
                rows = {r.get("log") or r.get("name"): r for r in ledger.rows()}
            self.assertEqual(rows[gone]["citations_total"], 4)
            self.assertIn("gone-panel", rows)
            rebuilt = rows[str(log)]
            self.assertTrue(rebuilt["backfilled"])
            self.assertEqual((rebuilt["citations_rev"], rebuilt["dir"]), (2, "/w"))

    def test_backfill_preserves_copilot_session_metrics_and_partial_stream_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            for stamp, raw in (
                ("20261022-120000-1", fixture()),
                ("20261022-120001-2", stream(
                    {"type": "assistant.message", "data": {"content": "Investigating"}})),
            ):
                bootstrap = Copilot().preamble(argparse.Namespace(dir=tmp, mode="write", add_dir=[]))
                (runs / f"{stamp}-copilot.prompt.txt").write_text(bootstrap + "Review")
                (runs / f"{stamp}-copilot.json").write_text(raw)
            with mock.patch.object(ledger, "STATE", root), \
                    mock.patch.object(ledger, "RUNS", runs), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"):
                self.assertEqual(ledger.backfill(), (2, 0))
                complete, partial = ledger.rows()
            self.assertEqual(complete["session"], "copilot-session")
            self.assertEqual(complete["kind"], "review-change")
            self.assertEqual(complete["tool_failures"], 2)
            self.assertEqual(complete["api_duration_ms"], 18000)
            self.assertEqual(complete["rc"], 0)
            self.assertEqual(partial["rc"], 1)

    def test_backfill_keeps_the_live_exit_status_over_a_complete_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            (runs / "20261022-120000-1-copilot.prompt.txt").write_text("Review")
            log = runs / "20261022-120000-1-copilot.json"
            log.write_text(fixture())
            with mock.patch.object(ledger, "STATE", root), \
                    mock.patch.object(ledger, "RUNS", runs), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"):
                ledger.record({"ts": 1, "type": "run", "peer": "copilot", "rc": 1, "log": str(log)})
                ledger.backfill()
                self.assertEqual(ledger.rows()[0]["rc"], 1)
                ledger.backfill()                                # a second rebuild must not forget it
                self.assertEqual(ledger.rows()[0]["rc"], 1)

    def test_backfill_reads_a_recovered_run_once_from_its_recovery_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            (runs / "20261022-120000-1-claude.prompt.txt").write_text("Review")
            (runs / "20261022-120000-1-claude.json").write_text(json.dumps(
                {"type": "result", "result": "I will read the files now.", "session_id": "s", "total_cost_usd": 0.1}))
            (runs / "20261022-120000-1-claude.recover.prompt.txt").write_text("Answer now")
            recovered = runs / "20261022-120000-1-claude.recover.json"
            recovered.write_text(json.dumps(
                {"type": "result", "result": "The bug is at a.py:3.", "session_id": "s", "total_cost_usd": 0.3}))
            with mock.patch.object(ledger, "STATE", root), \
                    mock.patch.object(ledger, "RUNS", runs), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"):
                # the live row names the log the answer came from (runner: `parsed, log = rp, rlog`)
                ledger.record({"ts": 9, "type": "run", "peer": "claude", "rc": 0, "log": str(recovered),
                               "citations_rev": 2})
                self.assertEqual(ledger.backfill(), (1, 0))                  # one run, one row
                row, = ledger.rows()
        self.assertEqual((row["log"], row["cost_session"], row["citations_rev"]), (str(recovered), 0.3, 2))

    def test_backfill_keeps_the_ask_back_and_session_a_codex_log_has_no_result_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            (runs / "20261022-120000-1-codex.prompt.txt").write_text("Review")
            log = runs / "20261022-120000-1-codex.json"
            log.write_text("\n".join(json.dumps(e) for e in (
                {"type": "thread.started", "thread_id": "th-1"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "[[ASK]] Allow access to /x"}},
                {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 7}})) + "\n")
            with mock.patch.object(ledger, "STATE", root), \
                    mock.patch.object(ledger, "RUNS", runs), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"):
                ledger.record({"ts": 1, "type": "run", "peer": "codex", "rc": 5, "log": str(log), "session": "th-1",
                               "asked_back": True, "ask": "Allow access to /x", "duration_s": 120})
                ledger.backfill()
                row, = ledger.rows()
        self.assertEqual((row["asked_back"], row["ask"], row["session"], row["rc"], row["duration_s"]),
                         (True, "Allow access to /x", "th-1", 5, 120))     # a Codex log holds no duration
        self.assertEqual(row["tokens_out"], 7)                               # what the log holds is still read

    def test_backfill_prices_a_turn_against_the_carried_turns_of_its_session(self):
        start = int(time.mktime(time.strptime("20261022120000", "%Y%m%d%H%M%S")))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            (runs / "20261022-120000-1-claude.prompt.txt").write_text("Review")
            log = runs / "20261022-120000-1-claude.json"
            log.write_text(json.dumps({"type": "result", "result": "ok", "session_id": "s", "total_cost_usd": 0.55,
                                       "duration_ms": 30000}))
            with mock.patch.object(ledger, "STATE", root), \
                    mock.patch.object(ledger, "RUNS", runs), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"):
                # the session's first turn: `bridge-panel gc` took its log, so only its carried row holds the total
                ledger.record({"ts": start - 60, "type": "run", "peer": "claude", "rc": 0, "session": "s",
                               "cost_session": 0.40, "cost": 0.40, "log": str(runs / "20261022-115800-1-claude.json")})
                ledger.backfill()
                rows = {r["log"]: r for r in ledger.rows()}
        self.assertEqual(rows[str(log)]["cost"], 0.15)

    def test_backfill_keeps_a_pruned_panel_and_what_only_the_live_panel_row_knew(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            pruned, whole = root / "panel" / "pruned", root / "panel" / "whole"
            pruned.mkdir(parents=True)
            (pruned / "agreement.md").write_text("card\n")                      # all gc left of it
            whole.mkdir()
            (whole / "status.json").write_text(json.dumps({"answered": {"root-cause": "claude"}}))
            (whole / "report.md").write_text("# Panel whole — 2026-10-22 12:00\n")
            with mock.patch.object(ledger, "STATE", root), \
                    mock.patch.object(ledger, "RUNS", runs), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"):
                ledger.record({"ts": 1, "type": "panel", "name": "pruned", "rc": 0,
                               "citations": {"root-cause": [3, 4]}})
                ledger.record({"ts": 2, "type": "panel", "name": "whole", "rc": 1, "citations_rev": 2})
                self.assertEqual(ledger.backfill(), (0, 2))
                panels = {r["name"]: r for r in ledger.rows()}
        self.assertEqual((panels["pruned"]["rc"], panels["pruned"]["citations"]), (0, {"root-cause": [3, 4]}))
        self.assertEqual((panels["whole"]["rc"], panels["whole"]["citations_rev"]), (1, 2))   # the exit status is live
        self.assertTrue(panels["whole"]["backfilled"])

    def test_backfill_carries_every_row_of_a_run_or_panel_with_nothing_on_disk(self):
        # a rebuild replaces every row of what it rebuilds, so two rows of a run or panel with nothing left on disk are
        # two facts: deduplicating them lost an attempt whichever row it kept
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            with mock.patch.object(ledger, "STATE", root), \
                    mock.patch.object(ledger, "RUNS", runs), \
                    mock.patch.object(ledger, "LEDGER", root / "ledger.jsonl"):
                # a panel run twice under one name: an earlier backfill rebuilt the first attempt, the second is live
                ledger.record({"ts": 1, "type": "panel", "name": "rerun", "rc": 4, "backfilled": True})
                ledger.record({"ts": 2, "type": "panel", "name": "rerun", "rc": 0})
                # a live session: a row per turn, each naming the session's one log
                turns = str(root / "live" / "claude" / "peer.jsonl")
                ledger.record({"ts": 3, "type": "run", "peer": "claude", "mode": "live", "rc": 0, "log": turns})
                ledger.record({"ts": 4, "type": "run", "peer": "claude", "mode": "live", "rc": 1, "log": turns})
                ledger.backfill()
                self.assertEqual([(r["type"], r["ts"], r["rc"]) for r in ledger.rows()],
                                 [("panel", 1, 4), ("panel", 2, 0), ("run", 3, 0), ("run", 4, 1)])

    def test_plan_card_names_alternative_peers(self):
        path = Path(__file__).resolve().parents[1] / "bin" / "bridge-plan"
        loader = importlib.machinery.SourceFileLoader("copilot_bridge_plan_alt", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        plan = importlib.util.module_from_spec(spec)
        loader.exec_module(plan)
        book = {"implement": {"name": "implement", "summary": "s", "form": "direct", "level": "routine", "peers": "claude",
                              "alt_peers": "cursor,copilot", "mode": "write"}}
        out = io.StringIO()
        with mock.patch.object(ledger, "load_playbooks", return_value=book), contextlib.redirect_stdout(out):
            o = argparse.Namespace(request="add x", kind="implement", name="t", peers=None, dir="/r", force=False)
            self.assertEqual(plan.plan(o), 0)
        self.assertRegex(out.getvalue(), r"alt peers\s+cursor,copilot")

    def test_learn_separates_task_and_mode_and_does_not_treat_missing_usage_as_zero(self):
        path = Path(__file__).resolve().parents[1] / "bin" / "bridge-plan"
        loader = importlib.machinery.SourceFileLoader("copilot_bridge_plan", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        plan = importlib.util.module_from_spec(spec)
        loader.exec_module(plan)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ledger, "LEDGER", Path(tmp) / "ledger.jsonl"), \
                mock.patch.dict("os.environ", {"BRIDGE_DOC_ROOTS": ""}):
            for kind, mode, extra in (
                ("review-change", "ask", {"api_duration_ms": 18000, "premium_requests": 1,
                                         "tool_failures": 2, "permission_denials": 1}),
                ("implement", "write", {"api_duration_ms": 6000, "premium_requests": 2,
                                       "tool_failures": 0, "permission_denials": 0}),
                ("review-change", None, {}),
            ):
                ledger.record({"ts": int(time.time()), "type": "run", "peer": "copilot",
                               "kind": kind, "mode": mode, "rc": 0, **extra})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(plan.learn(argparse.Namespace(backfill=False, days=30)), 0)
            text = out.getvalue()
        self.assertIn("Copilot operations", text)
        self.assertRegex(text, r"review-change\s+ask\s+1\s+1\s+18\.0\s+1\s+2\s+1")
        self.assertRegex(text, r"implement\s+write\s+1\s+1\s+6\.0\s+2\s+0\s+0")
        self.assertRegex(text, r"review-change\s+unknown\s+1\s+1\s+-\s+-\s+-\s+-")
        self.assertIn("not quality", text)


if __name__ == "__main__":
    unittest.main()


class SharedExtraRoots(unittest.TestCase):
    """Panel/work/fix --add-dir reaches every peer run they spawn, so evidence cited outside -d stays readable."""

    def test_orchestrator_roots_reach_the_peer_command_line(self):
        import os
        import subprocess
        from bridgelib import core
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as spec, \
                tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(core.ADD_DIRS_VAR, None)
            core.share_add_dirs([spec, spec])
            core.share_add_dirs([cwd])
            self.assertEqual(core.add_dirs_from_env(),
                             [str(Path(spec).resolve()), str(Path(cwd).resolve())])
            env = {k: v for k, v in os.environ.items() if k != core.HOP_VAR}
            env["HOME"] = home                      # the real ask-agent opens the board under HOME even on --dry-run
            out = subprocess.run([str(Path(__file__).resolve().parents[1] / "bin" / "ask-agent"), "copilot",
                                  "--dry-run", "-d", cwd, "q"], capture_output=True, text=True, env=env, cwd=cwd)
            self.assertIn(f"--add-dir={Path(spec).resolve()}", json.loads(out.stdout)["argv"])

    def test_fix_flow_keeps_absolute_roots_for_a_resume_from_another_directory(self):
        import os
        import subprocess
        from bridgelib import core
        loader = importlib.machinery.SourceFileLoader("bridge_fix_roots", str(Path(__file__).resolve().parents[1] / "bin" / "bridge-fix"))
        fix = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(fix)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(core.ADD_DIRS_VAR, None)
            repo, spec = Path(tmp, "repo"), Path(tmp, "spec")
            spec.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                core.share_add_dirs(["spec"])
                o = argparse.Namespace(name="roots", problem="p", dir=str(repo), peers="claude", roles=None,
                                       files=None, verify=None, add_dir=["spec"])
                with mock.patch.object(fix, "WORK", Path(tmp, "work")), mock.patch.object(fix, "analyze", return_value=1), \
                        mock.patch("sys.stderr"):
                    fix.run(o)
            finally:
                os.chdir(cwd)
            saved = json.loads(Path(tmp, "work", "roots", "flow.json").read_text())
            self.assertEqual(saved["add_dirs"], [str(spec.resolve())])

    def test_missing_root_is_refused_up_front(self):
        import os
        from bridgelib import core
        with mock.patch.dict(os.environ, {}, clear=False), self.assertRaises(SystemExit):
            core.share_add_dirs(["/nonexistent/agent-bridge-root"])


class CitedRoots(unittest.TestCase):
    """A read-only run may read the git repo of any path its prompt cites under HOME — without the caller remembering --add-dir."""

    def setUp(self):
        import subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        for repo in ("code", "spec"):
            subprocess.run(["git", "init", "-q", str(self.home / repo)], check=True)
        (self.home / "spec" / "docs").mkdir()
        (self.home / "spec" / "docs" / "design.md").write_text("x")
        (self.home / "loose").mkdir()
        (self.home / "loose" / "notes.md").write_text("x")
        self.addCleanup(self.tmp.cleanup)

    def test_only_git_roots_outside_the_workspace_and_under_home_are_granted(self):
        from bridgelib import core
        text = (f"see {self.home}/spec/docs/design.md:12, also `{self.home}/spec/docs/missing.md` and "
                f"{self.home}/code/src/A.java:3 plus {self.home}/loose/notes.md and /etc/hosts.")
        with mock.patch.object(core, "HOME", self.home):
            self.assertEqual(core.cited_roots(text, str(self.home / "code"), []), [str(self.home / "spec")])
            self.assertEqual(core.cited_roots(text, str(self.home / "code"), [str(self.home / "spec")]), [])

    def test_quoted_path_with_spaces_is_one_path(self):
        import subprocess
        from bridgelib import core
        subprocess.run(["git", "init", "-q", str(self.home / "My Specs")], check=True)
        (self.home / "My Specs" / "a.md").write_text("x")
        with mock.patch.object(core, "HOME", self.home):
            for quote in ('"', "'", "`"):
                self.assertEqual(core.cited_roots(f"read {quote}{self.home}/My Specs/a.md{quote}:3", str(self.home / "code"), []),
                                 [str(self.home / "My Specs")])

    def test_bridge_state_grants_one_run_folder_not_the_ledger(self):
        from bridgelib import core
        state = self.home / ".agent-bridge" / "state"
        (state / "panel" / "p1").mkdir(parents=True)
        (state / "panel" / "p1" / "agreement.md").write_text("x")
        (state / "ledger.jsonl").write_text("{}")
        text = f"{state}/panel/p1/agreement.md and {state}/ledger.jsonl and {self.home}/.agent-bridge/SHARED-RULES.md"
        with mock.patch.object(core, "HOME", self.home), mock.patch.object(core, "STATE", state):
            self.assertEqual(core.cited_roots(text, str(self.home / "code"), []), [str(state / "panel" / "p1")])

    def test_ask_agent_grants_cited_roots_in_read_modes_only(self):
        import os
        import subprocess
        from bridgelib import core
        env = {k: v for k, v in os.environ.items() if k not in (core.HOP_VAR, core.ADD_DIRS_VAR)}
        env["HOME"] = str(self.home)
        prompt = f"check {self.home}/spec/docs/design.md:4 against the code"
        argv = {}
        for mode in ("ask", "write"):
            out = subprocess.run([str(Path(__file__).resolve().parents[1] / "bin" / "ask-agent"), "copilot", "-m", mode,
                                  "--dry-run", "-d", str(self.home / "code"), prompt],
                                 capture_output=True, text=True, env=env)
            argv[mode] = json.loads(out.stdout)["argv"]
        self.assertIn(f"--add-dir={self.home / 'spec'}", argv["ask"])
        self.assertNotIn(f"--add-dir={self.home / 'spec'}", argv["write"])
