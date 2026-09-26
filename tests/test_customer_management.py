"""Customer discovery uses synthetic metadata, explicit selection and scoped sync."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile
import tempfile
import unittest
from unittest.mock import patch

from inquiry_product import customer_management as customers
from inquiry_product import manual_sync as sync
from inquiry_product.web_demo import ACCOUNT_ID, seed_workspace
from inquiry_product.workspace import Workspace


class CustomerManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config = {"id": "alpha", "name": "Test company", "mode": "customer", "industry": "logistics",
                  "required_fields": ["goods"], "rules": ["Verify before quoting"], "knowledge": []}
        self.config = config
        self.workspace = Workspace.init(self.root / "customer", "alpha", config, mode="customer")
        self.source = self.root / "synthetic.sqlite3"
        self.first = "447700900000@s.whatsapp.net"
        self.second = "447700900001@s.whatsapp.net"
        self.group = "120363000000@g.us"
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("CREATE TABLE chats (jid TEXT, name TEXT, last_message_time TIMESTAMP)")
        sync.configure_connection(self.workspace, self.source, "test-sales", [{"jid": self.first, "display_name": "Saved name"}])
        self.chat(self.first, "Source name")
        self.chat(self.second, "New buyer")
        self.chat(self.group, "Buyers group")

    def chat(self, jid, name, timestamp="2026-09-25T00:10:00Z"):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("INSERT INTO chats VALUES (?,?,?)", (jid, name, timestamp))

    def items(self, result):
        return {row["identifier"]: row for row in result["items"]}

    def selection(self, result, identifiers):
        return [row["id"] for row in result["items"] if row["identifier"] in identifiers]

    def config_bytes(self):
        return (self.workspace.root / sync.CONFIG_FILE).read_bytes()

    def test_status_does_not_open_source_and_scan_reads_metadata_without_messages_table(self):
        self.source.rename(self.source.with_suffix(".away"))
        result = customers.customer_status(self.workspace)
        self.assertTrue(result["configured"])
        self.assertEqual(result["selected_count"], 1)
        self.assertIsNone(result["scan_id"])
        self.source.with_suffix(".away").rename(self.source)
        before = self.source.read_bytes()
        scanned = customers.scan_customers(self.workspace)
        rows = self.items(scanned)
        self.assertEqual(set(rows), {self.first, self.second, self.group})
        self.assertEqual(rows[self.first]["display_name"], "Source name")
        self.assertEqual(rows[self.first]["status"], "active")
        self.assertTrue(rows[self.first]["selected"])
        self.assertEqual(rows[self.second]["status"], "new")
        self.assertFalse(rows[self.second]["selected"])
        self.assertEqual(rows[self.group]["kind"], "group")
        self.assertEqual(scanned["connection_state"], "unknown")
        self.assertNotIn(str(self.root), json.dumps(scanned))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(customers.customer_status(self.workspace)["scan_id"], scanned["scan_id"])
        store = self.workspace.open_store()
        try:
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        finally:
            store.close()

    def test_repeated_scan_sees_new_metadata_deduplicates_and_invalidates_old_snapshot(self):
        first = customers.scan_customers(self.workspace)
        self.chat(self.second, "New buyer", "2026-09-26T00:00:00Z")
        self.chat("447700900002@s.whatsapp.net", "Next buyer")
        second = customers.scan_customers(self.workspace)
        self.assertEqual(len(second["items"]), 4)
        self.assertNotEqual(first["scan_id"], second["scan_id"])
        self.assertEqual(self.items(first)[self.second]["id"], self.items(second)[self.second]["id"])
        self.assertTrue(self.items(second)[self.second]["last_message_at"].startswith("2026-09-26"))
        before = self.config_bytes()
        with self.assertRaises(customers.CustomerConflictError):
            customers.save_customers(self.workspace, first["scan_id"], self.selection(first, [self.first]))
        self.assertEqual(self.config_bytes(), before)

    def test_add_pause_resume_keeps_source_names_and_save_never_imports(self):
        first = customers.scan_customers(self.workspace)
        result = customers.save_customers(self.workspace, first["scan_id"], self.selection(first, [self.second]))
        self.assertEqual((result["selected_count"], result["managed_count"]), (1, 2))
        self.assertEqual(self.items(result)[self.first]["status"], "paused")
        self.assertEqual(self.items(result)[self.second]["status"], "active")
        self.assertEqual(self.items(result)[self.group]["status"], "new")
        config = json.loads(self.config_bytes())
        self.assertEqual({row["jid"] for row in config["conversations"]}, {self.first, self.second})
        second = customers.scan_customers(self.workspace)
        self.assertFalse(self.items(second)[self.first]["selected"])
        self.assertTrue(self.items(second)[self.second]["selected"])
        result = customers.save_customers(self.workspace, second["scan_id"], self.selection(second, [self.first]))
        self.assertEqual(self.items(result)[self.first]["display_name"], "Source name")
        self.assertEqual(self.items(result)[self.first]["status"], "active")
        self.assertEqual(self.items(result)[self.second]["status"], "paused")
        store = self.workspace.open_store()
        try:
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        finally:
            store.close()

    def test_save_unknown_duplicate_oversized_and_changed_scope_fail_without_partial_write(self):
        scanned = customers.scan_customers(self.workspace)
        selected = self.selection(scanned, [self.first])
        before = self.config_bytes()
        for bad in (selected + ["unissued"], selected + selected, "bad", [None]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                customers.save_customers(self.workspace, scanned["scan_id"], bad)
            self.assertEqual(self.config_bytes(), before)
        with patch.object(customers, "MAX_SELECTED", 1):
            with self.assertRaises(ValueError):
                customers.save_customers(self.workspace, scanned["scan_id"], self.selection(scanned, [self.first, self.second]))
        self.assertEqual(self.config_bytes(), before)
        sync.configure_connection(self.workspace, self.source, "test-sales", [{"jid": self.first, "display_name": "Renamed"}])
        with self.assertRaises(customers.CustomerConflictError):
            customers.save_customers(self.workspace, scanned["scan_id"], selected)
        self.assertIsNone(customers.customer_status(self.workspace)["scan_id"])

    def test_rescan_refreshes_names_without_saving_selection_or_importing(self):
        first = customers.scan_customers(self.workspace)
        before = json.loads(self.config_bytes())
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE chats SET name=? WHERE jid=?", ("Renamed in WhatsApp", self.first))
        second = customers.scan_customers(self.workspace)
        self.assertEqual(self.items(second)[self.first]["display_name"], "Renamed in WhatsApp")
        after = json.loads(self.config_bytes())
        self.assertEqual([(r["jid"], r.get("enabled", True)) for r in before["conversations"]],
                         [(r["jid"], r.get("enabled", True)) for r in after["conversations"]])
        self.assertEqual(after["conversations"][0]["display_name"], "Renamed in WhatsApp")
        label = sync.conversation_labels(Workspace.load(self.workspace.root))
        self.assertEqual(label[("whatsapp:bridge:test-sales", "whatsapp:" + self.first)]["title"], "Renamed in WhatsApp")
        with self.assertRaises(customers.CustomerConflictError):
            customers.save_customers(self.workspace, first["scan_id"], [])
        self.assertEqual(customers.save_customers(self.workspace, second["scan_id"], self.selection(second, [self.first]))["selected_count"], 1)

    def test_missing_managed_customer_remains_available_for_pause_or_keep(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("DELETE FROM chats WHERE jid=?", (self.first,))
        scanned = customers.scan_customers(self.workspace)
        row = self.items(scanned)[self.first]
        self.assertFalse(row["available"])
        self.assertTrue(row["selected"])
        saved = customers.save_customers(self.workspace, scanned["scan_id"], [])
        self.assertEqual(saved["selected_count"], 0)
        self.assertEqual(self.items(saved)[self.first]["status"], "paused")

    def test_scanned_snapshot_cannot_be_reused_after_save_or_on_another_workspace(self):
        scanned = customers.scan_customers(self.workspace)
        customers.save_customers(self.workspace, scanned["scan_id"], [])
        with self.assertRaises(customers.CustomerConflictError):
            customers.save_customers(self.workspace, scanned["scan_id"], [])
        another = Workspace.init(self.root / "another", "alpha", self.config, mode="customer")
        sync.configure_connection(another, self.source, "test-sales", [])
        with self.assertRaises(customers.CustomerConflictError):
            customers.save_customers(another, scanned["scan_id"], [])

    def test_candidate_limit_refuses_whole_scan_without_silent_truncation(self):
        before = self.config_bytes()
        with patch.object(customers, "MAX_CANDIDATES", 2):
            with self.assertRaisesRegex(ValueError, "上限"):
                customers.scan_customers(self.workspace)
        self.assertIsNone(customers.customer_status(self.workspace)["scan_id"])
        self.assertEqual(self.config_bytes(), before)

    def test_scan_failure_is_clear_and_invalidates_previous_candidate_snapshot(self):
        scanned = customers.scan_customers(self.workspace)
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, "桥接"):
            customers.scan_customers(self.workspace)
        with self.assertRaises(customers.CustomerConflictError):
            customers.save_customers(self.workspace, scanned["scan_id"], [])
        self.assertTrue(customers.customer_status(self.workspace)["configured"])

    def test_no_configuration_shows_setup_state_without_guessing_source(self):
        (self.workspace.root / sync.CONFIG_FILE).unlink()
        result = customers.customer_status(self.workspace)
        self.assertFalse(result["configured"])
        self.assertEqual(result["items"], [])
        with self.assertRaisesRegex(ValueError, "尚未配置"):
            customers.scan_customers(self.workspace)
        with self.assertRaisesRegex(ValueError, "尚未配置"):
            customers.save_customers(self.workspace, "anything", [])

    def test_scan_and_save_share_update_lock(self):
        scanned = customers.scan_customers(self.workspace)
        with sync._sync_lock(self.workspace):
            with self.assertRaises(sync.SyncBusyError):
                customers.scan_customers(self.workspace)
            with self.assertRaises(sync.SyncBusyError):
                customers.save_customers(self.workspace, scanned["scan_id"], [])

    def test_scan_expires_without_mutating_saved_selection(self):
        scanned = customers.scan_customers(self.workspace)
        path = self.workspace.root / customers.SCAN_FILE
        cache = json.loads(path.read_text())
        cache["scanned_at"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(cache))
        before = self.config_bytes()
        with self.assertRaises(customers.CustomerConflictError):
            customers.save_customers(self.workspace, scanned["scan_id"], [])
        self.assertEqual(before, self.config_bytes())
        self.assertIsNone(customers.customer_status(self.workspace)["scan_id"])

    def test_corrupt_cached_metadata_fails_closed_without_writing_selection(self):
        scanned = customers.scan_customers(self.workspace)
        path = self.workspace.root / customers.SCAN_FILE
        cache = json.loads(path.read_text())
        cache["items"] = [{"identifier": self.second, "available": True}]
        path.write_text(json.dumps(cache))
        before = self.config_bytes()
        with self.assertRaises(ValueError):
            customers.save_customers(self.workspace, scanned["scan_id"], [])
        self.assertEqual(before, self.config_bytes())
        self.assertFalse(customers.customer_status(self.workspace)["configured"])

    def test_source_schema_problem_rejects_instead_of_querying_messages(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("DROP TABLE chats")
        with self.assertRaisesRegex(ValueError, "结构"):
            customers.scan_customers(self.workspace)

    def test_failed_scan_invalidation_write_also_revokes_previous_token(self):
        scanned = customers.scan_customers(self.workspace)
        before = self.config_bytes()
        with patch.object(customers, "_atomic_write", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises((OSError, ValueError)):
                customers.scan_customers(self.workspace)
        with self.assertRaises(customers.CustomerConflictError):
            customers.save_customers(self.workspace, scanned["scan_id"], [])
        self.assertEqual(before, self.config_bytes())
        rescanned = customers.scan_customers(self.workspace)
        self.assertEqual(customers.save_customers(self.workspace, rescanned["scan_id"], [])["selected_count"], 0)

    def test_unselected_scan_metadata_does_not_enter_workspace_backup(self):
        customers.scan_customers(self.workspace)
        archive = self.root / "customer-backup.zip"
        self.workspace.backup(archive)
        with zipfile.ZipFile(archive) as backup:
            for name in backup.namelist():
                payload = backup.read(name)
                self.assertNotIn(self.second.encode(), payload, name)
                self.assertNotIn(b"New buyer", payload, name)

    def test_new_process_rejects_pre_restart_scan_token(self):
        scanned = customers.scan_customers(self.workspace)
        before = self.config_bytes()
        code = """import sys
from inquiry_product.customer_management import save_customers, CustomerConflictError
from inquiry_product.workspace import Workspace
try:
    save_customers(Workspace.load(sys.argv[1]), sys.argv[2], [])
except CustomerConflictError:
    print('rescan-required')
else:
    print('unexpected-replay')
"""
        completed = subprocess.run([sys.executable, "-B", "-c", code, str(self.workspace.root), scanned["scan_id"]],
                                   capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "rescan-required")
        self.assertEqual(before, self.config_bytes())

    def test_demo_save_does_not_depend_on_fallible_reads_after_commit(self):
        demo = seed_workspace(self.root / "demo")
        scanned = customers.scan_customers(demo)
        written = False
        actual_write, actual_read = customers._atomic_write, sync._read_json

        def write(path, content):
            nonlocal written
            actual_write(path, content)
            written = True

        def read(*args, **kwargs):
            if written:
                raise OSError("synthetic post-commit read failure")
            return actual_read(*args, **kwargs)

        with patch.object(customers, "_atomic_write", side_effect=write), patch.object(sync, "_read_json", side_effect=read):
            result = customers.save_customers(demo, scanned["scan_id"], [])
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(customers.customer_status(demo)["selected_count"], 0)

    def test_atomic_write_failure_does_not_claim_nothing_was_saved(self):
        scanned = customers.scan_customers(self.workspace)
        actual = customers._atomic_write

        def fail_after_replace(path, content):
            actual(path, content)
            raise OSError("synthetic post-replace durability failure")

        with patch.object(customers, "_atomic_write", side_effect=fail_after_replace):
            with self.assertRaisesRegex(ValueError, "核对"):
                customers.save_customers(self.workspace, scanned["scan_id"], [])
        self.assertEqual(customers.customer_status(self.workspace)["selected_count"], 0)

    def test_demo_has_persistent_repeatable_selection_and_new_customer_imports_only_on_update(self):
        demo = seed_workspace(self.root / "demo")
        before = customers.customer_status(demo)
        self.assertEqual(before["source_type"], "demo")
        self.assertEqual(before["selected_count"], 5)
        scanned = customers.scan_customers(demo)
        fresh = [row for row in scanned["items"] if row["status"] == "new"]
        self.assertTrue(fresh)
        added = fresh[0]
        saved = customers.save_customers(demo, scanned["scan_id"], [added["id"]])
        self.assertEqual((saved["selected_count"], saved["managed_count"]), (1, 6))
        store = demo.open_store()
        try:
            self.assertEqual(len(store.messages(demo.company_id, ACCOUNT_ID, added["identifier"])), 0)
        finally:
            store.close()
        result = sync.refresh_messages(demo)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["affected_conversations"], [{"account_id": ACCOUNT_ID, "conversation_id": added["identifier"]}])
        self.assertEqual(sync.refresh_messages(demo)["inserted"], 0)
        rescanned = customers.scan_customers(demo)
        self.assertEqual([row["identifier"] for row in rescanned["items"] if row["selected"]], [added["identifier"]])
        self.assertEqual(sync.sync_status(demo)["paused_conversations"], 5)
        self.assertEqual(sync.conversation_labels(demo)[(ACCOUNT_ID, added["identifier"])]["title"], added["display_name"])
        customers.save_customers(demo, rescanned["scan_id"], [])
        self.assertEqual(sync.refresh_messages(demo)["inserted"], 0)


if __name__ == "__main__":
    unittest.main()
