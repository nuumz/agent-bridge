"""Unit tests for bridgelib.grill — recommend matrix + gate_before_run / open_field_todos."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridgelib import grill  # noqa: E402


class Recommend(unittest.TestCase):
    def test_routine_skips(self):
        g = grill.recommend({"name": "x", "grill": "recommend", "form": "panel"}, ["a", "b"], "which option?", "routine")
        self.assertEqual(g["do"], "no")
        self.assertEqual(g["status"], "skipped")

    def test_pipeline_with_files_skips(self):
        g = grill.recommend(
            {"name": "implement", "grill": "recommend", "form": "pipeline"},
            ["touch only"],
            "add mapper --files src/X.java --verify 'mvn test'",
            "negotiate",
        )
        self.assertEqual(g["do"], "no")
        self.assertEqual(g["status"], "skipped")

    def test_pipeline_fuzzy_forces_grill(self):
        g = grill.recommend(
            {"name": "implement", "grill": "recommend", "form": "pipeline"},
            ["touch only", "verify"],
            "which approach should we take for the mapper?",
            "negotiate",
        )
        self.assertEqual(g["do"], "yes")
        self.assertEqual(g["when"], "before-agree")
        self.assertEqual(g["status"], "TODO")

    def test_fix_flow_optional_after_findings(self):
        g = grill.recommend(
            {"name": "analyze-decide-fix", "grill": "recommend", "form": "fix-flow"},
            [],
            "root cause then fix",
            "negotiate",
        )
        self.assertEqual(g["do"], "optional")
        self.assertEqual(g["when"], "after-findings")
        self.assertNotEqual(g["status"], "TODO")

    def test_confirm_pending_not_todo(self):
        g = grill.recommend(
            {"name": "design-decision", "grill": "recommend", "form": "panel"},
            ["goal", "options"],
            "decide the grain",
            "confirm",
        )
        self.assertEqual(g["status"], "pending")
        self.assertEqual(g["when"], "before-run")


class Gate(unittest.TestCase):
    def _card(self, do="yes", when="before-run", status="TODO", extra="- goal: done\n"):
        return grill.card_section({
            "do": do, "who": "main-agent", "when": when, "max_rounds": 1,
            "status": status, "skill": "grilling", "reason": "test",
        }) + extra

    def test_refuse_yes_todo_before_run(self):
        err, warn = grill.gate_before_run(self._card())
        self.assertIsNotNone(err)
        self.assertIsNone(warn)

    def test_warn_pending(self):
        err, warn = grill.gate_before_run(self._card(status="pending"))
        self.assertIsNone(err)
        self.assertIsNotNone(warn)

    def test_after_findings_ignores_todo(self):
        err, warn = grill.gate_before_run(self._card(when="after-findings", status="TODO"))
        self.assertIsNone(err)
        self.assertIsNone(warn)

    def test_open_field_todos_ignores_grill_status(self):
        text = "# Card\n- goal: TODO: fill me\n" + self._card(status="TODO")
        todos = grill.open_field_todos(text)
        self.assertEqual(len(todos), 1)
        self.assertIn("goal", todos[0])

    def test_grill_blocks_agree(self):
        self.assertIsNotNone(grill.grill_blocks_agree(self._card(when="before-agree")))
        self.assertIsNone(grill.grill_blocks_agree(self._card(when="after-findings")))


    def test_task_prose_todo_ignored(self):
        text = "# Card\nTask:\nremove TODO comments from Foo\n- goal: settled\n" + self._card(status="done")
        self.assertEqual(grill.open_field_todos(text), [])

    def test_marker_todo_still_caught(self):
        text = "# Card\n- goal: TODO: fill\n" + self._card(status="done")
        todos = grill.open_field_todos(text)
        self.assertEqual(len(todos), 1)


    def test_negotiate_todo_colon_prefix(self):
        # bridge-plan negotiate fields are "TODO: …" lines, not "goal: TODO"
        text = "## Goal\nTODO: what is being decided\n\n## Options\nTODO: list them\n\n## Done when\nTODO: observable\n\n## Budget\nTODO\n"
        todos = grill.open_field_todos(text)
        self.assertGreaterEqual(len(todos), 4)

    def test_mid_sentence_todo_still_ignored(self):
        text = "## Task\nPlease remove TODO comments from the mapper.\n- goal: settled\n"
        self.assertEqual(grill.open_field_todos(text), [])


class GrillEvidence(unittest.TestCase):
    """A do=yes grill closed by the main agent from its own context (pay-simplify, 2026-09-29) must not pass."""

    SID = "76fdcaaf-0000-4000-8000-000000000000"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = grill.PROJECTS, grill.CONV
        grill.PROJECTS, grill.CONV = root / "projects", root / "conv"
        (grill.CONV / "panel-pay-guarantees-spec-reader").mkdir(parents=True)
        proj = grill.PROJECTS / "-Users-x-repo"
        proj.mkdir(parents=True)
        records = [
            {"type": "user", "message": {"content": "ตามแนะนำ, แต่พิจารณา workflow task ที่ลูกค้าไม่เคยจ่าย"}},
            {"type": "user", "isCompactSummary": True, "message": {"content": "user ruled: fewer lines wins"}},
            {"type": "user", "toolUseResult": {"stdout": "echo: money may be dropped"},
             "message": {"content": [{"type": "tool_result", "content": "money may be dropped"}]}},
            {"type": "user", "toolUseResult": {"questions": [], "answers": {"G5?": "out of scope for now"}},
             "message": {"content": [{"type": "tool_result", "content": "answered"}]}},
            {"type": "queue-operation", "operation": "enqueue", "content": "ถาม agent ใน bridge เองก็ได้"},
        ]
        (proj / f"{self.SID}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records))
        self.src = root / "brd.md"
        self.src.write_text("ขั้นตอนที่ 11 ระบบปิด Popup และแสดงหน้ารายการ\n")

    def tearDown(self):
        grill.PROJECTS, grill.CONV = self.saved
        self.tmp.cleanup()

    def _card(self, log, status="done", who="main-agent"):
        return grill.card_section({
            "do": "yes", "who": who, "when": "before-agree", "max_rounds": 2,
            "status": status, "skill": "grilling", "reason": "test",
        }) + log + "\n## Out of scope\nnone\n"

    def _refused(self, log, **kw):
        card = self._card(log, **kw)
        return grill.gate_before_run(card)[0] is not None and grill.grill_blocks_agree(card) is not None

    def test_no_question_logged_refused(self):
        self.assertTrue(self._refused(""))
        self.assertTrue(self._refused("", status="skipped"))

    def test_main_agent_self_answer_does_not_count(self):
        self.assertTrue(self._refused('- Q: what must hold?\n  A (main): "fewer lines"\n'))

    def test_user_words_found_in_transcript_pass(self):
        log = f'- Q [decision]: which guarantees are mandatory?\n  A (user {self.SID[:8]}): "ตามแนะนำ" — G1–G4\n'
        self.assertFalse(self._refused(log))

    def test_mid_turn_message_and_ask_user_answer_count(self):
        log = (f'- Q: may a peer answer?\n  A (user {self.SID[:8]}): "ถาม agent ใน bridge เองก็ได้"\n'
               f'- Q: G5?\n  A (user {self.SID[:8]}): "out of scope for now"\n')
        self.assertFalse(self._refused(log))

    def test_user_quote_not_typed_by_user_refused(self):
        for words in ("user ruled: fewer lines wins", "money may be dropped", "G1–G4 must hold"):
            with self.subTest(words=words):
                self.assertTrue(self._refused(f'- Q: what must hold?\n  A (user {self.SID[:8]}): "{words}"\n'))

    def test_peer_cannot_settle_a_decision(self):
        log = "- Q [decision]: what must hold?\n  A (peer claude panel-pay-guarantees-spec-reader): G1–G4\n"
        self.assertTrue(self._refused(log))

    def test_peer_answers_analysis_when_conv_exists(self):
        ok = "- Q [analysis]: is G1 at risk?\n  A (peer claude panel-pay-guarantees-spec-reader): yes, via P1\n"
        self.assertFalse(self._refused(ok))
        self.assertTrue(self._refused(ok.replace("spec-reader", "never-ran")))
        self.assertTrue(self._refused(ok.replace("panel-pay-guarantees-spec-reader", "..")))

    def test_source_quote_must_be_in_the_file(self):
        ok = f'- Q [fact]: what does ยืนยัน fire?\n  A (source {self.src}): "ระบบปิด Popup และแสดงหน้ารายการ"\n'
        self.assertFalse(self._refused(ok))
        self.assertTrue(self._refused(ok.replace("แสดงหน้ารายการ", "ยิง warning-ack")))

    def test_every_question_needs_an_answer(self):
        log = (f'- Q: which guarantees?\n  A (user {self.SID[:8]}): "ตามแนะนำ"\n'
               "- Q [fact]: does the webhook resend?\n  A (main): yes\n")
        self.assertTrue(self._refused(log))

    def test_panel_leader_needs_no_log(self):
        card = grill.card_section({
            "do": "optional", "who": "panel-leader", "when": "after-findings", "max_rounds": 1,
            "status": "skipped", "skill": "grilling", "reason": "test",
        })
        self.assertNotIn("## Grill log", card)
        self.assertEqual(grill.gate_before_run(card), (None, None))


def _load_bridge_plan():
    path = Path(__file__).resolve().parents[1] / "bridge-plan"
    loader = importlib.machinery.SourceFileLoader("bridge_plan_mod", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class MustHold(unittest.TestCase):
    """A design-decision card cannot be agreed while its business guarantees are unstated."""

    @classmethod
    def setUpClass(cls):
        cls.bp = _load_bridge_plan()
        cls.book = {"name": "design-decision", "grill": "recommend", "form": "panel"}

    def test_request_without_invariant_is_a_gap(self):
        gaps = self.bp.missing_inputs(self.book, "simplify P1-P5 in PaymentLinkServiceImpl.java, options A or B, user decides")
        self.assertEqual(gaps, ["the invariant that must hold"])

    def test_negotiate_card_leaves_must_hold_open(self):
        text = self.bp.agreement_text("x", self.book, "req", "claude", [], Path("/tmp/x.md"), "negotiate")
        must_hold = text.split("## Must hold", 1)[1].split("\n## ", 1)[0]
        self.assertTrue(grill.open_field_todos(must_hold))


if __name__ == "__main__":
    unittest.main()
