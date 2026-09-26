import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inquiry_product.cli import main
from inquiry_product.core.engine import EngineError
from inquiry_product.workspace import Workspace


class TestRunner:
    name = "offline_delivery_test"

    def __init__(self, **kwargs):
        pass

    def analyze(self, context):
        return {"summary": "离线交付流程测试", "facts": [], "missing_fields": context["required_fields"],
                "uncertainties": ["非真实模型输出"], "next_action": "核对资料", "reply": "测试草稿，等待人工确认。",
                "citations": [], "needs_human": True}


class CLIDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "company.json"
        self.data = {"id": "alpha", "name": "独立测试客户", "mode": "simulation", "industry": "test",
                     "required_fields": ["quantity"], "rules": ["不承诺未知事项"], "knowledge": []}
        self.config.write_text(json.dumps(self.data))
        self.workspace = self.root / "workspace"

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, *args, root=None):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(["--workspace", str(root or self.workspace), *args])
        return code, json.loads(output.getvalue() or error.getvalue().splitlines()[-1])

    def initialize(self, mode="simulation"):
        self.data["mode"] = mode
        self.config.write_text(json.dumps(self.data))
        code, _ = self.command("init", "--company", "alpha", "--config", str(self.config), "--mode", mode)
        self.assertEqual(code, 0)
        records = [{"project_id": "alpha", "account_id": "a", "conversation_id": "c", "message_id": "m1",
                    "direction": "inbound", "body": "想采购一批货。", "sent_at": "2026-09-25T01:00:00Z",
                    "received_at": "2026-09-25T01:01:00Z", "mode": mode}]
        path = self.root / "messages.json"
        path.write_text(json.dumps(records))
        code, result = self.command("import", "--file", str(path), "--source", "test-input")
        self.assertEqual(code, 0)
        return path

    @patch("inquiry_product.cli.CodexRunner", TestRunner)
    def test_independent_delivery_backup_restore_and_manual_queue(self):
        path = self.initialize()
        code, repeat = self.command("import", "--file", str(path), "--source", "test-input")
        self.assertEqual(code, 0)
        self.assertEqual(repeat["inserted"], 0)
        code, draft = self.command("analyze", "--account", "a", "--conversation", "c", "--as-of", "2026-09-25")
        self.assertEqual(code, 0)
        code, review = self.command("review", "--id", draft["id"], "--decision", "approve", "--text", "人工最终文本", "--as-of", "2026-09-25")
        self.assertEqual(code, 0)
        self.assertEqual(review["status"], "approved")
        code, runs = self.command("runs")
        self.assertEqual(runs[0]["status"], "succeeded")
        archive = self.root / "backup.zip"
        code, _ = self.command("backup", "--output", str(archive))
        self.assertEqual(code, 0)
        restored = self.root / "restored"
        code, _ = self.command("restore", "--archive", str(archive), "--destination", str(restored))
        self.assertEqual(code, 0)
        code, outbox = self.command("outbox", root=restored)
        self.assertEqual(code, 0)
        self.assertEqual(outbox[0]["final_text"], "人工最终文本")
        code, restored_runs = self.command("runs", root=restored)
        self.assertEqual(restored_runs[0]["id"], runs[0]["id"])

    @patch("inquiry_product.cli.CodexRunner", TestRunner)
    def test_customer_model_requires_explicit_data_permission(self):
        self.initialize(mode="customer")
        args = ("analyze", "--account", "a", "--conversation", "c", "--as-of", "2026-09-25")
        code, error = self.command(*args)
        self.assertEqual(code, 1)
        self.assertIn("客户数据", error["error"])
        code, draft = self.command(*args, "--allow-customer-model")
        self.assertEqual(code, 0)
        self.assertEqual(draft["mode"], "customer")
        self.command("review", "--id", draft["id"], "--decision", "approve", "--use-original", "--as-of", "2026-09-25")
        code, rows = self.command("outbox")
        self.assertEqual(rows[0]["delivery_state"], "approved_for_manual_send")

    def test_failure_is_persisted_without_chat_text_in_run_log(self):
        self.initialize()
        class FailingRunner(TestRunner):
            def analyze(self, context):
                raise EngineError("执行失败", category="network")
        with patch("inquiry_product.cli.CodexRunner", FailingRunner):
            code, _ = self.command("analyze", "--account", "a", "--conversation", "c", "--as-of", "2026-09-25")
        self.assertEqual(code, 1)
        code, runs = self.command("runs")
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[0]["error_category"], "network")
        self.assertNotIn("想采购", json.dumps(runs, ensure_ascii=False))

    @patch("inquiry_product.cli.CodexRunner", TestRunner)
    def test_approved_history_is_preserved_but_outbox_warns_after_knowledge_changes(self):
        self.initialize()
        code, draft = self.command("analyze", "--account", "a", "--conversation", "c", "--as-of", "2026-09-25")
        self.assertEqual(code, 0)
        self.command("review", "--id", draft["id"], "--decision", "approve", "--use-original", "--as-of", "2026-09-25")
        self.data["rules"].append("资料已有变更，需要复核")
        self.config.write_text(json.dumps(self.data))
        self.command("publish", "--config", str(self.config))
        code, outbox = self.command("outbox")
        self.assertEqual(code, 0)
        self.assertTrue(outbox[0]["requires_recheck"])
        self.assertEqual(outbox[0]["delivery_status"], "external_delivery_unknown")
        self.assertFalse(outbox[0]["tool_delivery_performed"])
        code, history = self.command("draft", "--id", draft["id"])
        self.assertEqual(history["status"], "approved")


if __name__ == "__main__":
    unittest.main()
