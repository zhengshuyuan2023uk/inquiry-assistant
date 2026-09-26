"""Synthetic workbench provenance and non-destructive creation checks."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from inquiry_product.core.engine import validate_result
from inquiry_product.web_demo import (ACCOUNT_ID, AS_OF, COMPANY_ID, DEFAULT_CONVERSATION,
                                      MARKER_FILE, conversation_labels, demo_summary, seed_workspace)
from inquiry_product.workspace import Workspace


class WorkbenchDemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_has_scoped_messages_valid_fixture_drafts_and_unsent_approval(self):
        with patch("inquiry_product.core.engine.subprocess.run", side_effect=AssertionError("no model calls")):
            workspace = seed_workspace(self.root / "demo")
        stats = demo_summary(workspace)
        self.assertEqual((stats["conversations"], stats["messages"], stats["drafts"]), (5, 15, 4))
        self.assertEqual((stats["pending"], stats["approved"], stats["unanalysed"]), (3, 1, 1))
        self.assertEqual((stats["real_model_calls"], stats["external_messages_sent"]), (0, 0))
        self.assertEqual(stats["draft_source"], "demo_fixture")
        workspace = Workspace.load(stats["workspace"])
        self.assertEqual((workspace.company_id, workspace.mode), (COMPANY_ID, "simulation"))
        labels = conversation_labels(workspace)
        self.assertEqual(len(labels), 5)
        self.assertEqual(labels[(ACCOUNT_ID, DEFAULT_CONVERSATION)]["title"], "Ethan Cole")
        self.assertTrue(all("虚构客户" in item["subtitle"] for item in labels.values()))
        store = workspace.open_store()
        try:
            for conversation in store.conversations():
                rows = store.messages(COMPANY_ID, ACCOUNT_ID, conversation["conversation_id"])
                self.assertEqual(len(rows), 3)
                self.assertTrue(all(item["mode"] == "simulation" and item["project_id"] == COMPANY_ID for item in rows))
            identifiers = [row[0] for row in store.connection.execute("SELECT id FROM drafts")]
            for identity in identifiers:
                draft = store.get_draft(identity)
                self.assertEqual(draft["runner"], "demo_fixture")
                self.assertEqual(draft["as_of"], AS_OF)
                self.assertEqual(validate_result(draft["result"], draft["context"]), draft["result"])
            self.assertEqual(len(store.outbox()), 1)
            self.assertEqual(store.outbox()[0]["delivery_state"], "simulation_only")
            reviewer = store.connection.execute("SELECT reviewer FROM review_events").fetchone()[0]
            self.assertIn("演练", reviewer)
        finally:
            store.close()

    def test_existing_workspace_and_nonempty_directory_are_never_overwritten(self):
        target = self.root / "demo"
        seed_workspace(target)
        before = (target / "workspace.json").read_bytes()
        with self.assertRaises(ValueError):
            seed_workspace(target)
        self.assertEqual((target / "workspace.json").read_bytes(), before)
        other = self.root / "ordinary"
        other.mkdir()
        (other / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            seed_workspace(other)
        self.assertEqual((other / "keep.txt").read_text(), "keep")

    def test_empty_directory_is_supported_but_symlink_target_is_rejected(self):
        target = self.root / "empty"
        target.mkdir()
        seed_workspace(target)
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            seed_workspace(alias)

    def test_copied_or_malformed_marker_does_not_relabel_other_workspaces(self):
        seed_workspace(self.root / "one")
        seed_workspace(self.root / "two")
        source = self.root / "one" / MARKER_FILE
        other = Workspace.load(self.root / "two")
        target = other.root / MARKER_FILE
        valid = target.read_text(encoding="utf-8")
        target.write_bytes(source.read_bytes())
        self.assertEqual(conversation_labels(other), {})
        for corrupt in ("[]", "{}", "{broken", valid.replace('"mode": "simulation"', '"mode": "customer"')):
            target.write_text(corrupt, encoding="utf-8")
            self.assertEqual(conversation_labels(other), {})
        target.write_text(valid, encoding="utf-8")
        self.assertEqual(len(conversation_labels(other)), 5)

    def test_customer_workspace_never_uses_demo_labels(self):
        seed_workspace(self.root / "demo")
        config = {"id": COMPANY_ID, "name": "Customer company", "mode": "customer", "industry": "logistics",
                  "required_fields": ["goods"], "rules": ["Verify before quoting"], "knowledge": []}
        customer = Workspace.init(self.root / "customer", COMPANY_ID, config, mode="customer")
        (customer.root / MARKER_FILE).write_bytes((self.root / "demo" / MARKER_FILE).read_bytes())
        self.assertEqual(conversation_labels(customer), {})

    def test_labels_survive_added_messages_but_fail_when_seed_messages_are_missing(self):
        seed_workspace(self.root / "demo")
        workspace = Workspace.load(self.root / "demo")
        record = {"project_id": COMPANY_ID, "account_id": ACCOUNT_ID, "conversation_id": DEFAULT_CONVERSATION,
                  "message_id": "follow-up", "mode": "simulation", "direction": "inbound", "body": "The volume is 4 CBM.",
                  "sent_at": AS_OF + "T01:00:00Z", "received_at": AS_OF + "T01:00:01Z"}
        workspace.import_messages([record], source_label="test follow-up")
        self.assertEqual(len(conversation_labels(workspace)), 5)
        store = workspace.open_store()
        try:
            store.connection.execute("DELETE FROM messages WHERE message_id='wb-new-paper-01'")
        finally:
            store.close()
        self.assertEqual(conversation_labels(workspace), {})

    def test_seed_failure_leaves_no_partial_target(self):
        with patch("inquiry_product.web_demo._DemoFixtureRunner.analyze", side_effect=ValueError("fixture failure")):
            with self.assertRaisesRegex(ValueError, "fixture failure"):
                seed_workspace(self.root / "failed")
        self.assertFalse((self.root / "failed").exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_script_requires_explicit_target_and_reports_synthetic_source(self):
        product = Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, "-B", str(product / "create_workbench_demo.py"),
                                 "--workspace", str(self.root / "cli")], cwd=product,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["draft_source"], "demo_fixture")
        repeat = subprocess.run([sys.executable, "-B", str(product / "create_workbench_demo.py"),
                                 "--workspace", str(self.root / "cli")], cwd=product,
                                capture_output=True, text=True)
        self.assertEqual(repeat.returncode, 1)
        self.assertIn("error", json.loads(repeat.stderr))
        missing = subprocess.run([sys.executable, "-B", str(product / "create_workbench_demo.py")], cwd=product,
                                 capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)


if __name__ == "__main__":
    unittest.main()
