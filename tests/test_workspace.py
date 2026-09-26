"""Synthetic workspace, failure recovery and untrusted backup acceptance tests."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
import warnings
import zipfile

from inquiry_product.workspace import Workspace


class OfflineRunner:
    name = "offline_workspace_test"

    def __init__(self, hook=None):
        self.hook = hook

    def analyze(self, context):
        if self.hook:
            self.hook()
        return {"summary": "离线流程测试", "facts": [],
                "missing_fields": context["required_fields"], "uncertainties": ["人工核实"],
                "next_action": "人工核实", "reply": "请补充数量。", "needs_human": True,
                "citations": [{"id": item["id"], "version": item["version"]}
                              for item in context["knowledge"]]}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = {"id": "alpha", "mode": "simulation", "name": "Alpha 演练企业",
                       "industry": "test", "required_fields": ["quantity"], "rules": ["需人工审核"],
                       "knowledge": [{"id": "K1", "version": "1", "title": "测试资料",
                                      "content": "SENSITIVE_KNOWLEDGE", "valid_from": "2026-01-01",
                                      "valid_until": "2027-01-01"}]}
        self.workspace = Workspace.init(self.root / "workspace", "alpha", self.config)

    def tearDown(self):
        self.temporary.cleanup()

    def message(self, **updates):
        result = {"project_id": "alpha", "account_id": "test-account", "conversation_id": "chat",
                  "message_id": "m1", "direction": "inbound", "body": "SENSITIVE_MESSAGE",
                  "sent_at": "2026-09-25T08:00:00Z", "received_at": "2026-09-25T08:01:00Z",
                  "mode": "simulation"}
        result.update(updates)
        return result

    def query(self, sql, parameters=()):
        store = self.workspace.open_store()
        try:
            return [tuple(row) for row in store.connection.execute(sql, parameters)]
        finally:
            store.close()

    def changed_config(self):
        changed = deepcopy(self.config)
        changed["knowledge"][0]["version"] = "2"
        changed["knowledge"][0]["content"] = "Changed test knowledge"
        return changed

    def backup(self):
        path = self.root / "backup.zip"
        self.workspace.backup(path)
        return path

    def mutate_archive(self, mutate):
        original = self.backup()
        with zipfile.ZipFile(original) as bundle:
            contents = {info.filename: bundle.read(info.filename) for info in bundle.infolist()}
        mutate(contents)
        path = self.root / "modified.zip"
        with zipfile.ZipFile(path, "w") as bundle:
            for name, content in contents.items():
                bundle.writestr(name, content)
        return path

    def assert_restore_rejected(self, archive):
        destination = self.root / "rejected"
        with self.assertRaises(ValueError):
            Workspace.restore(archive, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".workspace-restore-*")))

    def test_init_binds_configuration_and_has_restrictive_permissions(self):
        loaded = Workspace.load(self.workspace.root)
        self.assertEqual((loaded.company_id, loaded.mode), ("alpha", "simulation"))
        self.assertEqual(stat.S_IMODE(loaded.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(loaded.db_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((loaded.root / "workspace.json").stat().st_mode), 0o600)
        for directory in loaded.root.rglob("*"):
            if directory.is_dir():
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(loaded.configs_dir.name, loaded.manifest["active_release"])
        self.assertEqual(hashlib.sha256((loaded.configs_dir / "alpha.json").read_bytes()).hexdigest(),
                         loaded.manifest["active_release"])
        self.assertEqual(self.query("SELECT value FROM metadata WHERE key='company_id'"), [("alpha",)])

    def test_init_does_not_overwrite_and_accepts_existing_empty_directory(self):
        before = (self.workspace.root / "workspace.json").read_bytes()
        with self.assertRaises(ValueError):
            Workspace.init(self.workspace.root, "alpha", self.config)
        self.assertEqual((self.workspace.root / "workspace.json").read_bytes(), before)
        target = self.root / "empty"
        target.mkdir()
        self.assertEqual(Workspace.init(target, "alpha", self.config).company_id, "alpha")

    def test_init_rejects_wrong_company_mode_and_fake_customer_name(self):
        for company, mode, config in [("other", "simulation", self.config),
                                      ("alpha", "customer", self.config),
                                      ("../escape", "simulation", self.config)]:
            with self.subTest(company=company, mode=mode), self.assertRaises(ValueError):
                Workspace.init(self.root / "bad", company, config, mode)
        config = dict(self.config, mode="customer")
        with self.assertRaises(ValueError):
            Workspace.init(self.root / "bad", "alpha", config, "customer")
        self.assertFalse((self.root / "bad").exists())

    def test_customer_mode_uses_explicitly_customer_config_and_messages(self):
        config = dict(self.config, mode="customer", name="Authorized Customer Fixture")
        workspace = Workspace.init(self.root / "customer", "alpha", config, "customer")
        result = workspace.import_messages([self.message(mode="customer")], "offline_authorized_fixture")
        self.assertEqual(result["inserted"], 1)
        with self.assertRaises(ValueError):
            workspace.import_messages([self.message(message_id="m2")], "wrong-mode")
        self.assertEqual(workspace.health()["mode"], "customer")

    def test_two_workspaces_isolate_same_external_message_identity(self):
        config = dict(self.config, id="beta")
        second = Workspace.init(self.root / "second", "beta", config)
        self.workspace.import_messages([self.message()], "fixture")
        second.import_messages([self.message(project_id="beta", body="OTHER_COMPANY")], "fixture")
        self.assertEqual(self.query("SELECT body FROM messages"), [("SENSITIVE_MESSAGE",)])
        service = self.workspace.service(OfflineRunner())
        try:
            with self.assertRaises(ValueError):
                service.project("beta")
            with self.assertRaises(ValueError):
                service.add_message(self.message(project_id="beta"))
        finally:
            service.store.close()

    def test_import_validates_all_records_before_writing(self):
        for bad in (self.message(project_id="beta"), self.message(mode="customer"),
                    self.message(body=""), {key: value for key, value in self.message().items() if key != "mode"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.workspace.import_messages([self.message(), bad], "fixture")
        self.assertEqual(self.query("SELECT count(*) FROM messages"), [(0,)])
        self.assertEqual(self.query("SELECT count(*) FROM import_batches"), [(0,)])

    def test_import_batch_conflict_rolls_back_and_audit_is_atomic(self):
        with self.assertRaises(ValueError):
            self.workspace.import_messages([self.message(), self.message(body="conflict")], "fixture")
        self.workspace.import_messages([self.message()], "existing")
        with self.assertRaises(ValueError):
            self.workspace.import_messages([self.message(message_id="new"), self.message(body="conflict")], "fixture")
        self.assertEqual(self.query("SELECT message_id FROM messages"), [("m1",)])
        self.assertEqual(self.query("SELECT count(*) FROM import_batches"), [(1,)])
        store = self.workspace.open_store()
        try:
            store.connection.execute("""CREATE TRIGGER fail_import_audit BEFORE INSERT ON import_batches
                                     BEGIN SELECT RAISE(ABORT, 'audit rejected'); END""")
        finally:
            store.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.workspace.import_messages([self.message(message_id="another-new")], "audit_failure")
        self.assertEqual(self.query("SELECT message_id FROM messages"), [("m1",)])

    def test_import_replay_is_idempotent_with_stable_receipt_independent_digest(self):
        first = self.workspace.import_messages([self.message()], "fixture")
        second = self.workspace.import_messages([self.message(received_at="2026-09-25T09:00:00Z")], "fixture")
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(first["imported_at"], second["imported_at"])
        self.assertTrue(second["replayed"])
        self.assertEqual(self.query("SELECT record_count,inserted,duplicates FROM import_batches"), [(1, 1, 0)])
        self.assertEqual(self.query("SELECT received_at FROM messages"), [("2026-09-25T08:01:00Z",)])

    def test_import_source_label_cannot_be_a_file_path(self):
        for label in ("", "/private/messages.json", "C:\\secret.json", "label\nextra"):
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.workspace.import_messages([self.message()], label)

    def test_publish_and_rollback_preserve_all_release_history(self):
        first = self.workspace.manifest["active_release"]
        result = self.workspace.publish(self.changed_config())
        second = result["release_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(result["previous_release"], first)
        self.assertEqual(Workspace.load(self.workspace.root).configs_dir.name, second)
        self.assertFalse(self.workspace.publish(self.changed_config())["changed"])
        self.workspace.rollback(first)
        loaded = Workspace.load(self.workspace.root)
        self.assertEqual(loaded.manifest["active_release"], first)
        self.assertEqual([event["action"] for event in loaded.manifest["knowledge_history"]],
                         ["init", "publish", "rollback"])
        self.assertEqual(len(list((self.workspace.root / "knowledge/releases").iterdir())), 2)

    def test_publish_failure_keeps_previous_release_loadable(self):
        first = self.workspace.manifest["active_release"]
        with patch("inquiry_product.workspace.os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                self.workspace.publish(self.changed_config())
        self.assertEqual(Workspace.load(self.workspace.root).manifest["active_release"], first)
        self.assertTrue(self.workspace.publish(self.changed_config())["changed"])

    def test_partial_release_preparation_does_not_poison_workspace(self):
        with patch("inquiry_product.workspace._exclusive_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.workspace.publish(self.changed_config())
        self.assertTrue(self.workspace.health()["ok"])
        self.assertFalse(list((self.workspace.root / "knowledge").glob(".release-*")))

    def test_rollback_rejects_paths_and_unknown_releases(self):
        for release in ("../workspace.json", "f" * 64, "ABC" * 22):
            with self.subTest(release=release), self.assertRaises(ValueError):
                self.workspace.rollback(release)

    def test_service_observes_publication_during_analysis_and_before_review(self):
        self.workspace.import_messages([self.message()], "fixture")
        service = self.workspace.service(OfflineRunner())
        try:
            draft = service.analyze("alpha", "test-account", "chat", "2026-09-25")
            self.workspace.publish(self.changed_config())
            with self.assertRaises(ValueError):
                service.review(draft["id"], "approve", "test-reviewer", "Approved fixture", "2026-09-25")
            self.workspace.rollback(self.workspace.manifest["knowledge_history"][0]["release_id"])
            service.runner = OfflineRunner(hook=lambda: self.workspace.publish(self.changed_config()))
            with self.assertRaises(ValueError):
                service.analyze("alpha", "test-account", "chat", "2026-09-25")
        finally:
            service.store.close()

    def test_publication_waits_for_review_commit(self):
        self.workspace.import_messages([self.message()], "fixture")
        service = self.workspace.service(OfflineRunner())
        publication_attempted = threading.Event()
        publication_done = threading.Event()
        errors = []
        publisher = None
        try:
            draft = service.analyze("alpha", "test-account", "chat", "2026-09-25")
            real_review = service.store.review

            def publish():
                try:
                    publication_attempted.set()
                    Workspace.load(self.workspace.root).publish(self.changed_config())
                    publication_done.set()
                except BaseException as exc:
                    errors.append(exc)

            def reviewed_while_publication_waits(*args, **kwargs):
                nonlocal publisher
                publisher = threading.Thread(target=publish)
                publisher.start()
                self.assertTrue(publication_attempted.wait(2))
                self.assertFalse(publication_done.wait(0.05), "publication crossed the review commit boundary")
                return real_review(*args, **kwargs)

            with patch.object(service.store, "review", side_effect=reviewed_while_publication_waits):
                result = service.review(draft["id"], "approve", "reviewer", "Approved fixture", "2026-09-25")
            self.assertTrue(publication_done.wait(2))
            self.assertFalse(errors)
            self.assertEqual(result["status"], "approved")
        finally:
            if publisher:
                publisher.join(3)
            service.store.close()

    def test_publication_waits_for_analysis_save_but_not_model_call(self):
        self.workspace.import_messages([self.message()], "fixture")
        service = self.workspace.service(OfflineRunner())
        publication_attempted = threading.Event()
        publication_done = threading.Event()
        errors = []
        publisher = None
        try:
            real_save = service.store.save_draft

            def publish():
                try:
                    publication_attempted.set()
                    Workspace.load(self.workspace.root).publish(self.changed_config())
                    publication_done.set()
                except BaseException as exc:
                    errors.append(exc)

            def save_while_publication_waits(*args, **kwargs):
                nonlocal publisher
                publisher = threading.Thread(target=publish)
                publisher.start()
                self.assertTrue(publication_attempted.wait(2))
                self.assertFalse(publication_done.wait(0.05), "publication crossed the draft save boundary")
                return real_save(*args, **kwargs)

            with patch.object(service.store, "save_draft", side_effect=save_while_publication_waits):
                draft = service.analyze("alpha", "test-account", "chat", "2026-09-25")
            self.assertTrue(publication_done.wait(2))
            self.assertFalse(errors)
            with self.assertRaises(ValueError):
                service.review(draft["id"], "approve", "reviewer", "Approved fixture", "2026-09-25")
        finally:
            if publisher:
                publisher.join(3)
            service.store.close()

    def test_load_rejects_tampered_release_and_extra_company(self):
        current = self.workspace.configs_dir / "alpha.json"
        original = current.read_bytes()
        current.write_bytes(original.replace(b"SENSITIVE_KNOWLEDGE", b"TAMPERED_KNOWLEDGE"))
        with self.assertRaises(ValueError):
            Workspace.load(self.workspace.root)
        current.write_bytes(original)
        (self.workspace.configs_dir / "beta.json").write_text("{}")
        with self.assertRaises(ValueError):
            Workspace.load(self.workspace.root)

    def test_load_rejects_unknown_manifest_version_and_mode_swap(self):
        path = self.workspace.root / "workspace.json"
        original = json.loads(path.read_bytes())
        for change in ({"version": "9.0.0"}, {"schema_version": 9}, {"mode": "customer"}):
            path.write_text(json.dumps(original | change))
            with self.subTest(change=change), self.assertRaises(ValueError):
                Workspace.load(self.workspace.root)
        path.write_text(json.dumps(original))

    def test_load_rejects_manifest_symlink_and_foreign_database(self):
        path = self.workspace.root / "workspace.json"
        saved = self.root / "manifest.json"
        saved.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(saved)
        with self.assertRaises(ValueError):
            Workspace.load(self.workspace.root)
        path.unlink()
        path.write_bytes(saved.read_bytes())
        with closing(sqlite3.connect(self.workspace.db_path)) as conn, conn:
            conn.execute("UPDATE metadata SET value='another' WHERE key='company_id'")
        with self.assertRaises(ValueError):
            Workspace.load(self.workspace.root)

    def test_health_backup_and_restore_reject_missing_core_schema(self):
        with closing(sqlite3.connect(self.workspace.db_path)) as connection, connection:
            connection.execute("DROP TABLE outbox")
        self.assertFalse(self.workspace.health()["ok"])
        with self.assertRaises(ValueError):
            self.workspace.backup(self.root / "incomplete.zip")
        self.assertFalse((self.root / "incomplete.zip").exists())

    def test_restore_rejects_missing_core_table_even_with_valid_checksum(self):
        def mutate(files):
            db = self.root / "altered.sqlite3"
            db.write_bytes(files["data/inquiries.sqlite3"])
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute("DROP TABLE outbox")
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            content = db.read_bytes()
            files["data/inquiries.sqlite3"] = content
            manifest = json.loads(files["backup_manifest.json"])
            manifest["files"]["data/inquiries.sqlite3"] = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            files["backup_manifest.json"] = json.dumps(manifest).encode()
        self.assert_restore_rejected(self.mutate_archive(mutate))

    def test_backup_restore_preserves_messages_reviews_history_and_logs(self):
        imported = self.workspace.import_messages([self.message()], "fixture")
        service = self.workspace.service(OfflineRunner())
        try:
            draft = service.analyze("alpha", "test-account", "chat", "2026-09-25")
            service.review(draft["id"], "approve", "test-reviewer", "Approved fixture", "2026-09-25")
        finally:
            service.store.close()
        self.workspace.publish(self.changed_config())
        (self.workspace.root / "logs" / "test.log").write_text("test log")
        (self.workspace.root / "reports" / "test.json").write_text('{"synthetic":true}')
        archive = self.backup()
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        restored = Workspace.restore(archive, self.root / "restored")
        self.assertTrue(restored.health()["ok"])
        self.assertEqual(restored.manifest, self.workspace.manifest)
        self.assertEqual((restored.root / "logs" / "test.log").read_text(), "test log")
        store = restored.open_store()
        try:
            self.assertEqual(store.get_draft(draft["id"])["status"], "approved")
            self.assertEqual(store.outbox("alpha")[0]["final_text"], "Approved fixture")
            self.assertEqual(store.connection.execute("SELECT batch_id FROM import_batches").fetchone()[0], imported["batch_id"])
            self.assertEqual(store.connection.execute("SELECT count(*) FROM review_events").fetchone()[0], 1)
        finally:
            store.close()
        self.assertTrue(restored.import_messages([self.message()], "fixture")["replayed"])

    def test_backup_uses_online_snapshot_with_open_wal_connection(self):
        store = self.workspace.open_store()
        try:
            store.ingest(self.message())
            archive = self.backup()
            store.ingest(self.message(message_id="after-snapshot"))
            restored = Workspace.restore(archive, self.root / "restored")
            snapshot = restored.open_store()
            try:
                self.assertEqual([m["message_id"] for m in snapshot.messages("alpha", "test-account", "chat")], ["m1"])
            finally:
                snapshot.close()
        finally:
            store.close()

    def test_backup_ignores_interrupted_knowledge_staging_and_restores(self):
        interrupted = self.workspace.root / "knowledge" / ".release-interrupted"
        interrupted.mkdir()
        (interrupted / "incomplete.json").write_text('{"unfinished":')
        archive = self.backup()
        with zipfile.ZipFile(archive) as bundle:
            self.assertFalse(any(".release-interrupted" in name for name in bundle.namelist()))
        restored = Workspace.restore(archive, self.root / "restored")
        self.assertTrue(restored.health()["ok"])
        self.assertTrue(interrupted.exists())

    def test_backup_and_restore_never_overwrite_existing_content(self):
        archive = self.backup()
        original = archive.read_bytes()
        with self.assertRaises(ValueError):
            self.workspace.backup(archive)
        self.assertEqual(archive.read_bytes(), original)
        destination = self.root / "already-there"
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_text("keep")
        with self.assertRaises(ValueError):
            Workspace.restore(archive, destination)
        self.assertEqual(sentinel.read_text(), "keep")
        with self.assertRaises(ValueError):
            self.workspace.backup(self.workspace.root / "backup.zip")

    def test_restore_accepts_empty_directory(self):
        destination = self.root / "empty"
        destination.mkdir()
        restored = Workspace.restore(self.backup(), destination)
        self.assertTrue(restored.health()["ok"])

    def test_restore_rejects_traversal_absolute_and_duplicate_paths(self):
        archive = self.backup()
        for index, unsafe in enumerate(("../escape", "/tmp/escape", "reports/../escape", "reports\\escape", "reports//escape")):
            modified = self.root / f"unsafe-{index}.zip"
            modified.write_bytes(archive.read_bytes())
            with zipfile.ZipFile(modified, "a") as bundle:
                bundle.writestr(unsafe, b"escape")
            self.assert_restore_rejected(modified)
        duplicate = self.root / "duplicate.zip"
        duplicate.write_bytes(archive.read_bytes())
        with warnings.catch_warnings(), zipfile.ZipFile(duplicate, "a") as bundle:
            warnings.simplefilter("ignore", UserWarning)
            bundle.writestr("workspace.json", b"{}")
        self.assert_restore_rejected(duplicate)

    def test_restore_rejects_symlink_zip_entry(self):
        archive = self.backup()
        with zipfile.ZipFile(archive, "a") as bundle:
            link = zipfile.ZipInfo("logs/credential-link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(link, "/private/secret")
        self.assert_restore_rejected(archive)

    def test_restore_rejects_checksum_mismatch(self):
        archive = self.mutate_archive(lambda files: files.__setitem__("workspace.json", b"{}"))
        self.assert_restore_rejected(archive)

    def test_restore_rejects_corrupt_sqlite_even_with_matching_file_checksum(self):
        def mutate(files):
            files["data/inquiries.sqlite3"] = b"not a sqlite database"
            manifest = json.loads(files["backup_manifest.json"])
            content = files["data/inquiries.sqlite3"]
            manifest["files"]["data/inquiries.sqlite3"] = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            files["backup_manifest.json"] = json.dumps(manifest).encode()
        self.assert_restore_rejected(self.mutate_archive(mutate))

    def test_backup_rejects_symlink_to_outside_content(self):
        outside = self.root / "secret.txt"
        outside.write_text("not permitted")
        (self.workspace.root / "logs" / "link.txt").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.workspace.backup(self.root / "bad.zip")
        self.assertFalse((self.root / "bad.zip").exists())

    def test_health_has_no_message_knowledge_or_path_content(self):
        self.workspace.import_messages([self.message()], "fixture")
        healthy = self.workspace.health()
        self.assertTrue(healthy["ok"])
        self.assertEqual(healthy["readiness"], "alpha_requires_pilot")
        serialized = json.dumps(healthy)
        for private in ("SENSITIVE_MESSAGE", "SENSITIVE_KNOWLEDGE", str(self.root)):
            self.assertNotIn(private, serialized)
        (self.workspace.configs_dir / "alpha.json").write_text("{}")
        unhealthy = self.workspace.health()
        self.assertFalse(unhealthy["ok"])
        self.assertNotIn(str(self.root), json.dumps(unhealthy))


if __name__ == "__main__":
    unittest.main()
