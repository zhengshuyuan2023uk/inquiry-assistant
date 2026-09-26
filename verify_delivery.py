#!/usr/bin/env python3
"""Offline repeatable delivery acceptance. Never invokes a real model or channel."""
import argparse
import json
from pathlib import Path
import tempfile

from inquiry_product.adapters import parse_chatwoot_events
from inquiry_product.workspace import Workspace

ROOT = Path(__file__).resolve().parent


class OfflineAcceptanceRunner:
    name = "offline_acceptance_not_ai"

    def analyze(self, context):
        return {"summary": "固定离线输出，仅测试交付流程", "facts": [], "missing_fields": context["required_fields"],
                "uncertainties": ["本轮不是模型质量测试"], "next_action": "人工核对", "reply": "离线测试草稿。",
                "citations": [], "needs_human": True}


def verify():
    checks = {}
    with tempfile.TemporaryDirectory(prefix="inquiry-delivery-") as folder:
        root = Path(folder)
        a = Workspace.init(root / "a", "logistics-demo", ROOT / "templates/logistics-demo.json")
        b = Workspace.init(root / "b", "trade-demo", ROOT / "templates/trade-demo.json")
        checks["two_independent_workspaces"] = a.db_path != b.db_path and a.company_id != b.company_id
        events = json.loads((ROOT / "examples/chatwoot-message.json").read_text())
        private_note = dict(events[0], id=50003, private=True, content="仅供内部的固定测试备注")
        parsed = parse_chatwoot_events(events + [private_note],
                                      company_id=b.company_id, mode=b.mode, account_id=101, inbox_id=201,
                                      received_at="2026-09-25T23:00:00Z")
        first = b.import_messages(parsed["messages"], "fixture-chatwoot")
        second = b.import_messages(parsed["messages"], "fixture-chatwoot")
        checks["message_replay_idempotent"] = first["inserted"] == len(parsed["messages"]) and second["inserted"] == 0
        checks["private_notes_excluded"] = any(x["reason"] == "private_message" for x in parsed["ignored"])
        try:
            a.import_messages(parsed["messages"], "wrong-company")
            checks["cross_company_input_rejected"] = False
        except ValueError:
            checks["cross_company_input_rejected"] = True
        service = b.service(OfflineAcceptanceRunner())
        message = parsed["messages"][0]
        old = service.analyze(b.company_id, message["account_id"], message["conversation_id"], "2026-09-25")
        old_config = service.project(b.company_id).data
        changed = json.loads(json.dumps(old_config))
        changed["rules"].append("离线发布测试：报价需两人核验。")
        changed_file = root / "new-knowledge.json"
        changed_file.write_text(json.dumps(changed, ensure_ascii=False))
        released = b.publish(changed_file)
        try:
            service.review(old["id"], "approve", "offline-acceptance", old["result"]["reply"], "2026-09-25")
            checks["knowledge_change_invalidates_old_draft"] = False
        except ValueError:
            checks["knowledge_change_invalidates_old_draft"] = True
        current = service.analyze(b.company_id, message["account_id"], message["conversation_id"], "2026-09-25")
        edited = "固定离线人工稿，不是 AI 输出或客户消息。"
        service.review(current["id"], "approve", "offline-acceptance", edited, "2026-09-25")
        service.review(current["id"], "approve", "offline-acceptance", edited, "2026-09-25")
        checks["approved_edits_saved_once"] = len(service.store.outbox()) == 1 and service.store.outbox()[0]["final_text"] == edited
        b.rollback(released["previous_release"])
        checks["knowledge_rollback_matches_original"] = service.project(b.company_id).data == old_config
        service.store.close()
        backup = root / "backup.zip"
        b.backup(backup)
        restored = Workspace.restore(backup, root / "restored")
        with_store = restored.open_store()
        checks["backup_restore_preserves_approval"] = with_store.get_draft(current["id"])["status"] == "approved" and len(with_store.outbox()) == 1
        with_store.close()
        checks["restore_integrity"] = restored.health()["ok"]
        with_store = a.open_store()
        checks["other_company_unchanged"] = with_store.conversations() == []
        with_store.close()
    return {"verification": "offline_delivery", "real_model_calls": 0, "external_messages_sent": 0,
            "data": "synthetic_only", "checks": checks, "passed": all(checks.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = verify()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
