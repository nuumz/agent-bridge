"""bridge-plan target: which live session gets a handoff — by topic, not by name (SHARED-RULES.md §3).

Fixtures follow the real file shapes of Claude Code 2.1.322 (checked 2026-11-17): the registry
~/.claude/sessions/<pid>.json,
transcript records (`type: user` with `origin.kind` human / peer / task-notification, `type: assistant` text blocks,
`type: queue-operation`), and a workspace claim pipeline/<code>.md with a flat frontmatter `session:`.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
from bridgelib import sessions  # noqa: E402

WS = "/Users/u/appl/Workspace/p3.2-defect"


def human(text):
    return {"type": "user", "promptSource": "typed", "origin": {"kind": "human"}, "isSidechain": False,
            "message": {"role": "user", "content": text}}


def peer(body, sender="s1-design-spec-7d"):
    return {"type": "user", "isMeta": True, "promptSource": "system", "isSidechain": False,
            "origin": {"kind": "peer", "from": "uds:/tmp/cc-socks/1.sock", "name": sender, "body": body},
            "message": {"role": "user",
                        "content": f'Another Claude session sent a message:\n<cross-session-message>{body}'}}


def reply(text):  # one content block per assistant record, as Claude Code writes them
    return {"type": "assistant", "isSidechain": False, "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}]}}


def tool_turn():
    return [{"type": "assistant", "isSidechain": False, "message": {"role": "assistant",
             "content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "ok"}]}}]


def notification(text):
    return {"type": "user", "origin": {"kind": "task-notification"}, "promptSource": "system", "isSidechain": False,
            "message": {"role": "user", "content": f"<task-notification>{text}</task-notification>"}}


class TargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.patches = [(sessions, "SESSIONS_DIR", sessions.SESSIONS_DIR),
                        (sessions, "PROJECTS_DIR", sessions.PROJECTS_DIR)]
        sessions.SESSIONS_DIR = base / "sessions"
        sessions.PROJECTS_DIR = base / "projects"
        sessions.SESSIONS_DIR.mkdir()
        self.ws = base / "ws"
        (self.ws / "pipeline").mkdir(parents=True)
        self.n = 0

    def tearDown(self):
        for mod, name, value in self.patches:
            setattr(mod, name, value)
        self.tmp.cleanup()

    def session(self, name, records, status="idle", cwd=WS, kind="interactive", pid=None, entrypoint="cli"):
        self.n += 1
        sid = f"00000000-0000-0000-0000-{self.n:012d}"
        rec = {"pid": pid or os.getpid(), "sessionId": sid, "cwd": cwd, "version": "2.1.322", "peerProtocol": 1,
               "kind": kind, "entrypoint": entrypoint, "messagingSocketPath": f"/tmp/cc-socks/{self.n}.sock",
               "name": name,
               "nameSource": "derived", "status": status, "updatedAt": 1794893000000 + self.n}
        (sessions.SESSIONS_DIR / f"{self.n}.json").write_text(json.dumps(rec), encoding="utf-8")
        folder = sessions.PROJECTS_DIR / "".join(c if c.isalnum() else "-" for c in cwd)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def claim(self, code, owner, qcweb="", jira="", related=""):
        (self.ws / "pipeline" / f"{code}.md").write_text(
            f"---\ncode: {code}\nqcweb: {qcweb}\njira: {jira}\nrelated: [{related}]\ngroup: [{code}]\n"
            f"session: '{owner}'\n"
            "step: 3\n---\n# body\n", encoding="utf-8")

    def run_target(self, request, keys=(), dir_=None):
        return sessions.target(request, list(keys), str(dir_ or self.ws), self_name="caller")

    # SAME
    def test_the_claim_owner_wins_over_a_session_that_was_sent_the_topic_by_mistake(self):
        self.claim("QC-740", "p3-2-defect-2a", qcweb="740", jira="KESC-10249")
        self.session("p3-2-defect-66", [peer("QC-740: take the defect"), reply("Needs you: stood down")])
        self.session("p3-2-defect-2a", [reply("Needs you: R3 waits on QA")], status="busy")
        res = self.run_target("continue the spinner defect", keys=["740"])
        self.assertEqual(res["recommend"]["action"], "send")
        self.assertEqual(res["recommend"]["name"], "p3-2-defect-2a")
        self.assertEqual(res["recommend"]["socket"], "/tmp/cc-socks/2.sock")

    def test_a_key_in_a_received_prompt_or_slash_command_is_same_even_while_busy(self):
        self.session("worker-a", [human("<command-name>/defect-fix</command-name>\n<command-args>740</command-args>")],
                     status="busy")
        self.session("worker-b", [human("unrelated"), reply("Done (evidence: tests)")])
        res = self.run_target("QC-740 again", keys=["740"])  # a bare number matches only when the caller names it
        self.assertEqual((res["recommend"]["action"], res["recommend"]["name"]), ("send", "worker-a"))
        self.assertEqual(self.run_target("QC-740 again")["recommend"]["action"], "spawn")

    def test_two_sessions_on_the_topic_without_a_claim_ask_the_user(self):
        self.session("a", [peer("KESC-10249 trace")])
        self.session("b", [human("look at KESC-10249")])
        res = self.run_target("KESC-10249")
        self.assertEqual(res["recommend"]["action"], "ask")

    def test_a_claim_naming_a_session_that_is_not_live_asks_the_user(self):
        self.claim("QC-592", "p3-2-defect-43", qcweb="592")
        self.session("idle-done", [human("x"), reply("Done (evidence: ok)")])
        res = self.run_target("QC-592 reopened")
        self.assertEqual(res["recommend"]["action"], "ask")
        self.assertEqual(res["orphan_claims"][0]["session"], "p3-2-defect-43")

    def test_a_notification_or_tool_result_naming_the_key_is_not_a_topic(self):
        tool_result = {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "QC-740.md"}]}}
        self.session("w", [notification("QC-740 run finished"), tool_result, human("x"), reply("Done (evidence: ok)")])
        res = self.run_target("QC-740")
        self.assertEqual(res["sessions"][0]["class"], "FREE")

    # RELATED
    def test_an_open_task_sharing_two_identifiers_is_related(self):
        self.session("w", [human("fix SignatureV2 loader in SignatureSignNew.tsx"), reply("Needs you: pick 1 or 2")],
                     status="busy")
        res = self.run_target("check SignatureSignNew.tsx and SignatureV2 timeouts")
        self.assertEqual((res["sessions"][0]["class"], res["recommend"]["name"]), ("RELATED", "w"))

    def test_one_shared_identifier_is_not_enough(self):
        self.session("w", [human("fix SignatureV2 loader"), reply("Needs you: pick 1 or 2")])
        res = self.run_target("SignatureV2 docs typo")
        self.assertEqual(res["sessions"][0]["class"], "OCCUPIED")
        self.assertEqual(res["recommend"]["action"], "spawn")

    def test_identifiers_of_a_task_closed_with_done_do_not_count(self):
        self.session("w", [human("fix SignatureV2 in SignatureSignNew.tsx"), reply("Done (evidence: abc123)")])
        res = self.run_target("SignatureV2 SignatureSignNew.tsx follow-up")
        self.assertEqual(res["sessions"][0]["class"], "FREE")

    # FREE / OCCUPIED
    def test_unrelated_work_goes_to_an_idle_session_whose_task_is_done_in_the_same_dir(self):
        self.session("open", [human("x"), reply("Needs you: decide the table")])
        self.session("done", [human("y"), reply("**Done** (evidence: ls-remote)")])
        res = self.run_target("write a changelog", dir_=WS)
        self.assertEqual((res["recommend"]["action"], res["recommend"]["name"]), ("send", "done"))
        self.assertEqual({r["name"]: r["class"] for r in res["sessions"]}, {"open": "OCCUPIED", "done": "FREE"})

    def test_a_free_session_in_another_dir_is_not_given_unrelated_work(self):
        self.session("elsewhere", [human("y"), reply("Done (evidence: ok)")], cwd="/Users/u/other")
        self.assertEqual(self.run_target("write a changelog", dir_=WS)["recommend"]["action"], "spawn")

    def test_busy_queued_or_unclosed_sessions_are_occupied(self):
        queued = {"type": "queue-operation", "operation": "enqueue", "content": "<cross-session-message>..."}
        self.session("busy", [human("y"), reply("Done (evidence: ok)")], status="busy")
        self.session("queued", [human("y"), reply("Done (evidence: ok)"), queued])
        self.session("cut", [human("y"), reply("API Error: Connection lost mid-response.")])
        res = self.run_target("write a changelog", dir_=WS)
        self.assertEqual({r["class"] for r in res["sessions"]}, {"OCCUPIED"})
        self.assertEqual(res["recommend"]["action"], "spawn")

    def test_headless_bridged_runs_dead_pids_and_the_caller_are_not_listed(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.session("bridge · conv: review", [], entrypoint="sdk-cli")
        self.session("p3-2-defect-9f", [human("QC-740")], entrypoint="sdk-cli")  # a resumed run: cwd-derived name
        self.session("gone", [], pid=dead.pid)
        self.session("caller", [])
        self.session("fresh", [])
        self.assertEqual([r["name"] for r in self.run_target("anything")["sessions"]], ["fresh"])

    # review round 1 (2026-11-17)
    def test_a_claim_owner_that_is_not_live_beats_a_session_that_only_heard_of_the_topic(self):
        self.claim("QC-740", "p3-2-defect-2a", qcweb="740")
        self.session("p3-2-defect-66", [peer("QC-740: take the defect"), reply("Needs you: stood down")])
        res = self.run_target("QC-740 next step")
        self.assertEqual(res["recommend"]["action"], "ask")
        self.assertIn("p3-2-defect-2a", res["recommend"]["why"])

    def test_a_claim_that_names_the_caller_says_the_work_is_yours(self):
        self.claim("QC-740", "caller", qcweb="740")
        self.assertEqual(self.run_target("QC-740")["recommend"]["action"], "self")

    def test_a_prompt_after_done_keeps_the_session_occupied(self):
        self.session("w", [human("a"), reply("Done (evidence: ok)"), human("now do b")])
        res = self.run_target("write a changelog")
        self.assertEqual(res["sessions"][0]["class"], "OCCUPIED")

    def test_a_key_next_to_thai_text_is_found(self):
        self.session("w", [human("แก้ QC-740ให้หน่อย")], status="busy")
        res = self.run_target("ต่อ QC-740นะ")
        self.assertEqual(res["keys"], ["QC-740"])
        self.assertEqual(res["sessions"][0]["class"], "SAME")

    def test_a_bare_number_matches_a_tag_or_a_slash_argument_but_not_a_line_number(self):
        self.session("lines", [human("see Foo.java:740 and line 740")])
        self.session("tagged", [human("defect #740 again")], status="busy")
        res = self.run_target("the spinner", keys=["740"])
        self.assertEqual({r["name"]: r["class"] for r in res["sessions"]}, {"lines": "OCCUPIED", "tagged": "SAME"})

    def test_a_claim_listing_the_key_only_as_related_is_not_its_owner(self):
        self.claim("QC-741", "other-owner", qcweb="741", related="QC-740")
        self.session("other-owner", [human("QC-741"), reply("Done (evidence: ok)")])
        self.assertNotEqual(self.run_target("QC-740")["recommend"].get("name"), "other-owner")

    def test_an_open_task_on_another_ticket_is_not_related_whatever_it_shares(self):
        self.session("w", [peer("QC-740: loader.ts and communication.ts:51 hold the spinner"),
                           reply("Needs you: pick 1 or 2")], status="busy")
        res = self.run_target("QC-801: check loader.ts communication.ts:51 timeout")
        self.assertEqual(res["sessions"][0]["class"], "OCCUPIED")

    def test_a_done_line_inside_a_turn_does_not_close_the_task(self):
        self.session("w", [human("fix SignatureV2 in SignatureSignNew.tsx"), reply("Done."), *tool_turn(),
                           reply("Needs you: pick 1 or 2")], status="busy")
        res = self.run_target("SignatureV2 SignatureSignNew.tsx follow-up")
        self.assertEqual(res["sessions"][0]["class"], "RELATED")

    def test_a_session_at_home_is_not_in_every_workspace(self):
        self.session("home", [human("y"), reply("Done (evidence: ok)")], cwd=str(Path.home()))
        workspace = str(Path.home() / "appl" / "Workspace" / "p3.2-defect")  # under home, as on the machine
        self.assertEqual(self.run_target("write a changelog", dir_=workspace)["recommend"]["action"], "spawn")

    def test_an_unreadable_transcript_is_occupied_not_a_crash(self):
        self.session("w", [human("y"), reply("Done (evidence: ok)")])
        path = next(sessions.PROJECTS_DIR.glob("*/*.jsonl"))
        path.chmod(0)
        try:
            res = self.run_target("write a changelog", dir_=WS)
        finally:
            path.chmod(0o644)
        self.assertEqual(res["sessions"][0]["class"], "OCCUPIED")

    def test_the_caller_is_found_through_the_process_tree(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.session("me", [])  # pid = this process
        self.session("parent", [], pid=os.getppid())
        self.session("other", [], pid=child.pid)  # alive, not an ancestor
        res = sessions.target("anything", [], str(self.ws))
        self.assertEqual([r["name"] for r in res["sessions"]], ["other"])

    # review round 2 (2026-11-17)
    def test_a_session_that_closed_the_topic_and_moved_on_is_not_on_it(self):
        self.session("moved-on", [human("QC-740 fix"), reply("Done (evidence: abc)"),
                                  human("KESC-10300 implement API")], status="busy")
        res = self.run_target("QC-740 reopened")
        self.assertEqual(res["sessions"][0]["class"], "OCCUPIED")
        self.assertEqual(res["recommend"]["action"], "spawn")

    def test_a_free_session_that_handled_the_topic_before_is_preferred(self):
        self.session("knows-it", [human("QC-740 fix"), reply("Done (evidence: abc)")], cwd=str(self.ws))
        self.session("newer", [human("other"), reply("Done (evidence: def)")], cwd=str(self.ws))
        res = self.run_target("QC-740 reopened")
        self.assertEqual((res["recommend"]["action"], res["recommend"]["name"]), ("send", "knows-it"))

    def test_a_claim_alias_is_the_same_ticket_not_another(self):
        self.claim("QC-740", "owner", qcweb="740", jira="KESC-10249")
        self.session("owner", [reply("Needs you: R3")], status="busy")
        self.session("alias", [human("KESC-10249: loader.ts communication.ts:51"), reply("Needs you: pick")],
                     status="busy")
        res = self.run_target("QC-740 loader.ts communication.ts:51")
        self.assertEqual({r["name"]: r["class"] for r in res["sessions"]}, {"owner": "SAME", "alias": "SAME"})
        self.assertEqual(res["recommend"]["name"], "owner")

    def test_a_list_valued_jira_field_still_names_the_ticket(self):
        self.claim("QC-750", "owner", jira="[KESC-1, KESC-2]")
        self.session("owner", [], status="busy")
        self.assertEqual(self.run_target("KESC-2 follow-up")["recommend"]["name"], "owner")

    def test_a_bare_number_inside_another_ticket_or_after_a_path_is_not_the_key(self):
        self.session("other-ticket", [human("KESC-740 is unrelated")])
        self.session("path", [human("/Users/x/Foo.java has 740 lines")])
        res = self.run_target("the spinner", keys=["740"])
        self.assertEqual({r["class"] for r in res["sessions"]}, {"OCCUPIED"})

    def test_a_missing_transcript_is_occupied(self):
        self.session("w", [])
        next(sessions.PROJECTS_DIR.glob("*/*.jsonl")).unlink()
        res = self.run_target("write a changelog", dir_=WS)
        self.assertEqual((res["sessions"][0]["class"], res["recommend"]["action"]), ("OCCUPIED", "spawn"))

    # review round 3 (2026-11-17)
    def test_a_done_line_mid_turn_of_a_busy_session_does_not_close_its_task(self):
        self.session("w", [human("QC-740 fix"), reply("Done: step 1 of 3")], status="busy")
        self.assertEqual(self.run_target("QC-740 next")["sessions"][0]["class"], "SAME")

    # peer check from s1-design-spec-7d (2026-11-17)
    def test_a_key_a_relayed_brief_only_cites_as_an_example_is_not_ownership(self):
        brief = "New task: pick the target by topic\nThe incident behind it: QC-740 went astray."
        self.session("briefed", [peer(brief)], status="busy")
        self.session("typed", [human("the incident: QC-740 went astray, look at it")], status="busy")
        res = self.run_target("QC-740 next")
        self.assertEqual({r["name"]: r["class"] for r in res["sessions"]}, {"briefed": "OCCUPIED", "typed": "SAME"})

    def test_a_free_session_outside_the_dir_says_it_is_not_offered_the_work(self):
        self.session("elsewhere", [human("y"), reply("Done (evidence: ok)")], cwd="/Users/u/other")
        row = self.run_target("write a changelog", dir_=WS)["sessions"][0]
        self.assertEqual(row["class"], "FREE")
        self.assertIn("not offered", row["evidence"])

    # review round 5 (2026-11-17)
    def test_a_relay_whose_topic_follows_a_label_line_is_same_even_while_busy(self):
        self.session("w", [peer("Task from the user:\nQC-740 spinner in loader.ts communication.ts:51")], status="busy")
        res = self.run_target("QC-740 loader.ts communication.ts:51")
        self.assertEqual((res["sessions"][0]["class"], res["recommend"]["name"]), ("SAME", "w"))

    def test_a_relay_citing_this_ticket_below_its_topic_can_still_be_related(self):
        self.session("w", [peer("Spinner refactor in loader.ts communication.ts:51\nseen first in QC-740")],
                     status="busy")
        self.assertEqual(self.run_target("QC-740 loader.ts communication.ts:51")["sessions"][0]["class"], "RELATED")

    def test_a_free_session_whose_last_line_says_not_offered_is_still_offered(self):
        self.session("w", [human("y"), reply("Done (evidence: abc) — option 2 not offered to QA")], cwd=str(self.ws))
        self.assertEqual(self.run_target("write a changelog")["recommend"]["name"], "w")

    # peer check from s1-design-spec-7d, round 2 (2026-11-17): records that ask nothing, in their real shapes
    def test_a_compaction_summary_restating_a_brief_is_not_a_prompt(self):
        summary = {"type": "user", "isVisibleInTranscriptOnly": True, "isCompactSummary": True, "isSidechain": False,
                   "turnOrigin": "peer", "message": {"role": "user", "content": (
                       "This session is being continued from a previous conversation that ran out of context.\n"
                       "Summary:\n1. the incident: QC-740 went astray")}}
        brief = "New task: pick the target by topic\nThe incident behind it: QC-740 went astray."
        self.session("compacted", [peer(brief), summary], status="busy")
        self.assertEqual(self.run_target("QC-740 next")["sessions"][0]["class"], "OCCUPIED")

    def test_a_shell_command_its_output_and_an_interrupt_ask_nothing(self):
        user = {"type": "user", "isSidechain": False}
        records = [human("y"), reply("Done (evidence: ok)"),
                   {**user, "message": {"role": "user", "content": "<bash-input>git log QC-740</bash-input>"}},
                   {**user, "message": {"role": "user", "content": "<bash-stdout>QC-740 fix</bash-stdout><bash-stderr>"
                                                                  "</bash-stderr>"}},
                   {**user, "message": {"role": "user", "content": [{"type": "text",
                                                                    "text": "[Request interrupted by user]"}]}}]
        self.session("w", records, cwd=str(self.ws))
        res = self.run_target("QC-740 next")
        self.assertEqual((res["sessions"][0]["class"], res["recommend"]["name"]), ("FREE", "w"))


class TargetCliTest(unittest.TestCase):
    def test_exit_3_and_json_when_the_user_must_choose(self):
        with tempfile.TemporaryDirectory() as home:
            reg = Path(home, ".claude", "sessions")
            reg.mkdir(parents=True)
            for i, name in enumerate(("a", "b"), 1):
                sid = f"00000000-0000-0000-0000-00000000000{i}"
                rec = {"pid": os.getpid(), "sessionId": sid, "cwd": home, "kind": "interactive", "name": name,
                       "status": "idle"}
                reg.joinpath(f"{i}.json").write_text(json.dumps(rec))
                folder = Path(home, ".claude", "projects", "".join(c if c.isalnum() else "-" for c in home))
                folder.mkdir(parents=True, exist_ok=True)
                folder.joinpath(f"{sid}.jsonl").write_text(json.dumps(human("QC-1 trace")) + "\n")
            r = subprocess.run([str(ROOT / "bin" / "bridge-plan"), "target", "QC-1", "--self", "caller", "-d", home,
                                "--json"], env={**os.environ, "HOME": home}, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 3, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["recommend"]["action"], "ask")
            self.assertEqual(sorted(s["class"] for s in out["sessions"]), ["SAME", "SAME"])


if __name__ == "__main__":
    unittest.main()
