"""Explicit, bounded reads of an allowlisted WhatsApp bridge snapshot.

No network, credentials, bridge mutation, automatic polling or sending. A local
bridge snapshot cannot establish whether WhatsApp is online or history complete.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .core.store import StoreError, message_payload, normalize_message
from .web_demo import ACCOUNT_ID, conversation_labels as demo_labels
from .workspace import Workspace, _atomic_write, _json_bytes, _mkdir

CONFIG_FILE = "connections/whatsapp.json"
STATE_FILE = "logs/manual-sync.json"
MAX_ROWS = 10000
MAX_CONTENT_CHARS = 8_000_000
BATCH_SIZE = 250
MAX_ACTIVE = 100
MAX_MANAGED = 2000
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_JID = re.compile(r"[A-Za-z0-9_.:-]{1,160}@(s\.whatsapp\.net|lid|g\.us)\Z")
_SOURCE_LABEL = "WhatsApp 桥接库（手动读取）"
_DEMO_LABEL = "固定虚构消息（演练更新，未连接 WhatsApp）"
_NOTE = "仅检查桥接库当前已接收的授权会话；无法确认 WhatsApp 在线或历史完整。"


class SyncBusyError(ValueError):
    pass


class _SourceFailure(ValueError):
    def __init__(self, code, note, coverage=None):
        super().__init__(note)
        self.code, self.coverage = code, coverage or {}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _fresh(workspace):
    current = Workspace.load(workspace.root)
    if (current.company_id, current.mode) != (workspace.company_id, workspace.mode):
        raise ValueError("工作区绑定已改变")
    return current


def _read_json(path, maximum=16_000_000):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("同步配置或状态文件不可用")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("同步配置或状态文件不可用") from exc
    if not isinstance(value, dict):
        raise ValueError("同步配置或状态必须是 JSON 对象")
    return value


@contextmanager
def _sync_lock(workspace):
    path = workspace.root / "logs" / ".manual-sync.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("同步锁文件不可用")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncBusyError("聊天正在更新，请等待本次更新完成") from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _binding(workspace):
    return {"company_id": workspace.company_id, "mode": workspace.mode,
            "workspace_created_at": workspace.manifest["created_at"]}


def _validate_config(workspace, config):
    if workspace.mode != "customer":
        raise ValueError("真实 WhatsApp 来源只能绑定 customer 工作区，不能写入演练库")
    if (config.get("schema_version") != 1 or config.get("source_type") != "whatsapp_bridge"
            or any(config.get(key) != value for key, value in _binding(workspace).items())):
        raise ValueError("WhatsApp 配置与工作区绑定不一致")
    identity = config.get("source_account_id")
    if not isinstance(identity, str) or not _ID.fullmatch(identity):
        raise ValueError("来源账号标识仅允许 1–64 位英文字母、数字、下划线或短横线")
    path = config.get("messages_db")
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise ValueError("消息库需要本机绝对路径")
    scope = config.get("conversations")
    if not isinstance(scope, list) or len(scope) > MAX_MANAGED:
        raise ValueError(f"会话名单最多保留 {MAX_MANAGED} 个已管理客户")
    seen = set()
    for item in scope:
        if not isinstance(item, dict):
            raise ValueError("会话配置必须包含 jid 与 display_name")
        jid, name = item.get("jid"), item.get("display_name")
        if not isinstance(jid, str) or not _JID.fullmatch(jid) or jid in seen:
            raise ValueError("会话 JID 无效或重复")
        if (not isinstance(name, str) or not name.strip() or len(name) > 120
                or any(ord(char) < 32 for char in name)):
            raise ValueError("会话显示名必须是简短非空文字")
        if not isinstance(item.get("enabled", True), bool):
            raise ValueError("会话启用状态必须是布尔值")
        seen.add(jid)
    if sum(item.get("enabled", True) for item in scope) > MAX_ACTIVE:
        raise ValueError(f"同时更新的客户最多 {MAX_ACTIVE} 位")
    return config


def configure_connection(workspace, messages_db, source_account_id, conversations):
    """CLI-only local setup. Does not open the source or validate account login."""
    workspace = _fresh(workspace)
    path = Path(messages_db).expanduser().absolute()
    config = {"schema_version": 1, "source_type": "whatsapp_bridge", **_binding(workspace),
              "messages_db": str(path), "source_account_id": source_account_id,
              "conversations": conversations}
    _validate_config(workspace, config)
    with _sync_lock(workspace):
        target = workspace.root / CONFIG_FILE
        _mkdir(target.parent)
        if target.exists() or target.is_symlink():
            old = _validate_config(workspace, _read_json(target, 2_000_000))
            if any(old[key] != config[key] for key in ("messages_db", "source_account_id")):
                raise ValueError("已绑定来源路径和账号标识不能原地替换；请为另一来源建立独立工作区")
        _atomic_write(target, _json_bytes(config))
    return {"configured": True, "source_type": "whatsapp_bridge", "source_label": _SOURCE_LABEL,
            "authorized_conversations": sum(item.get("enabled", True) for item in conversations), "connection_state": "unknown",
            "history_complete": False, "note": "配置已保存；逻辑账号标识由操作者指定，尚未核实登录身份。"}


def _source(workspace):
    path = workspace.root / CONFIG_FILE
    if path.parent.is_symlink():
        raise ValueError("连接配置目录不可用")
    if path.exists() or path.is_symlink():
        return "whatsapp_bridge", _validate_config(workspace, _read_json(path, 2_000_000))
    labels = demo_labels(workspace)
    if labels:
        return "demo", None
    return "none", None


def _source_id(workspace, kind, config):
    return _hash({**_binding(workspace), "source_type": kind,
                  "account": config["source_account_id"] if config else ACCOUNT_ID,
                  "source": config["messages_db"] if config else "fixed_workbench_update_v1"})


def _load_state(workspace, identity):
    path = workspace.root / STATE_FILE
    if not path.exists() and not path.is_symlink():
        return {"schema_version": 1, **_binding(workspace), "source_id": identity,
                "accepted": {}, "last_result": None, "last_import_success": None}
    value = _read_json(path)
    if (value.get("schema_version") != 1 or value.get("source_id") != identity
            or any(value.get(key) != item for key, item in _binding(workspace).items())
            or not isinstance(value.get("accepted"), dict)):
        raise ValueError("同步状态绑定不一致；请核查原来源配置")
    for key, digest in value["accepted"].items():
        try:
            pair = json.loads(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("同步去重记录不可用") from exc
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(item, str) for item in pair)
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("同步去重记录不可用")
    return value


def sync_status(workspace):
    """Safe UI status: no source paths, message bodies, credentials or ledger."""
    workspace = _fresh(workspace)
    try:
        kind, config = _source(workspace)
        state = _load_state(workspace, _source_id(workspace, kind, config)) if kind != "none" else {}
        scope = _sync_scope(workspace, kind, config)
    except (OSError, ValueError):
        return {"configured": False, "source_type": "none", "source_label": "配置需要检查",
                "connection_state": "unknown", "history_complete": False,
                "last_result": None, "last_import_success": None,
                "note": "本机连接配置或同步记录未通过校验，请由负责人检查。"}
    return {"configured": kind != "none", "source_type": kind,
            "source_label": _DEMO_LABEL if kind == "demo" else _SOURCE_LABEL if config else "尚未配置聊天来源",
            "connection_state": "unknown", "history_complete": False,
            "last_result": state.get("last_result"), "last_import_success": state.get("last_import_success"),
            "authorized_conversations": sum(item.get("enabled", True) for item in scope),
            "active_conversations": sum(item.get("enabled", True) for item in scope),
            "paused_conversations": sum(not item.get("enabled", True) for item in scope),
            "note": "演练更新只添加固定虚构消息，不连接 WhatsApp。" if kind == "demo" else _NOTE}


def conversation_labels(workspace):
    workspace = _fresh(workspace)
    try:
        kind, config = _source(workspace)
    except (OSError, ValueError):
        return {}
    if kind == "demo":
        from .customer_management import demo_scope
        labels = demo_labels(workspace)
        for item in demo_scope(workspace):
            key = (ACCOUNT_ID, item["jid"])
            label = dict(labels.get(key, {"title": item["display_name"], "subtitle": "虚构演练客户"}))
            if not item.get("enabled", True):
                label["subtitle"] += " · 已暂停更新"
            labels[key] = label
        return labels
    if not config:
        return {}
    return {(_account(config), _conversation(item["jid"])):
            {"title": item["display_name"], "subtitle": "WhatsApp · 已配置授权会话" if item.get("enabled", True) else "WhatsApp · 已暂停更新"}
            for item in config["conversations"]}


def _sync_scope(workspace, kind, config):
    if kind == "demo":
        from .customer_management import demo_scope
        return demo_scope(workspace)
    return config["conversations"] if config else []


def _account(config):
    return "whatsapp:bridge:" + config["source_account_id"]


def _conversation(jid):
    return "whatsapp:" + jid


def _refresh_customer_names(workspace, config, names):
    """Persist source names, never local aliases or changes to sync selection.

    Caller holds the workspace sync lock. Changing the config also invalidates
    older scan revisions, so another tab cannot restore a stale name.
    """
    scope = [dict(item, display_name=names.get(item["jid"], item["display_name"]))
             for item in config["conversations"]]
    if scope == config["conversations"]:
        return config
    updated = dict(config, conversations=scope)
    _validate_config(workspace, updated)
    _atomic_write(workspace.root / CONFIG_FILE, _json_bytes(updated))
    return updated


def _time_range(rows, field="timestamp"):
    """UTC range of this snapshot only; unknown timestamps cannot imply coverage."""
    times = []
    invalid = 0
    for row in rows:
        try:
            timestamp = datetime.fromisoformat(row[field].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("timezone missing")
            times.append(timestamp.astimezone(timezone.utc))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            invalid += 1
    return {"source_first_at": min(times).isoformat(timespec="microseconds") if times and not invalid else None,
            "source_last_at": max(times).isoformat(timespec="microseconds") if times and not invalid else None,
            "source_time_invalid_rows": invalid}


def _bridge_snapshot(config):
    scope = [item["jid"] for item in config["conversations"] if item.get("enabled", True)]
    if not scope:
        return [], {"source_rows": 0, "authorized_conversations": 0, "scan_limit": MAX_ROWS,
                    "snapshot_complete": True, "history_complete": False, "late_arrivals_checked": False,
                    "source_first_at": None, "source_last_at": None}, {}
    path = Path(config["messages_db"])
    if path.is_symlink() or not path.is_file():
        raise _SourceFailure("source_unavailable", "无法读取配置的桥接消息库，请检查本机桥接程序和来源配置。")
    marks = ",".join("?" for _ in scope)
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
            required = {"id", "chat_jid", "sender", "content", "timestamp", "is_from_me", "media_type"}
            if not required.issubset(columns):
                raise _SourceFailure("schema_unsupported", "桥接消息库结构不受支持，未导入消息。")
            stats = connection.execute(
                f"SELECT COUNT(*),COALESCE(SUM(length(content)),0) FROM messages WHERE chat_jid IN ({marks})", scope).fetchone()
            coverage = {"source_rows": stats[0], "authorized_conversations": len(scope),
                        "scan_limit": MAX_ROWS, "snapshot_complete": False,
                        "history_complete": False, "late_arrivals_checked": False,
                        "source_first_at": None, "source_last_at": None}
            if stats[0] > MAX_ROWS or stats[1] > MAX_CONTENT_CHARS:
                raise _SourceFailure("scan_limit_exceeded", "授权会话的本地记录超过本次读取上限；未导入，请缩小会话范围后重试。", coverage)
            rows = list(connection.execute(
                f"SELECT id,chat_jid,sender,content,timestamp,is_from_me,media_type FROM messages "
                f"WHERE chat_jid IN ({marks}) ORDER BY chat_jid,id", scope))
            names = {}
            chat_columns = {row[1] for row in connection.execute("PRAGMA table_info(chats)")}
            if {"jid", "name"}.issubset(chat_columns):
                names = dict(connection.execute(f"SELECT jid,name FROM chats WHERE jid IN ({marks})", scope))
            connection.rollback()
        from .whatsapp_names import resolve_names
        names = resolve_names(config, names,
                              previous_names={row["jid"]: row["display_name"] for row in config["conversations"]})
        coverage.update(snapshot_complete=True, late_arrivals_checked=True, **_time_range(rows))
        return [dict(row) for row in rows], coverage, names
    except sqlite3.Error as exc:
        raise _SourceFailure("source_read_failed", "桥接消息库暂时不可读，未确认本次更新完成。") from exc


def _bridge_message(workspace, config, row, received):
    if (not isinstance(row["id"], str) or not row["id"].strip() or len(row["id"]) > 256
            or row["is_from_me"] not in (0, 1)
            or not isinstance(row["content"], (str, type(None)))
            or not isinstance(row["media_type"], (str, type(None)))):
        raise ValueError("来源记录格式异常")
    content = row["content"] or ""
    media = row["media_type"] or ""
    if media:
        label = {"image": "图片", "audio": "语音", "video": "视频", "document": "文件"}.get(media, "附件")
        content = content + ("\n" if content else "") + f"[附件尚未解析：{label}；未下载，文字仅为原消息附文]"
    sent = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
    if sent.tzinfo is None or sent.utcoffset() is None:
        raise ValueError("来源时间缺少时区")
    record = normalize_message({"project_id": workspace.company_id, "mode": workspace.mode,
              "account_id": _account(config), "conversation_id": _conversation(row["chat_jid"]),
              "message_id": "whatsapp:" + row["id"],
              "direction": "outbound" if row["is_from_me"] else "inbound", "body": content,
              "sent_at": sent.astimezone(timezone.utc).isoformat(timespec="microseconds"), "received_at": received})
    key = json.dumps([row["chat_jid"], row["id"]], ensure_ascii=False, separators=(",", ":"))
    digest = _hash({"sender": row["sender"], "message": message_payload(record)})
    return key, digest, record


def _demo_records(workspace):
    messages = [
        ("new-paper", "wb-manual-update-v1-paper", "The supplier confirmed 360 kg gross and 2.4 CBM for the 60 cartons. We would prefer arrival before 20 November 2026."),
        ("details-needed", "wb-manual-update-v1-details", "We are considering cotton tote bags from a supplier in Guangzhou, for delivery to Barcelona. Could you tell me which packing details to ask the supplier for?"),
    ]
    records = [{"project_id": workspace.company_id, "mode": "simulation", "account_id": ACCOUNT_ID,
             "conversation_id": "workbench:" + slug, "message_id": identity, "direction": "inbound", "body": body,
             "sent_at": f"2026-09-25T02:0{index}:00Z", "received_at": f"2026-09-25T02:0{index}:01Z"}
            for index, (slug, identity, body) in enumerate(messages)]
    from .customer_management import demo_new_messages, demo_scope
    active = {item["jid"] for item in demo_scope(workspace) if item.get("enabled", True)}
    return [row for row in records + demo_new_messages(workspace) if row["conversation_id"] in active]


def _existing_payloads(workspace, records):
    store = workspace.open_store()
    try:
        result = {}
        for record in records:
            key = (record["account_id"], record["conversation_id"], record["message_id"])
            row = store.connection.execute("SELECT * FROM messages WHERE project_id=? AND account_id=? AND conversation_id=? AND message_id=?",
                                           (workspace.company_id, *key)).fetchone()
            if row:
                result[key] = message_payload(dict(row))
        return result
    finally:
        store.close()


def refresh_messages(workspace):
    """Manually import one consistent full allowlisted snapshot, never a watermark.

    Returns succeeded/partial with committed counts. Busy/missing/invalid setup
    raises ValueError. Read failures are partial with zero imports, never success.
    """
    workspace = _fresh(workspace)
    with _sync_lock(workspace):
        kind, config = _source(workspace)
        if kind == "none":
            raise ValueError("尚未配置聊天来源；请先由负责人设置本机 WhatsApp 会话范围")
        state = _load_state(workspace, _source_id(workspace, kind, config))
        scope = _sync_scope(workspace, kind, config)
        active_count = sum(item.get("enabled", True) for item in scope)
        started = _now()
        result = {"status": "succeeded", "inserted": 0, "duplicates": 0, "conflicts": 0,
                  "affected_conversations": [], "started_at": started, "finished_at": None,
                  "source_type": kind, "coverage": {"snapshot_complete": False, "history_complete": False,
                                                       "source_first_at": None, "source_last_at": None},
                  "note": "已读取固定虚构消息；没有连接 WhatsApp。" if kind == "demo" else _NOTE}
        if not active_count:
            result["note"] = "当前没有启用更新的客户，请在添加客户中扫描并选择；已有聊天仍保留。"
        accepted = dict(state["accepted"])
        affected = set()
        conflict_records = []

        def conflict(record, reason):
            result["conflicts"] += 1
            # IDs and reasons only; never persist original/changed message bodies.
            conflict_records.append({"conversation_id": record.get("conversation_id"),
                                     "message_id": record.get("message_id"), "reason": reason})

        try:
            if kind == "demo":
                entries = [(json.dumps([row["conversation_id"], row["message_id"]]), _hash(message_payload(row)), row)
                           for row in _demo_records(workspace)]
                result["coverage"].update(source_rows=len(entries), authorized_conversations=active_count,
                                           snapshot_complete=True, late_arrivals_checked=True,
                                           **_time_range([entry[2] for entry in entries], "sent_at"))
            else:
                rows, result["coverage"], names = _bridge_snapshot(config)
                config = _refresh_customer_names(workspace, config, names)
                entries, source_keys = [], set()
                for row in rows:
                    key = json.dumps([row["chat_jid"], row["id"]], ensure_ascii=False, separators=(",", ":"))
                    source_keys.add(key)
                    try:
                        entries.append(_bridge_message(workspace, config, row, started))
                    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                        conflict({"conversation_id": _conversation(row["chat_jid"]),
                                  "message_id": "whatsapp:" + str(row["id"])}, "invalid_source_record")
                # A missing previously imported ID can mean deletion/revocation or
                # source retention. Preserve history and flag for manual review.
                allowed = {item["jid"] for item in config["conversations"] if item.get("enabled", True)}
                missing = 0
                for key in accepted:
                    chat, identity = json.loads(key)
                    if chat in allowed and key not in source_keys:
                        missing += 1
                        conflict({"conversation_id": _conversation(chat), "message_id": "whatsapp:" + identity},
                                 "previous_source_record_missing")
                result["coverage"]["missing_previous_records"] = missing
            prior = _existing_payloads(workspace, [entry[2] for entry in entries])
            eligible = []
            for key, digest, record in entries:
                scope = (record["account_id"], record["conversation_id"], record["message_id"])
                if ((key in accepted and accepted[key] != digest)
                        or (scope in prior and prior[scope] != message_payload(record))):
                    conflict(record, "source_identity_changed")
                else:
                    eligible.append((key, digest, record))
            label = _DEMO_LABEL if kind == "demo" else _SOURCE_LABEL

            def import_batch(batch):
                records = [entry[2] for entry in batch]
                try:
                    counts = workspace.import_messages(records, label)
                except StoreError:
                    if len(batch) == 1:
                        conflict(batch[0][2], "concurrent_identity_conflict")
                        return
                    for entry in batch:
                        import_batch([entry])
                    return
                result["inserted"] += counts["inserted"]
                result["duplicates"] += counts["duplicates"]
                for key, digest, record in batch:
                    accepted[key] = digest
                    scope = (record["account_id"], record["conversation_id"], record["message_id"])
                    if counts["inserted"] and scope not in prior:
                        affected.add(scope[:2])

            for index in range(0, len(eligible), BATCH_SIZE):
                import_batch(eligible[index:index + BATCH_SIZE])
            if result["conflicts"]:
                result.update(status="partial", error_code="source_conflicts",
                              note="发现内容变化、缺失或格式异常的来源记录；已跳过并保留原记录，其余正常消息已导入。" +
                              (" 此来源为固定虚构演练。" if kind == "demo" else _NOTE))
        except _SourceFailure as exc:
            result.update(status="partial", error_code=exc.code, note=str(exc))
            result["coverage"].update(exc.coverage)
        except (OSError, sqlite3.Error, ValueError):
            result.update(status="partial", error_code="import_failed",
                          note="本次更新未全部完成；下列数量仅表示已经保存的消息，可修复问题后重试。")
        result["finished_at"] = _now()
        result["coverage"]["import_complete"] = result["status"] == "succeeded"
        result["affected_conversations"] = [{"account_id": account, "conversation_id": conversation}
                                              for account, conversation in sorted(affected)]
        state.update(accepted=accepted, last_result=result, last_conflicts=conflict_records)
        if result["status"] == "succeeded":
            state["last_import_success"] = result["finished_at"]
        _atomic_write(workspace.root / STATE_FILE, _json_bytes(state))
        return result
