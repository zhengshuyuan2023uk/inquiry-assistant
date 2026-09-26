"""Storage contract tests using synthetic messages only."""

import tempfile
import sqlite3
import threading
import unittest
from pathlib import Path

from inquiry_product.core.store import Store


SCOPE = ("logistics-demo", "fake-account", "fake-conversation")


def message(message_id="m1", **changes):
    value = dict(zip(("project_id", "account_id", "conversation_id"), SCOPE))
    value.update(message_id=message_id, direction="inbound", body="虚构货物询问",
                 sent_at="2026-09-25T10:00:00+08:00",
                 received_at="2026-09-25T10:01:00+08:00")
    value.update(changes)
    return value


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "synthetic.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def draft(self, **changes):
        self.store.ingest(message())
        params = dict(project_id=SCOPE[0], account_id=SCOPE[1], conversation_id=SCOPE[2],
                      fingerprint=self.store.fingerprint(*SCOPE), knowledge_digest="knowledge-v1",
                      as_of="2026-09-25", result={"reply": "原始虚构草稿", "extra": ["保留"]},
                      runner="fake-runner")
        params.update(changes)
        return self.store.save_draft(**params)

    def approve(self, draft, **changes):
        params = dict(draft_id=draft["id"], decision="approve", reviewer="测试审核员",
                      final_text="人工编辑后的虚构回复", current_knowledge_digest="knowledge-v1",
                      as_of="2026-09-25")
        params.update(changes)
        return self.store.review(**params)

    def test_scoped_duplicates_ignore_receipt_time_and_keep_first_receipt(self):
        original = message()
        self.assertTrue(self.store.ingest(original))
        fingerprint = self.store.fingerprint(*SCOPE)
        self.assertFalse(self.store.ingest(message(received_at="2026-09-27T02:00:00Z")))
        self.assertEqual(self.store.fingerprint(*SCOPE), fingerprint)
        self.assertEqual(self.store.messages(*SCOPE)[0]["received_at"], original["received_at"])
        self.assertTrue(self.store.ingest(message(account_id="other-fake-account")))
        self.assertTrue(self.store.ingest(message(project_id="trade-demo")))
        self.assertEqual(len(self.store.conversations()), 3)
        self.assertEqual(len(self.store.conversations("trade-demo")), 1)

    def test_payload_conflicts_do_not_mutate_original(self):
        self.store.ingest(message())
        original_fingerprint = self.store.fingerprint(*SCOPE)
        for change in ({"body": "changed"}, {"direction": "outbound"},
                       {"sent_at": "2026-09-25T11:00:00+08:00"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.ingest(message(**change))
        self.assertEqual(self.store.fingerprint(*SCOPE), original_fingerprint)

    def test_late_arrivals_and_timezones_sort_by_instant_stably(self):
        self.store.ingest(message("latest", sent_at="2026-09-25T01:00:00-04:00"))
        self.store.ingest(message("b-tie", sent_at="2026-09-25T10:00:00+08:00"))
        self.store.ingest(message("a-tie", sent_at="2026-09-25T02:00:00Z"))
        self.store.ingest(message("earliest", sent_at="2026-09-24T23:30:00Z",
                                  received_at="2026-09-27T01:00:00Z"))
        self.assertEqual([m["message_id"] for m in self.store.messages(*SCOPE)],
                         ["earliest", "a-tie", "b-tie", "latest"])

    def test_invalid_input_cannot_insert(self):
        for change in ({"project_id": " "}, {"direction": "system"}, {"body": 42}, {"body": ""}, {"body": " \n"},
                       {"sent_at": "2026-09-25T01:00:00"}, {"received_at": "bad"},
                       {"mode": "production"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.ingest(message(**change))
        self.assertEqual(self.store.conversations(), [])

    def test_save_checks_fingerprint_after_another_connection_changes_messages(self):
        self.store.ingest(message())
        old_fingerprint = self.store.fingerprint(*SCOPE)
        other = Store(self.path)
        try:
            other.ingest(message("m2", body="迟到的虚构消息"))
            with self.assertRaises(ValueError):
                self.draft(fingerprint=old_fingerprint)
        finally:
            other.close()
        self.assertEqual(self.store.daily(SCOPE[0], "2026-09-25")["drafts"], [])

    def test_original_result_is_immutable_and_edited_final_is_audited(self):
        draft = self.draft()
        draft["result"]["reply"] = "调用方修改"
        approved = self.approve(draft)
        self.assertEqual(approved["result"]["reply"], "原始虚构草稿")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["final_text"], "人工编辑后的虚构回复")
        self.assertEqual(approved["runner"], "fake-runner")
        outbox = self.store.outbox(SCOPE[0])
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["mode"], "simulation")
        self.assertEqual(outbox[0]["final_text"], approved["final_text"])
        self.assertEqual(outbox[0]["draft_id"], draft["id"])
        review = self.store.daily(SCOPE[0], "2026-09-25")["reviews"][0]
        self.assertEqual(review["reviewer"], "测试审核员")
        self.assertEqual(review["outcome"], "approved")

    def test_source_context_is_saved_as_an_immutable_snapshot(self):
        context = {"mode": "simulation", "knowledge": [{"id": "fake-k1", "content": "虚构旧资料"}]}
        draft = self.draft(context=context)
        context["knowledge"][0]["content"] = "虚构新资料"
        draft["context"]["knowledge"][0]["content"] = "调用方更改"
        self.assertEqual(self.store.get_draft(draft["id"])["context"]["knowledge"][0]["content"], "虚构旧资料")

    def test_context_message_snapshot_must_match_draft_fingerprint(self):
        self.store.ingest(message())
        for snapshot in ([], [message(body="篡改的虚构内容")], [message(project_id="trade-demo")]):
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                self.draft(context={"messages": snapshot})
        draft = self.draft(context={"messages": [message()]})
        self.assertEqual(draft["context"]["messages"][0]["message_id"], "m1")

    def test_changes_in_another_scope_do_not_invalidate_draft(self):
        draft = self.draft()
        self.store.ingest(message("m2", account_id="different-account"))
        self.store.ingest(message("m2", project_id="trade-demo"))
        self.assertEqual(self.store.get_draft(draft["id"])["status"], "pending")
        approved = self.approve(draft, as_of="2026-09-26")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(self.store.outbox()[0]["as_of"], "2026-09-26")

    def test_outbox_failure_rolls_back_status_and_audit_event(self):
        draft = self.draft()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("""CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox
                BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.approve(draft)
        self.assertEqual(self.store.get_draft(draft["id"])["status"], "pending")
        self.assertEqual(self.store.outbox(), [])
        self.assertEqual(self.store.daily(SCOPE[0], "2026-09-25")["reviews"], [])

    def test_ingest_cannot_interleave_approval_check_and_outbox_write(self):
        draft = self.draft()
        checked = threading.Event()
        release = threading.Event()
        ingest_started = threading.Event()
        ingest_finished = threading.Event()
        errors = []

        class PausingStore(Store):
            def fingerprint(inner_self, *scope):
                value = super().fingerprint(*scope)
                checked.set()
                if not release.wait(3):
                    raise RuntimeError("test coordination timed out")
                return value

        def approve():
            other = PausingStore(self.path)
            try:
                other.review(draft["id"], "approve", "测试审核员", "并发测试最终稿", "knowledge-v1", "2026-09-25")
            except BaseException as exc:
                errors.append(exc)
            finally:
                other.close()

        def ingest():
            other = Store(self.path)
            try:
                ingest_started.set()
                other.ingest(message("late-message"))
            except BaseException as exc:
                errors.append(exc)
            finally:
                other.close()
                ingest_finished.set()

        approval_thread = threading.Thread(target=approve)
        ingest_thread = threading.Thread(target=ingest)
        approval_thread.start()
        try:
            self.assertTrue(checked.wait(3))
            ingest_thread.start()
            self.assertTrue(ingest_started.wait(3))
            self.assertFalse(ingest_finished.wait(0.1), "ingest bypassed approval's transaction lock")
        finally:
            release.set()
            approval_thread.join(5)
            if ingest_thread.ident is not None:
                ingest_thread.join(5)
        self.assertFalse(approval_thread.is_alive())
        self.assertFalse(ingest_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.outbox()), 1)
        self.assertEqual(len(self.store.messages(*SCOPE)), 2)

    def test_message_staleness_is_persisted_and_reject_is_still_possible(self):
        draft = self.draft()
        other = Store(self.path)
        try:
            other.ingest(message("m2"))
            with self.assertRaises(ValueError):
                self.approve(draft)
        finally:
            other.close()
        self.assertEqual(self.store.get_draft(draft["id"])["status"], "stale")
        self.assertEqual(self.store.outbox(), [])
        self.assertEqual(self.store.daily(SCOPE[0], "2026-09-25")["reviews"][0]["outcome"], "stale")
        rejected = self.store.review(draft["id"], "reject", "测试审核员", None,
                                     "another-knowledge", "2026-09-26")
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaises(ValueError):
            self.approve(draft, as_of="2026-09-26")
        self.assertEqual(self.store.outbox(), [])

    def test_knowledge_staleness_commits_before_raising(self):
        draft = self.draft()
        with self.assertRaises(ValueError):
            self.approve(draft, current_knowledge_digest="changed")
        other = Store(self.path)
        try:
            self.assertEqual(other.get_draft(draft["id"])["status"], "stale")
        finally:
            other.close()
        self.assertEqual(self.store.outbox(), [])

    def test_approve_is_idempotent_but_a_new_final_is_not(self):
        draft = self.draft()
        self.approve(draft)
        repeated = self.approve(draft, as_of="2026-09-26")
        self.assertEqual(repeated["status"], "approved")
        self.assertEqual(len(self.store.outbox()), 1)
        self.assertEqual(len(self.store.daily(SCOPE[0], "2026-09-25")["reviews"]), 1)
        self.assertEqual(self.store.daily(SCOPE[0], "2026-09-26")["reviews"], [])
        with self.assertRaises(ValueError):
            self.approve(draft, final_text="different final")
        self.assertEqual(len(self.store.outbox()), 1)

    def test_completed_approval_remains_immutable_after_messages_and_knowledge_change(self):
        draft = self.draft()
        approved = self.approve(draft)
        self.store.ingest(message("m2", received_at="2026-09-26T12:00:00Z"))
        repeated = self.approve(draft, current_knowledge_digest="knowledge-v2", as_of="2026-09-26")
        self.assertEqual(repeated, approved)
        self.assertEqual(len(self.store.outbox()), 1)
        with self.assertRaises(ValueError):
            self.store.review(draft["id"], "reject", "测试审核员", None, "knowledge-v2", "2026-09-26")
        self.assertEqual(self.store.get_draft(draft["id"])["status"], "approved")

    def test_daily_reconstructs_status_and_final_text_as_of_report_date(self):
        draft = self.draft()
        self.approve(draft, as_of="2026-09-26")
        earlier = self.store.daily(SCOPE[0], "2026-09-25")
        self.assertEqual(earlier["counts"]["pending_drafts"], 1)
        self.assertEqual(earlier["counts"]["outbox"], 0)
        self.assertEqual(earlier["drafts"][0]["status"], "pending")
        self.assertIsNone(earlier["drafts"][0]["final_text"])
        self.assertIsNone(earlier["drafts"][0]["reviewed_as_of"])
        later = self.store.daily(SCOPE[0], "2026-09-26")
        self.assertEqual(later["counts"]["pending_drafts"], 0)
        self.assertEqual(later["counts"]["outbox"], 1)

    def test_daily_reconstructs_automatic_staleness_at_receipt_day(self):
        draft = self.draft()
        self.store.ingest(message("m2", received_at="2026-09-26T12:00:00Z"))
        self.store.review(draft["id"], "reject", "测试审核员", None, "knowledge-v1", "2026-09-27")
        day25 = self.store.daily(SCOPE[0], "2026-09-25")
        self.assertEqual(day25["counts"]["pending_drafts"], 1)
        self.assertEqual(day25["counts"]["stale_drafts"], 0)
        self.assertEqual(day25["conversations"][0]["message_count"], 1)
        day26 = self.store.daily(SCOPE[0], "2026-09-26")
        self.assertEqual(day26["counts"]["pending_drafts"], 0)
        self.assertEqual(day26["counts"]["stale_drafts"], 1)
        self.assertEqual(day26["stale_drafts"][0]["status"], "stale")
        self.assertIsNone(day26["stale_drafts"][0]["reviewed_as_of"])
        day27 = self.store.daily(SCOPE[0], "2026-09-27")
        self.assertEqual(day27["counts"]["stale_drafts"], 0)

    def test_review_cannot_be_backdated_before_a_later_state_change(self):
        draft = self.draft()
        self.store.ingest(message("m2", received_at="2026-09-27T12:00:00Z"))
        with self.assertRaises(ValueError):
            self.store.review(draft["id"], "reject", "测试审核员", None, "knowledge-v1", "2026-09-26")
        self.assertEqual(self.store.get_draft(draft["id"])["status"], "stale")
        self.assertEqual(self.store.daily(SCOPE[0], "2026-09-26")["reviews"], [])

    def test_drafts_from_earlier_process_get_history_backfilled(self):
        draft = self.draft(context={"messages": [message()]})
        self.approve(draft, as_of="2026-09-26")
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DELETE FROM draft_states")
            connection.commit()
        finally:
            connection.close()
        earlier = self.store.daily(SCOPE[0], "2026-09-25")
        self.assertEqual(earlier["counts"]["pending_drafts"], 1)
        self.assertEqual(earlier["drafts"][0]["status"], "pending")
        later = self.store.daily(SCOPE[0], "2026-09-26")
        self.assertEqual(later["counts"]["pending_drafts"], 0)
        self.assertEqual(self.store.get_draft(draft["id"])["status"], "approved")

    def test_rejected_draft_never_creates_outbox_or_becomes_approved(self):
        draft = self.draft()
        self.store.review(draft["id"], "reject", "测试审核员", None, "expired", "2026-09-25")
        with self.assertRaises(ValueError):
            self.approve(draft)
        self.assertEqual(self.store.outbox(), [])

    def test_reviewer_and_dates_are_validated_without_mutation(self):
        draft = self.draft()
        for changes in ({"reviewer": " "}, {"as_of": "2026-09-24"},
                        {"as_of": "2026-02-30"}, {"final_text": ""}, {"decision": "send"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.approve(draft, **changes)
        self.assertEqual(self.store.get_draft(draft["id"])["status"], "pending")
        self.assertEqual(self.store.outbox(), [])

    def test_daily_uses_first_receipt_utc_and_logical_review_days(self):
        draft = self.draft()
        self.store.ingest(message("m2", received_at="2026-09-26T00:05:00+08:00"))
        self.store.ingest(message("m3", received_at="2026-09-26T12:00:00Z"))
        self.store.ingest(message(account_id="new-fake-account", received_at="2026-09-26T12:00:00Z"))
        self.store.ingest(message(project_id="trade-demo", received_at="2026-09-26T12:00:00Z"))
        day25 = self.store.daily(SCOPE[0], "2026-09-25")
        self.assertEqual(day25["counts"]["new_messages"], 2)
        self.assertEqual(day25["counts"]["new_conversations"], 1)
        day26 = self.store.daily(SCOPE[0], "2026-09-26")
        self.assertEqual(day26["counts"]["new_messages"], 2)
        self.assertEqual(day26["counts"]["new_conversations"], 1)
        self.assertEqual(day26["counts"]["stale_drafts"], 1)
        self.assertEqual(day26["messages"][0]["body"], "虚构货物询问")
        self.store.review(draft["id"], "reject", "测试审核员", None, "changed", "2026-09-27")
        day27 = self.store.daily(SCOPE[0], "2026-09-27")
        self.assertEqual(day27["counts"]["reviews"], 1)
        self.assertEqual(day27["counts"]["outbox"], 0)
        self.assertEqual(day27["reviews"][0]["draft_id"], draft["id"])

    def test_persisted_storage_and_unknown_draft_errors(self):
        draft = self.draft()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_draft(draft["id"])["result"], draft["result"])
        with self.assertRaises(ValueError):
            self.store.get_draft("does-not-exist")


if __name__ == "__main__":
    unittest.main()
