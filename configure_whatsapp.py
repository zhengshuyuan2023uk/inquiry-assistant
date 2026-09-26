#!/usr/bin/env python3
"""Bind an explicitly approved bridge database and conversation allowlist locally."""
import argparse
import json
from pathlib import Path
import sys

from inquiry_product.manual_sync import configure_connection
from inquiry_product.workspace import Workspace


def main(argv=None):
    parser = argparse.ArgumentParser(description="为 customer 工作区配置手动聊天更新；仅读取桥接消息库，不登录、不发送。")
    parser.add_argument("--workspace", required=True, help="已经建立的 customer 工作区路径")
    parser.add_argument("--messages-db", required=True, help="本机 WhatsApp 桥接 messages.db 路径；不在网页中配置")
    parser.add_argument("--source-account-id", required=True, help="操作者指定的固定逻辑标识，如 sales-01；不是经过验证的 WhatsApp 登录身份")
    parser.add_argument("--conversations", required=True, help="初始获准会话 JSON 文件，可为 []，之后可在工作台扫描勾选；条目为 {\"jid\":\"…@s.whatsapp.net\",\"display_name\":\"…\"}")
    args = parser.parse_args(argv)
    try:
        source = Path(args.conversations).expanduser()
        if source.stat().st_size > 128_000:
            raise ValueError("会话配置文件过大")
        conversations = json.loads(source.read_text(encoding="utf-8"))
        result = configure_connection(Workspace.load(args.workspace), args.messages_db,
                                      args.source_account_id, conversations)
    except (ValueError, OSError) as exc:
        # Do not echo OSError filenames or raw configuration/message content.
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "无法读取工作区或授权会话配置文件"
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
