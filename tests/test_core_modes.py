"""Offline checks for persistent provenance and all-or-nothing imports."""
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from inquiry_product.core.config import ProjectConfig
from inquiry_product.core.engine import CodexRunner, EngineError, build_prompt, validate_result
from inquiry_product.core.service import InquiryService
from inquiry_product.core.store import Store, StoreError, normalize_message


def config(mode="customer"):
    # All test inputs are constructed here; no actual customer data is read.
    return {"id": "a", "mode": mode, "name": "Test company", "industry": "test",
            "required_fields": ["quantity"], "rules": ["Check before committing"], "knowledge": []}


def message(identity="m1", mode="customer", **changes):
    return {"project_id": "a", "account_id": "account", "conversation_id": "chat",
            "message_id": identity, "mode": mode, "direction": "inbound", "body": "Request details",
            "sent_at": "2026-09-25T08:00:00Z", "received_at": "2026-09-25T08:01:00Z", **changes}


def context(mode="customer"):
    return {**ProjectConfig(config(mode)).context("2026-09-25"), "messages": [message(mode=mode)]}


def result():
    return {"summary": "Test request", "facts": [], "missing_fields": ["quantity"],
            "uncertainties": [], "next_action": "Ask quantity", "reply": "What quantity?",
            "citations": [], "needs_human": True}


class OfflineRunner:
    name = "offline_test"

    def analyze(self, current):
        return result()


class CoreModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_database_mode_is_bound_at_creation_and_survives_reopen(self):
        path = self.root / "db.sqlite3"
        store = Store(path, mode="customer")
        store.ingest(message())
        self.assertEqual(dict(store.connection.execute("SELECT key,value FROM metadata")),
                         {"schema_version": "2", "mode": "customer"})
        store.close()
        with self.assertRaises(StoreError):
            Store(path, mode="simulation")
        store = Store(path, mode="customer")
        try:
            self.assertEqual(store.mode, "customer")
            self.assertEqual(store.messages("a", "account", "chat"), [message()])
        finally:
            store.close()

    def test_unbound_or_unknown_version_database_is_not_silently_adopted(self):
        legacy = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.execute("CREATE TABLE old_records (id TEXT)")
        connection.close()
        with self.assertRaises(StoreError):
            Store(legacy)
        store = Store(self.root / "db.sqlite3")
        store.connection.execute("UPDATE metadata SET value='99' WHERE key='schema_version'")
        store.close()
        with self.assertRaises(StoreError):
            Store(self.root / "db.sqlite3")

    def test_message_mode_is_preserved_and_mismatches_are_rejected(self):
        self.assertEqual(normalize_message(message())["mode"], "customer")
        legacy = message()
        del legacy["mode"]
        self.assertEqual(normalize_message(legacy)["mode"], "simulation")
        for mode in ("simulation", "customer"):
            store = Store(":memory:", mode=mode)
            try:
                opposite = "customer" if mode == "simulation" else "simulation"
                with self.assertRaises(StoreError):
                    store.ingest(message(mode=opposite))
                self.assertEqual(store.conversations(), [])
            finally:
                store.close()

    def test_service_rejects_project_mode_change_before_calling_runner(self):
        configs = self.root / "projects"
        configs.mkdir()
        path = configs / "a.json"
        path.write_text(json.dumps(config()), encoding="utf-8")
        store = Store(":memory:", mode="customer")
        try:
            service = InquiryService(store, configs, OfflineRunner())
            service.add_message(message())
            path.write_text(json.dumps(config("simulation")), encoding="utf-8")
            with patch.object(service.runner, "analyze") as run, self.assertRaises(ValueError):
                service.analyze("a", "account", "chat", "2026-09-25")
            run.assert_not_called()
            with self.assertRaises(ValueError):
                InquiryService(store, configs, OfflineRunner())
        finally:
            store.close()

    def test_customer_prompt_keeps_mode_without_synthetic_label(self):
        current = context()
        prompt = build_prompt(current)
        self.assertIn('"mode": "customer"', prompt)
        self.assertIn("获准", prompt)
        self.assertNotIn("虚构", prompt)
        self.assertNotIn("模拟上下文", prompt)
        self.assertEqual(validate_result(result(), current), result())
        self.assertIn("虚构", build_prompt(context("simulation")))

    def test_engine_rejects_context_message_mode_mismatch(self):
        for mode in ("simulation", "customer"):
            current = context(mode)
            current["messages"][0]["mode"] = "customer" if mode == "simulation" else "simulation"
            with self.assertRaises(EngineError):
                build_prompt(current)

    def test_customer_uses_same_restricted_runner_and_result_schema(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            Path(command[command.index("-o") + 1]).write_text(json.dumps(result()), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch("inquiry_product.core.engine.subprocess.run", side_effect=run):
            self.assertEqual(CodexRunner().analyze(context()), result())
        command, kwargs = calls[0]
        self.assertIn("--ephemeral", command)
        self.assertIn("mcp_servers={}", command)
        self.assertIn('web_search="disabled"', command)
        self.assertIn('"mode": "customer"', kwargs["input"])

    def test_draft_report_and_outbox_preserve_mode_and_never_claim_sent(self):
        for mode, delivery in (("simulation", "simulation_only"),
                               ("customer", "approved_for_manual_send")):
            store = Store(":memory:", mode=mode)
            try:
                store.ingest(message(mode=mode))
                current = context(mode)
                draft = store.save_draft("a", "account", "chat", store.fingerprint("a", "account", "chat"),
                                         current["knowledge_digest"], "2026-09-25", result(), "offline_test", current)
                store.review(draft["id"], "approve", "tester", "Approved text",
                             current["knowledge_digest"], "2026-09-25")
                self.assertEqual(draft["mode"], mode)
                self.assertEqual(store.outbox()[0]["mode"], mode)
                self.assertEqual(store.outbox()[0]["delivery_state"], delivery)
                self.assertEqual(store.daily("a", "2026-09-25")["mode"], mode)
            finally:
                store.close()

    def test_draft_snapshot_mode_must_match_store(self):
        store = Store(":memory:", mode="customer")
        try:
            store.ingest(message())
            with self.assertRaises(StoreError):
                store.save_draft("a", "account", "chat", store.fingerprint("a", "account", "chat"),
                                 "digest", "2026-09-25", result(), "offline_test", {"mode": "simulation"})
        finally:
            store.close()

    def test_batch_conflict_or_invalid_record_leaves_no_partial_inserts(self):
        store = Store(":memory:", mode="customer")
        try:
            store.ingest(message())
            for batch in ([message("m2"), message(body="Conflicting payload")],
                          [message("m2"), message("m3", mode="simulation")],
                          [message("m2"), message("m2", body="Conflicting batch payload")]):
                with self.subTest(batch=batch), self.assertRaises(ValueError):
                    store.ingest_many(batch)
                self.assertEqual(store.messages("a", "account", "chat"), [message()])
            self.assertEqual(store.ingest_many([message(), message("m2"), message("m2")]),
                             {"inserted": 1, "duplicates": 2})
        finally:
            store.close()

    def test_batch_storage_failure_rolls_back_inserts_and_draft_staleness(self):
        store = Store(":memory:", mode="customer")
        try:
            store.ingest(message())
            draft = store.save_draft("a", "account", "chat", store.fingerprint("a", "account", "chat"),
                                     "digest", "2026-09-25", result(), "offline_test")
            store.connection.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON messages
                WHEN NEW.message_id='m3' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                store.ingest_many([message("m2"), message("m3")])
            self.assertEqual(store.messages("a", "account", "chat"), [message()])
            self.assertEqual(store.get_draft(draft["id"])["status"], "pending")
        finally:
            store.close()

    def test_batch_can_join_outer_transaction_for_atomic_source_audit(self):
        store = Store(":memory:", mode="customer")
        try:
            store.connection.execute("CREATE TABLE audit (batch_id TEXT UNIQUE)")
            store.connection.execute("INSERT INTO audit VALUES ('existing')")
            store.connection.execute("BEGIN IMMEDIATE")
            store.ingest_many([message()])
            with self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute("INSERT INTO audit VALUES ('existing')")
            store.connection.rollback()
            self.assertEqual(store.messages("a", "account", "chat"), [])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
