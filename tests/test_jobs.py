"""Offline job persistence checks use new synthetic workspaces only."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from inquiry_product.jobs import JobStore, RESTART_ERROR, TERMINAL
from inquiry_product.workspace import Workspace


def config(identity="alpha"):
    return {"id": identity, "name": "虚构任务测试", "mode": "simulation", "industry": "test",
            "required_fields": ["数量"], "rules": ["不承诺未知事实"], "knowledge": []}


def job(identity="job-1", **changes):
    return {"id": identity, "account_id": "synthetic-account", "conversation_id": "synthetic-chat",
            "status": "queued", "started_at": "2026-09-25T00:00:00+00:00", "revision": 0,
            "request": {"mode": "polish", "model": "gpt-test"}, **changes}


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = Workspace.init(self.root / "a", "alpha", config())
        self.store = JobStore(self.workspace)

    def tearDown(self):
        self.temporary.cleanup()

    def test_save_load_across_store_restart_and_additive_schema(self):
        self.store.save(job(progress_message="正在处理"))
        restored = JobStore(Workspace.load(self.workspace.root))
        saved = restored.get("job-1")
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["progress_message"], "正在处理")
        self.assertEqual(saved["request"], {"mode": "polish", "model": "gpt-test"})
        self.assertTrue(self.workspace.health()["ok"])
        with closing(sqlite3.connect(self.workspace.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], "2")

    def test_only_allowlisted_fields_reach_database_file(self):
        private = "PRIVATE_SYNTHETIC_CONTENT_NOT_FOR_JOB_STORAGE"
        incoming = job(original_text=private, instruction=private, prompt=private,
                       warning='回复已保存，但运行记录写入失败',
                       preview_reply=private, token=private, credentials={"secret": private},
                       request={"mode": "refine", "model": "gpt-test", "original_text": private,
                                "instruction": private, "prompt": private, "token": private})
        self.store.save(incoming)
        saved = self.store.get("job-1")
        self.assertNotIn(private, json.dumps(saved))
        self.assertEqual(saved["request"], {"mode": "refine", "model": "gpt-test"})
        self.assertEqual(saved['warning'], '回复已保存，但运行记录写入失败')
        self.assertEqual(set(saved), {"id", "account_id", "conversation_id", "status", "started_at",
                                     "finished_at", "draft_id", "error", "warning", "progress_message", "revision", "request"})
        self.assertNotIn(private.encode(), self.workspace.db_path.read_bytes())
        self.assertEqual(incoming["original_text"], private)

    def test_request_none_and_missing_fields_are_not_manufactured(self):
        self.store.save(job(request=None))
        self.store.save(job("job-2", request={"model": None, "prompt": "synthetic ignored"}))
        self.assertIsNone(self.store.get("job-1")["request"])
        self.assertEqual(self.store.get("job-2")["request"], {"model": None})

    def test_empty_optional_display_fields_are_stored_as_null(self):
        self.store.save(job(error="", progress_message="", draft_id=""))
        saved = self.store.get("job-1")
        for field in ("error", "progress_message", "draft_id"):
            self.assertIsNone(saved[field])

    def test_restart_recovery_only_interrupts_unfinished_jobs_once(self):
        for index, status in enumerate(("queued", "running", "cancelling", "succeeded", "failed", "cancelled", "interrupted")):
            self.store.save(job(f"job-{index}", status=status, revision=2))
        finished = "2026-09-25T03:00:00.000000+00:00"
        with patch("inquiry_product.jobs._now", return_value=finished):
            self.assertEqual(JobStore(self.workspace).recover_interrupted(), 3)
        for index in range(3):
            saved = self.store.get(f"job-{index}")
            self.assertEqual(saved["status"], "interrupted")
            self.assertEqual(saved["finished_at"], finished)
            self.assertEqual(saved["error"], RESTART_ERROR)
            self.assertEqual(saved["revision"], 3)
        self.assertEqual(self.store.get("job-3")["status"], "succeeded")
        self.assertEqual(self.store.recover_interrupted(), 0)

    def test_terminal_record_cannot_be_revived_or_replaced(self):
        for index, status in enumerate(sorted(TERMINAL)):
            identity = f"job-{index}"
            self.store.save(job(identity, status=status, revision=3, error="固定测试结果"))
            before = self.store.get(identity)
            self.store.save(job(identity, status="running", revision=4))
            self.store.save(job(identity, status="succeeded", revision=5))
            self.assertEqual(self.store.get(identity), before)

    def test_delayed_revision_and_cancelling_progress_cannot_go_backwards(self):
        self.store.save(job(status="running", revision=2, progress_message="较新进度"))
        self.store.save(job(status="running", revision=1, progress_message="迟到进度"))
        self.assertEqual(self.store.get("job-1")["progress_message"], "较新进度")
        self.store.save(job(status="cancelling", revision=3))
        self.store.save(job(status="running", revision=4))
        self.assertEqual(self.store.get("job-1")["status"], "cancelling")
        self.store.save(job(status="cancelled", revision=5))
        self.assertEqual(self.store.get("job-1")["status"], "cancelled")

    def test_existing_identity_cannot_switch_conversation_or_request(self):
        self.store.save(job())
        for update in ({"account_id": "other"}, {"conversation_id": "other"},
                       {"request": {"mode": "generate", "model": "gpt-other"}},
                       {"started_at": "2026-09-26T00:00:00Z"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.store.save(job(**update))
        self.assertEqual(self.store.get("job-1")["status"], "queued")

    def test_workspace_isolation_unknown_identity_and_parameterized_id(self):
        other = Workspace.init(self.root / "b", "beta", config("beta"))
        other_store = JobStore(other)
        self.store.save(job(progress_message="企业A"))
        other_store.save(job(progress_message="企业B"))
        self.assertEqual(self.store.get("job-1")["progress_message"], "企业A")
        self.assertEqual(other_store.get("job-1")["progress_message"], "企业B")
        self.assertIsNone(self.store.get("missing"))
        self.assertIsNone(self.store.get("' OR 1=1 --"))
        self.store.save(job("'; DROP TABLE workbench_jobs; --"))
        self.assertEqual(len(self.store.recent()), 2)
        self.assertEqual(len(other_store.recent()), 1)
        with self.assertRaises(ValueError):
            self.store.save(job(company_id="beta"))

    def test_database_binding_change_is_rejected(self):
        with closing(sqlite3.connect(self.workspace.db_path)) as connection, connection:
            connection.execute("UPDATE metadata SET value='other' WHERE key='company_id'")
        with self.assertRaises(ValueError):
            self.store.get("job-1")

    def test_recovery_does_not_touch_other_company_rows_or_model_runs(self):
        self.store.save(job())
        foreign = self.store.get("job-1") | {"id": "foreign"}
        with closing(sqlite3.connect(self.workspace.db_path)) as connection, connection:
            connection.execute("INSERT INTO workbench_jobs VALUES (?,?,?,?,?,?)",
                               ("beta", "foreign", "running", foreign["started_at"], 0, json.dumps(foreign)))
            connection.execute("CREATE TABLE model_runs (id TEXT PRIMARY KEY,status TEXT)")
            connection.execute("INSERT INTO model_runs VALUES ('unrelated-run','running')")
        self.assertIsNone(self.store.get("foreign"))
        self.assertEqual(len(self.store.recent()), 1)
        self.assertEqual(self.store.recover_interrupted(), 1)
        with closing(sqlite3.connect(self.workspace.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM workbench_jobs WHERE company_id='beta'").fetchone()[0], "running")
            self.assertEqual(connection.execute("SELECT status FROM model_runs").fetchone()[0], "running")

    def test_recent_limit_orders_latest_without_removing_history(self):
        beginning = datetime(2026, 9, 25, tzinfo=timezone.utc)
        for index in range(105):
            self.store.save(job(f"job-{index:03}", started_at=(beginning + timedelta(seconds=index)).isoformat()))
        self.assertEqual(len(self.store.recent()), 100)
        self.assertEqual([item["id"] for item in self.store.recent(2)], ["job-104", "job-103"])
        self.assertEqual(len(self.store.recent(200)), 105)
        self.assertIsNotNone(self.store.get("job-000"))
        for value in (0, -1, True, 1.5, "2", 1001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.recent(value)

    def test_reads_do_not_change_database_mtime_and_connections_close(self):
        self.store.save(job())
        before = self.workspace.db_path.stat().st_mtime_ns
        opened = []
        real_connect = sqlite3.connect

        def capture(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch("inquiry_product.jobs.sqlite3.connect", side_effect=capture):
            self.store.get("job-1")
            self.store.recent()
        self.assertEqual(self.workspace.db_path.stat().st_mtime_ns, before)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_invalid_input_does_not_persist_a_record(self):
        for update in ({"status": "unknown"}, {"status": []}, {"revision": True}, {"revision": -1},
                       {"started_at": "2026-09-25"}, {"request": []}, {"request": {"model": "--config"}},
                       {"request": {"mode": "send"}}, {"id": ""}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.store.save(job(**update))
        self.assertEqual(self.store.recent(), [])


if __name__ == "__main__":
    unittest.main()
