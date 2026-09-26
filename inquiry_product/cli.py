"""One-company local delivery toolkit. No external message sender."""
import argparse
import getpass
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .adapters import parse_chatwoot_events
from .core.engine import CodexRunner, EngineError
from .workspace import Workspace
from .telemetry import start_run, finish_run, list_runs


def _day():
    return datetime.now(timezone.utc).date().isoformat()


def _read_records(path):
    if path.stat().st_size > 5_000_000:
        raise ValueError("单批文件上限为 5 MB，请按来源批次拆分")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        data = json.loads(text)
        if not isinstance(data, (dict, list)):
            raise ValueError("输入必须为消息数组或消息对象")
        records = data if isinstance(data, list) else data.get("messages", [data])
    if not isinstance(records, list) or len(records) > 2000:
        raise ValueError("每批最多 2000 条记录")
    return records


def _parser():
    p = argparse.ArgumentParser(description="询盘助手 alpha · 一客户一工作区 · 审核后人工发送")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--workspace", type=Path, help="独立客户工作区；除了 restore 都必填")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--company", required=True)
    init.add_argument("--config", required=True, type=Path)
    init.add_argument("--mode", choices=["simulation", "customer"], default="simulation")
    for name in ("status", "health", "outbox", "runs"):
        sub.add_parser(name)
    imp = sub.add_parser("import")
    imp.add_argument("--file", type=Path, required=True)
    imp.add_argument("--source", required=True, help="业务来源标签，例如 approved-export-01")
    imp.add_argument("--format", choices=["normalized", "chatwoot"], default="normalized")
    imp.add_argument("--chatwoot-account", type=int)
    imp.add_argument("--chatwoot-inbox", type=int)
    imp.add_argument("--received-at", help="Chatwoot 文件的实际接收时间，缺省当前 UTC")
    for name in ("messages", "context", "analyze"):
        command = sub.add_parser(name)
        command.add_argument("--account", required=True)
        command.add_argument("--conversation", required=True)
        if name != "messages":
            command.add_argument("--as-of", default=_day())
        if name == "analyze":
            command.add_argument("--timeout", type=int, default=240)
            command.add_argument("--allow-customer-model", action="store_true",
                                 help="确认本批客户数据获准提供给本机模型账号；不构成发送授权")
    draft = sub.add_parser("draft")
    draft.add_argument("--id", required=True)
    draft.add_argument("--full", action="store_true")
    review = sub.add_parser("review")
    review.add_argument("--id", required=True)
    review.add_argument("--decision", required=True, choices=["approve", "reject"])
    text = review.add_mutually_exclusive_group()
    text.add_argument("--text")
    text.add_argument("--text-file", type=Path)
    text.add_argument("--use-original", action="store_true")
    review.add_argument("--as-of", default=_day())
    daily = sub.add_parser("daily")
    daily.add_argument("--day", required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("--config", type=Path, required=True)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--release", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    return p


def _trim(draft):
    return {k: v for k, v in draft.items() if k != "context"}


def main(argv=None):
    args = _parser().parse_args(argv)
    store = None
    service = None
    try:
        if args.command == "restore":
            ws = Workspace.restore(args.archive, args.destination)
            result = ws.health()
        else:
            if not args.workspace:
                raise ValueError("请指定 --workspace，客户数据必须保存在独立目录")
            if args.command == "init":
                ws = Workspace.init(args.workspace, args.company, args.config, mode=args.mode)
                result = ws.health()
            else:
                ws = Workspace.load(args.workspace)
                if args.command in ("status", "health"):
                    result = ws.health()
                    if args.command == "status":
                        store = ws.open_store()
                        result["conversations"] = store.conversations(ws.company_id)
                        result["operator"] = "local:" + getpass.getuser()
                        result["delivery"] = "审核通过只入待人工发送清单；未连接发送器"
                elif args.command == "import":
                    records = _read_records(args.file)
                    ignored = []
                    if args.format == "chatwoot":
                        if args.chatwoot_account is None or args.chatwoot_inbox is None:
                            raise ValueError("Chatwoot 导入必须指定 --chatwoot-account 和 --chatwoot-inbox 白名单")
                        parsed = parse_chatwoot_events(records, company_id=ws.company_id, mode=ws.mode,
                                                       account_id=args.chatwoot_account, inbox_id=args.chatwoot_inbox,
                                                       received_at=args.received_at or datetime.now(timezone.utc).isoformat())
                        records, ignored = parsed["messages"], parsed["ignored"]
                    result = ws.import_messages(records, args.source)
                    result["ignored"] = ignored
                    result["coverage"] = "仅本批文本文件；不表示渠道历史已完整同步"
                elif args.command == "publish":
                    result = ws.publish(args.config)
                elif args.command == "rollback":
                    result = ws.rollback(args.release)
                elif args.command == "backup":
                    result = ws.backup(args.output)
                elif args.command == "runs":
                    result = list_runs(ws)
                else:
                    store = ws.open_store()
                    service = ws.service(CodexRunner(timeout=getattr(args, "timeout", 240)))
                    if args.command == "messages":
                        result = store.messages(ws.company_id, args.account, args.conversation)
                    elif args.command == "context":
                        result = service.context(ws.company_id, args.account, args.conversation, args.as_of)
                    elif args.command == "analyze":
                        if ws.mode == "customer" and not args.allow_customer_model:
                            raise ValueError("客户数据的模型使用尚未确认；经授权后使用 --allow-customer-model，本机应登录该客户自己的账号")
                        context = service.context(ws.company_id, args.account, args.conversation, args.as_of)
                        if len(context["messages"]) > 500 or len(json.dumps(context, ensure_ascii=False)) > 120_000:
                            raise ValueError("当前会话或资料超出本版输入限额，需人工整理；未静默截断或调用模型")
                        run_id = start_run(ws, args.conversation, context)
                        started = time.monotonic()
                        try:
                            print("正在调用本机 Codex；使用当前登录的用量。", file=sys.stderr)
                            draft = service.analyze(ws.company_id, args.account, args.conversation, args.as_of)
                            finish_run(ws, run_id, int((time.monotonic() - started) * 1000), draft_id=draft["id"])
                            result = _trim(draft) | {"run_id": run_id}
                        except Exception as exc:
                            finish_run(ws, run_id, int((time.monotonic() - started) * 1000), error_category=getattr(exc, "category", "validation_or_runtime"))
                            raise
                    elif args.command in ("draft", "review"):
                        draft = store.get_draft(args.id)
                        if draft["project_id"] != ws.company_id:
                            raise ValueError("草稿不属于当前客户")
                        if args.command == "draft":
                            result = draft if args.full else _trim(draft)
                        else:
                            final = args.text_file.read_text(encoding="utf-8") if args.text_file else args.text
                            if args.use_original:
                                final = draft["result"]["reply"]
                            if args.decision == "approve" and final is None:
                                raise ValueError("批准需提供最终文案或显式 --use-original")
                            result = _trim(service.review(args.id, args.decision, "local:" + getpass.getuser(), final, args.as_of))
                    elif args.command == "outbox":
                        result = store.outbox(ws.company_id)
                        current_knowledge = service.project(ws.company_id).context(_day())["knowledge_digest"]
                        for entry in result:
                            basis = store.get_draft(entry["draft_id"])
                            entry["requires_recheck"] = (
                                current_knowledge != basis["knowledge_digest"] or
                                store.fingerprint(ws.company_id, basis["account_id"], basis["conversation_id"]) != basis["fingerprint"]
                            )
                            entry["delivery_status"] = "external_delivery_unknown"
                            entry["tool_delivery_performed"] = False
                    elif args.command == "daily":
                        result = service.daily(ws.company_id, args.day)
                    else:
                        raise ValueError("不支持的命令")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, EngineError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if service and service.store is not store:
            service.store.close()
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
