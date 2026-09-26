"""Synthetic-only manual update, source isolation and failure accounting."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from inquiry_product import manual_sync as sync
from inquiry_product.web_demo import ACCOUNT_ID, seed_workspace
from inquiry_product.workspace import Workspace


class ManualSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config = {"id": "alpha", "name": "Test company", "mode": "customer", "industry": "logistics",
                  "required_fields": ["goods"], "rules": ["Verify before quoting"], "knowledge": []}
        self.workspace = Workspace.init(self.root / "customer", "alpha", config, mode="customer")
        self.source = self.root / "synthetic-bridge.sqlite3"
        self.jid = "447700900000@s.whatsapp.net"
        self.other_jid = "447700900001@s.whatsapp.net"
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute("CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN, media_type TEXT, filename TEXT, PRIMARY KEY(id,chat_jid))")
        self.scope = [{"jid": self.jid, "display_name": "Synthetic buyer"}]

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, **changes):
        args = {"messages_db": self.source, "source_account_id": "sales-01", "conversations": self.scope}
        args.update(changes)
        return sync.configure_connection(self.workspace, **args)

    def row(self, identity="m1", **changes):
        record = {"id": identity, "chat_jid": self.jid, "sender": "synthetic-sender", "content": "Sensitive synthetic text",
                  "timestamp": "2026-09-25 00:10:00+00:00", "is_from_me": 0, "media_type": "", "filename": ""}
        record.update(changes)
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute("INSERT OR REPLACE INTO messages VALUES (:id,:chat_jid,:sender,:content,:timestamp,:is_from_me,:media_type,:filename)", record)
        return record

    def messages(self, workspace=None):
        store = (workspace or self.workspace).open_store()
        try:
            return [dict(row) for row in store.connection.execute("SELECT * FROM messages ORDER BY sent_at,message_id")]
        finally:
            store.close()

    def test_unconfigured_non_demo_does_not_probe_a_source(self):
        status = sync.sync_status(self.workspace)
        self.assertFalse(status["configured"])
        self.assertEqual(status["source_type"], "none")
        with self.assertRaisesRegex(ValueError, "尚未配置"):
            sync.refresh_messages(self.workspace)
        self.assertEqual(self.messages(), [])

    def test_configure_does_not_open_source_or_claim_login(self):
        with patch.object(sync.sqlite3, "connect", wraps=sqlite3.connect) as opened:
            self.configure(messages_db=self.root / "not-started.db")
        self.assertFalse(any("not-started.db" in str(call) for call in opened.call_args_list))
        result = sync.sync_status(self.workspace)
        self.assertTrue(result["configured"])
        self.assertEqual(result["connection_state"], "unknown")
        self.assertFalse(result["history_complete"])
        self.assertIsNone(result["last_import_success"])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_scope_dedup_and_late_arrival_without_timestamp_watermark(self):
        self.configure()
        self.row()
        self.row("private-unapproved", chat_jid=self.other_jid)
        first = sync.refresh_messages(self.workspace)
        second = sync.refresh_messages(self.workspace)
        self.row("late-history", timestamp="2026-09-20 00:00:00+00:00")
        third = sync.refresh_messages(self.workspace)
        self.assertEqual((first["inserted"], second["inserted"], second["duplicates"], third["inserted"]), (1, 0, 1, 1))
        self.assertEqual({row["message_id"] for row in self.messages()}, {"whatsapp:m1", "whatsapp:late-history"})
        self.assertTrue(third["coverage"]["late_arrivals_checked"])
        self.assertFalse(third["coverage"]["history_complete"])
        self.assertEqual(third["coverage"]["source_first_at"], "2026-09-20T00:00:00.000000+00:00")
        self.assertEqual(third["coverage"]["source_last_at"], "2026-09-25T00:10:00.000000+00:00")
        self.assertEqual(first["affected_conversations"], [{"account_id": "whatsapp:bridge:sales-01", "conversation_id": "whatsapp:" + self.jid}])
        self.assertEqual(second["affected_conversations"], [])
        self.assertEqual(sync.conversation_labels(self.workspace)[("whatsapp:bridge:sales-01", "whatsapp:" + self.jid)]["title"], "Synthetic buyer")

    def test_paused_customer_keeps_history_and_is_not_read_on_refresh(self):
        self.configure()
        self.row()
        sync.refresh_messages(self.workspace)
        self.configure(conversations=[{"jid": self.jid, "display_name": "Synthetic buyer", "enabled": False}])
        self.row("new-paused")
        result = sync.refresh_messages(self.workspace)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(sync.sync_status(self.workspace)["active_conversations"], 0)
        self.assertEqual(sync.sync_status(self.workspace)["paused_conversations"], 1)
        self.assertIn("暂停", sync.conversation_labels(self.workspace)[("whatsapp:bridge:sales-01", "whatsapp:" + self.jid)]["subtitle"])

    def test_empty_active_scope_can_be_configured_and_does_not_open_source(self):
        self.configure(conversations=[])
        self.source.unlink()
        result = sync.refresh_messages(self.workspace)
        self.assertEqual((result["status"], result["inserted"]), ("succeeded", 0))
        self.assertEqual(result["coverage"]["authorized_conversations"], 0)
        self.assertIn("选择", result["note"])

    def test_enabled_must_be_boolean_and_active_limit_counts_enabled_only(self):
        with self.assertRaises(ValueError):
            self.configure(conversations=[{"jid": self.jid, "display_name": "Buyer", "enabled": "false"}])
        scope = [{"jid": f"4477009{i:05d}@s.whatsapp.net", "display_name": f"Buyer {i}", "enabled": False} for i in range(101)]
        self.configure(conversations=scope)
        self.assertEqual(sync.sync_status(self.workspace)["paused_conversations"], 101)
        for item in scope:
            item["enabled"] = True
        with self.assertRaises(ValueError):
            self.configure(conversations=scope)

    def test_same_message_id_in_two_authorized_chats_is_not_a_duplicate(self):
        self.configure(conversations=self.scope + [{"jid": self.other_jid, "display_name": "Second buyer"}])
        self.row()
        self.row(chat_jid=self.other_jid, content="Separate customer message")
        result = sync.refresh_messages(self.workspace)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(len(result["affected_conversations"]), 2)

    def test_update_refreshes_contact_name_without_new_messages_or_identity_change(self):
        self.configure()
        self.row()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP)")
            db.execute("INSERT INTO chats VALUES (?,?,?)", (self.jid, "First WhatsApp name", "2026-09-25T00:00:00Z"))
        first = sync.refresh_messages(self.workspace)
        messages_before = self.messages()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE chats SET name=? WHERE jid=?", ("Updated WhatsApp name", self.jid))
        second = sync.refresh_messages(self.workspace)
        self.assertEqual((first["inserted"], second["inserted"], second["duplicates"]), (1, 0, 1))
        self.assertEqual(self.messages(), messages_before)
        self.assertEqual(sync.conversation_labels(self.workspace)[("whatsapp:bridge:sales-01", "whatsapp:" + self.jid)]["title"], "Updated WhatsApp name")
        self.assertEqual(sync.sync_status(self.workspace)["active_conversations"], 1)

    def test_source_is_readonly_and_a_consistent_transaction(self):
        self.configure()
        self.row()
        original = self.source.read_bytes()
        actual = sqlite3.connect
        source_connections = []
        traces = []

        def connect(*args, **kwargs):
            connection = actual(*args, **kwargs)
            if args and str(args[0]).startswith(self.source.as_uri()):
                self.assertIn("mode=ro", args[0])
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM messages")
                connection.rollback()
                connection.set_trace_callback(traces.append)
                source_connections.append(connection)
            return connection

        with patch.object(sync.sqlite3, "connect", side_effect=connect):
            result = sync.refresh_messages(self.workspace)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(len(source_connections), 1)
        self.assertIn("BEGIN", traces)
        self.assertIn("ROLLBACK", traces)
        self.assertIn("PRAGMA query_only=ON", traces)

    def test_simulation_cannot_read_real_config_even_if_manually_written(self):
        demo = seed_workspace(self.root / "demo")
        with self.assertRaisesRegex(ValueError, "customer"):
            sync.configure_connection(demo, self.source, "sales-01", self.scope)
        self.configure()
        folder = demo.root / "connections"
        folder.mkdir()
        (folder / "whatsapp.json").write_bytes((self.workspace.root / sync.CONFIG_FILE).read_bytes())
        with patch.object(sync, "_bridge_snapshot", side_effect=AssertionError("must not read source")):
            status = sync.sync_status(demo)
            self.assertFalse(status["configured"])
            with self.assertRaises(ValueError):
                sync.refresh_messages(demo)
        self.assertEqual(len(self.messages(demo)), 15)

    def test_demo_manual_update_is_fixed_synthetic_idempotent_and_stales_draft(self):
        demo = seed_workspace(self.root / "demo")
        with patch.object(sync, "_bridge_snapshot", side_effect=AssertionError("no real source")):
            self.assertEqual(sync.sync_status(demo)["source_type"], "demo")
            result = sync.refresh_messages(demo)
            repeat = sync.refresh_messages(demo)
        self.assertEqual((result["inserted"], repeat["inserted"], repeat["duplicates"]), (2, 0, 2))
        self.assertEqual(len(self.messages(demo)), 17)
        self.assertIn("虚构", result["note"])
        self.assertTrue(all(row["mode"] == "simulation" for row in self.messages(demo)))
        store = demo.open_store()
        try:
            row = store.connection.execute("SELECT status FROM drafts WHERE conversation_id='workbench:details-needed'").fetchone()
            self.assertEqual(row["status"], "stale")
        finally:
            store.close()
        self.assertEqual(len(sync.conversation_labels(demo)), 5)
        self.assertFalse(sync.sync_status(demo)["history_complete"])

    def test_ordinary_simulation_does_not_automatically_get_demo_messages(self):
        config = {"id": "sim", "name": "Other demo", "mode": "simulation", "industry": "logistics",
                  "required_fields": ["goods"], "rules": ["Synthetic"], "knowledge": []}
        workspace = Workspace.init(self.root / "other-demo", "sim", config)
        self.assertEqual(sync.sync_status(workspace)["source_type"], "none")
        with self.assertRaises(ValueError):
            sync.refresh_messages(workspace)

    def test_changed_and_missing_source_records_are_conflicts_not_overwrites(self):
        self.configure()
        self.row()
        self.row("vanishes")
        sync.refresh_messages(self.workspace)
        last_success = sync.sync_status(self.workspace)["last_import_success"]
        self.row(content="Changed upstream message")
        self.row("normal-new", content="Another legitimate message")
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute("DELETE FROM messages WHERE id='vanishes'")
        result = sync.refresh_messages(self.workspace)
        self.assertEqual((result["status"], result["inserted"], result["conflicts"]), ("partial", 1, 2))
        self.assertEqual(result["coverage"]["missing_previous_records"], 1)
        self.assertEqual(sync.sync_status(self.workspace)["last_import_success"], last_success)
        messages = {row["message_id"]: row["body"] for row in self.messages()}
        self.assertEqual(messages["whatsapp:m1"], "Sensitive synthetic text")
        self.assertIn("whatsapp:vanishes", messages)
        self.assertIn("whatsapp:normal-new", messages)
        report = json.dumps(sync.sync_status(self.workspace))
        self.assertNotIn("Sensitive synthetic text", report)
        self.assertNotIn(str(self.source), report)

    def test_sender_change_is_detected_even_when_display_body_is_unchanged(self):
        self.configure()
        self.row()
        sync.refresh_messages(self.workspace)
        self.row(sender="another-sender")
        result = sync.refresh_messages(self.workspace)
        self.assertEqual((result["status"], result["conflicts"]), ("partial", 1))

    def test_attachments_are_marked_not_parsed_and_outbound_is_retained(self):
        self.configure()
        self.row("image", media_type="image", content="Carton dimensions shown here", filename="file.jpg")
        self.row("audio", media_type="audio", content="")
        self.row("outbound", is_from_me=1, content="Please confirm the packing details")
        result = sync.refresh_messages(self.workspace)
        self.assertEqual(result["inserted"], 3)
        records = {row["message_id"]: row for row in self.messages()}
        self.assertIn("附件尚未解析", records["whatsapp:image"]["body"])
        self.assertIn("原消息附文", records["whatsapp:image"]["body"])
        self.assertNotIn("file.jpg", records["whatsapp:image"]["body"])
        self.assertEqual(records["whatsapp:outbound"]["direction"], "outbound")
        self.row("image", media_type="image", content="Carton dimensions shown here", filename="regenerated.jpg")
        self.assertEqual(sync.refresh_messages(self.workspace)["conflicts"], 0)

    def test_empty_or_invalid_records_skip_only_bad_rows(self):
        self.configure()
        self.row("blank", content="")
        self.row("naive-time", timestamp="2026-09-25 00:10:00")
        self.row("no-time", timestamp=None)
        self.row("bad-direction", is_from_me=4)
        self.row("good")
        result = sync.refresh_messages(self.workspace)
        self.assertEqual((result["status"], result["inserted"], result["conflicts"]), ("partial", 1, 4))

    def test_empty_snapshot_and_failed_read_have_distinct_status(self):
        self.configure()
        empty = sync.refresh_messages(self.workspace)
        self.assertEqual((empty["status"], empty["inserted"]), ("succeeded", 0))
        self.assertTrue(empty["coverage"]["snapshot_complete"])
        self.assertFalse(empty["coverage"]["history_complete"])
        self.assertIsNone(empty["coverage"]["source_first_at"])
        self.assertIsNone(empty["coverage"]["source_last_at"])
        previous = sync.sync_status(self.workspace)["last_import_success"]
        self.source.unlink()
        failed = sync.refresh_messages(self.workspace)
        self.assertEqual((failed["status"], failed["inserted"]), ("partial", 0))
        self.assertEqual(failed["error_code"], "source_unavailable")
        self.assertFalse(failed["coverage"]["snapshot_complete"])
        self.assertIsNone(failed["coverage"]["source_first_at"])
        self.assertIsNone(failed["coverage"]["source_last_at"])
        self.assertEqual(sync.sync_status(self.workspace)["last_import_success"], previous)
        self.assertFalse(self.source.exists())

    def test_coverage_normalizes_offsets_and_failure_never_reuses_previous_range(self):
        self.configure()
        self.row("earlier", timestamp="2026-09-25T08:00:00+08:00")
        self.row("later", timestamp="2026-09-25T00:10:00Z")
        self.row("unapproved", chat_jid=self.other_jid, timestamp="2020-01-01T00:00:00Z")
        result = sync.refresh_messages(self.workspace)
        self.assertEqual(result["coverage"]["source_first_at"], "2026-09-25T00:00:00.000000+00:00")
        self.assertEqual(result["coverage"]["source_last_at"], "2026-09-25T00:10:00.000000+00:00")
        self.source.unlink()
        failed = sync.refresh_messages(self.workspace)
        self.assertIsNone(failed["coverage"]["source_first_at"])
        self.assertIsNone(failed["coverage"]["source_last_at"])
        self.assertIsNone(sync.sync_status(self.workspace)["last_result"]["coverage"]["source_first_at"])

    def test_corrupt_state_fails_closed_in_display_without_opening_source(self):
        self.configure()
        self.row()
        sync.refresh_messages(self.workspace)
        path = self.workspace.root / sync.STATE_FILE
        state = json.loads(path.read_text())
        state["accepted"] = {"not-json": "no-hash"}
        path.write_text(json.dumps(state))
        with patch.object(sync, "_bridge_snapshot", side_effect=AssertionError("must not open source")):
            self.assertFalse(sync.sync_status(self.workspace)["configured"])
            with self.assertRaises(ValueError):
                sync.refresh_messages(self.workspace)

    def test_source_snapshot_excludes_records_committed_during_scan(self):
        self.configure()
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        self.row("before")
        actual = sqlite3.connect
        written = False

        def connect(*args, **kwargs):
            connection = actual(*args, **kwargs)
            if args and str(args[0]).startswith(self.source.as_uri()):
                def trace(sql):
                    nonlocal written
                    if sql.startswith("SELECT id,chat_jid") and not written:
                        written = True
                        with closing(actual(self.source)) as writer, writer:
                            writer.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
                                           ("during", self.jid, "synthetic", "Later commit", "2026-09-25T00:11:00Z", 0, "", ""))
                connection.set_trace_callback(trace)
            return connection

        with patch.object(sync.sqlite3, "connect", side_effect=connect):
            result = sync.refresh_messages(self.workspace)
        self.assertTrue(written)
        self.assertEqual((result["inserted"], result["coverage"]["source_rows"]), (1, 1))
        next_result = sync.refresh_messages(self.workspace)
        self.assertEqual((next_result["inserted"], next_result["duplicates"]), (1, 1))

    def test_unsupported_schema_returns_partial_and_keeps_display_available(self):
        self.configure()
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute("DROP TABLE messages")
        result = sync.refresh_messages(self.workspace)
        self.assertEqual((result["status"], result["inserted"], result["error_code"]),
                         ("partial", 0, "schema_unsupported"))
        self.assertTrue(sync.sync_status(self.workspace)["configured"])
        self.assertEqual(len(sync.conversation_labels(self.workspace)), 1)

    def test_scan_limit_does_not_silently_truncate_or_import(self):
        self.configure()
        self.row("one")
        self.row("two")
        with patch.object(sync, "MAX_ROWS", 1):
            result = sync.refresh_messages(self.workspace)
        self.assertEqual(result["error_code"], "scan_limit_exceeded")
        self.assertEqual(result["coverage"]["source_rows"], 2)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(self.messages(), [])

    def test_content_budget_is_checked_before_fetching_message_bodies(self):
        self.configure()
        self.row(content="Long synthetic content")
        with patch.object(sync, "MAX_CONTENT_CHARS", 1):
            result = sync.refresh_messages(self.workspace)
        self.assertEqual((result["error_code"], result["inserted"]), ("scan_limit_exceeded", 0))

    def test_second_batch_failure_reports_committed_counts_and_retry_dedups(self):
        self.configure()
        self.row("a")
        self.row("b")
        actual = Workspace.import_messages
        calls = 0

        def fail_second(workspace, records, label):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("synthetic disk error")
            return actual(workspace, records, label)

        with patch.object(sync, "BATCH_SIZE", 1), patch.object(Workspace, "import_messages", fail_second):
            result = sync.refresh_messages(self.workspace)
        self.assertEqual((result["status"], result["inserted"]), ("partial", 1))
        self.assertEqual(len(self.messages()), 1)
        self.assertIsNone(sync.sync_status(self.workspace)["last_import_success"])
        retry = sync.refresh_messages(self.workspace)
        self.assertEqual((retry["status"], retry["inserted"], retry["duplicates"]), ("succeeded", 1, 1))

    def test_concurrent_manual_update_is_rejected_and_first_finishes(self):
        self.configure()
        self.row()
        entered, release = threading.Event(), threading.Event()
        actual = sync._bridge_snapshot
        results = []

        def pause(config):
            entered.set()
            self.assertTrue(release.wait(5))
            return actual(config)

        def run():
            try:
                results.append(sync.refresh_messages(self.workspace))
            except BaseException as exc:
                results.append(exc)

        with patch.object(sync, "_bridge_snapshot", side_effect=pause):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(sync.SyncBusyError):
                    sync.refresh_messages(self.workspace)
                with self.assertRaises(sync.SyncBusyError):
                    self.configure()
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(results[0]["inserted"], 1)

    def test_configuration_rejects_cross_workspace_copy_source_rebind_and_symlinks(self):
        self.configure()
        with self.assertRaisesRegex(ValueError, "不能原地替换"):
            self.configure(source_account_id="another-account")
        path = self.workspace.root / sync.CONFIG_FILE
        value = json.loads(path.read_text())
        value["company_id"] = "other"
        path.write_text(json.dumps(value))
        self.assertFalse(sync.sync_status(self.workspace)["configured"])
        with self.assertRaises(ValueError):
            sync.refresh_messages(self.workspace)
        path.unlink()
        path.symlink_to(self.source)
        self.assertFalse(sync.sync_status(self.workspace)["configured"])
        with self.assertRaises(ValueError):
            sync.refresh_messages(self.workspace)

    def test_cli_stores_only_explicit_scope_and_does_not_read_messages(self):
        scope_file = self.root / "allowed.json"
        scope_file.write_text(json.dumps(self.scope))
        product = Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, "-B", str(product / "configure_whatsapp.py"),
                                 "--workspace", str(self.workspace.root), "--messages-db", str(self.source),
                                 "--source-account-id", "sales-01", "--conversations", str(scope_file)],
                                capture_output=True, text=True, cwd=product)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["connection_state"], "unknown")
        self.assertEqual(self.messages(), [])
        self.assertNotIn(str(self.source), result.stdout)
        value = json.loads((self.workspace.root / sync.CONFIG_FILE).read_text())
        self.assertEqual(value["conversations"], self.scope)


if __name__ == "__main__":
    unittest.main()
