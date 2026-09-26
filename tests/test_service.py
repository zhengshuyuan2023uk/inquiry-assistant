"""Workflow tests use explicitly fake model output, never presented as live AI."""
import json
import tempfile
import unittest
from pathlib import Path

from inquiry_product.core.service import InquiryService
from inquiry_product.core.store import Store


class OfflineTestRunner:
    name = "offline_test"

    def __init__(self, hook=None):
        self.contexts = []
        self.hook = hook

    def analyze(self, context):
        self.contexts.append(context)
        if self.hook:
            self.hook()
        return {"summary": "仅测试流程", "facts": [],
                "missing_fields": context["required_fields"], "uncertainties": ["离线测试"],
                "next_action": "人工核对", "reply": "仅供测试的草稿，请补充需求。",
                "citations": [{"id": d["id"], "version": d["version"]} for d in context["knowledge"]],
                "needs_human": True}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "test.sqlite3")
        self.configs = self.root / "projects"
        self.configs.mkdir()
        for project in ("a", "b"):
            data = {"id": project, "mode": "simulation", "name": project, "industry": "test",
                    "required_fields": ["quantity"], "rules": ["不能编造"], "knowledge": [
                        {"id": project + "-only", "version": "1", "title": "模拟", "content": project + " private knowledge",
                         "valid_from": "2026-09-25", "valid_until": "2026-09-26"}]}
            (self.configs / (project + ".json")).write_text(json.dumps(data))
        self.runner = OfflineTestRunner()
        self.service = InquiryService(self.store, self.configs, self.runner)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def message(self, project="a", id="m1", day="2026-09-25", body="客户需求"):
        return {"project_id": project, "account_id": "simulation-account", "conversation_id": "chat",
                "message_id": id, "direction": "inbound", "body": body,
                "sent_at": day + "T08:00:00Z", "received_at": day + "T08:01:00Z"}

    def analyze(self):
        return self.service.analyze("a", "simulation-account", "chat", "2026-09-25")

    def review(self, draft, decision="approve", text="人工确认后的测试文案", day="2026-09-25"):
        return self.service.review(draft["id"], decision, "simulation-reviewer", text, day)

    def test_project_isolation_and_arbitrary_message_are_real_inputs(self):
        self.service.add_message(self.message(body="本次新增一条独立需求"))
        self.service.add_message(self.message(project="b", body="B 的不可见消息"))
        draft = self.analyze()
        serialized = json.dumps(self.runner.contexts[-1], ensure_ascii=False)
        self.assertIn("本次新增一条独立需求", serialized)
        self.assertNotIn("b private knowledge", serialized)
        self.assertNotIn("B 的不可见消息", serialized)
        self.assertEqual(draft["runner"], "offline_test")

    def test_human_edit_approval_is_idempotent_and_rejection_creates_no_outbox(self):
        self.service.add_message(self.message())
        draft = self.analyze()
        self.review(draft)
        self.review(draft)
        self.assertEqual(len(self.store.outbox("a")), 1)
        self.assertEqual(self.store.outbox("a")[0]["final_text"], "人工确认后的测试文案")
        self.assertEqual(self.store.get_draft(draft["id"])["result"]["reply"], "仅供测试的草稿，请补充需求。")
        self.service.add_message(self.message(id="m2", body="第二个需求"))
        rejected = self.analyze()
        self.review(rejected, "reject", "原因：缺乏资料")
        self.assertEqual(len(self.store.outbox("a")), 1)

    def test_late_arrival_invalidates_pending_and_is_not_skipped(self):
        self.service.add_message(self.message())
        draft = self.analyze()
        late = self.message(id="m0", day="2026-09-24", body="迟到的重要条件")
        late["received_at"] = "2026-09-25T09:00:00Z"
        self.service.add_message(late)
        with self.assertRaises(ValueError):
            self.review(draft)
        self.assertEqual(self.store.get_draft(draft["id"])["status"], "stale")
        self.analyze()
        self.assertEqual(self.runner.contexts[-1]["messages"][0]["message_id"], "m0")

    def test_knowledge_expiry_or_mid_run_change_blocks_approval_or_save(self):
        self.service.add_message(self.message())
        draft = self.analyze()
        with self.assertRaises(ValueError):
            self.review(draft, day="2026-09-27")
        def change_rules():
            path = self.configs / "a.json"
            data = json.loads(path.read_text())
            data["rules"].append("新增规则")
            path.write_text(json.dumps(data))
        self.service.runner = OfflineTestRunner(hook=change_rules)
        with self.assertRaises(ValueError):
            self.analyze()

    def test_future_state_is_not_used_in_past_analysis(self):
        self.service.add_message(self.message(day="2026-09-26"))
        with self.assertRaises(ValueError):
            self.analyze()

    def test_import_validates_entire_batch_before_writing(self):
        fixture = self.root / "scenarios.json"
        fixture.write_text(json.dumps({"scenarios": [{"messages": [self.message(), self.message(project="unknown")]}]}))
        with self.assertRaises(ValueError):
            self.service.import_scenarios(fixture, "2026-09-25")
        self.assertEqual(self.store.conversations(), [])


if __name__ == "__main__":
    unittest.main()
