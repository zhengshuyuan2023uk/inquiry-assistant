"""Repeatable, metadata-only customer discovery and explicit sync selection.

The only real source is the administrator-bound local bridge database. Scans
never read message bodies. Opaque scan tokens prevent saving stale selections;
manual sync and customer management share one workspace lock.
"""
from collections import OrderedDict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import secrets
import sqlite3

from . import manual_sync as sync
from .web_demo import ACCOUNT_ID, conversation_labels as seed_labels
from .workspace import _atomic_write, _json_bytes, _mkdir

SCAN_FILE = "connections/customer-scan.json"
DEMO_FILE = "connections/demo-customers.json"
MAX_SELECTED = 100
MAX_CANDIDATES = 2000
MAX_METADATA_CHARS = 2_000_000
SCAN_TTL_SECONDS = 15 * 60
# Tokens authorize only scans completed by this running process. Revoking in
# memory also closes a disk-failure gap before the invalidation write succeeds.
_ACTIVE_SCANS = OrderedDict()
_MAX_ACTIVE_SCANS = 128
_DEMO_NEW = [
    {"jid": "workbench:new-textiles", "display_name": "Clara Finch（虚构）",
     "body": "Hello, could you help check sea freight for 30 cartons of cotton towels from Qingdao to Hamburg? I am waiting for the gross weight and packing dimensions."},
    {"jid": "workbench:new-furniture", "display_name": "Oscar Bell（虚构）",
     "body": "Hi, we are planning to ship flat-packed wooden chairs from Foshan to Sydney. Which shipment details do you need to check the available options?"},
]
_NOTE = "扫描只读取本机桥接已接收会话的名称、标识和最后消息时间；保存勾选后，再点更新聊天导入正文。无法确认 WhatsApp 在线或历史完整。"
_DEMO_NOTE = "固定虚构客户供反复练习；勾选保存后，再点更新聊天导入对应演练消息。未连接 WhatsApp。"


class CustomerConflictError(ValueError):
    """The user must rescan before replacing the active customer selection."""


def _name(value, fallback):
    if not isinstance(value, str):
        return fallback
    value = " ".join("".join(c for c in value if ord(c) >= 32 and ord(c) != 127).split())
    return value[:120] or fallback


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None or moment.utcoffset() is None:
            return None
        return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        return None


def demo_scope(workspace):
    """Saved synthetic sync list, or the five original demo customers."""
    labels = seed_labels(workspace)
    if not labels:
        raise ValueError("演练来源未通过校验")
    path = workspace.root / DEMO_FILE
    if path.parent.is_symlink():
        raise ValueError("演练客户配置目录不可用")
    if not path.exists() and not path.is_symlink():
        return [{"jid": key[1], "display_name": value["title"], "enabled": True}
                for key, value in labels.items()]
    value = sync._read_json(path, 2_000_000)
    if (value.get("schema_version") != 1
            or any(value.get(k) != v for k, v in sync._binding(workspace).items())):
        raise ValueError("演练客户配置与工作区不一致")
    allowed = {key[1] for key in labels} | {item["jid"] for item in _DEMO_NEW}
    scope = value.get("conversations")
    if not isinstance(scope, list) or len(scope) > sync.MAX_MANAGED:
        raise ValueError("演练客户配置不可用")
    seen = set()
    for item in scope:
        if (not isinstance(item, dict) or item.get("jid") not in allowed or item["jid"] in seen
                or not isinstance(item.get("enabled", True), bool)
                or not isinstance(item.get("display_name"), str)
                or _name(item["display_name"], "") != item["display_name"] or not item["display_name"]):
            raise ValueError("演练客户配置不可用")
        seen.add(item["jid"])
    if sum(item.get("enabled", True) for item in scope) > MAX_SELECTED:
        raise ValueError("已选客户超过上限")
    return scope


def demo_new_messages(workspace):
    return [{"project_id": workspace.company_id, "mode": "simulation", "account_id": ACCOUNT_ID,
             "conversation_id": item["jid"], "message_id": "customer-scan-v1-" + item["jid"],
             "direction": "inbound", "body": item["body"], "sent_at": "2026-09-25T03:00:00Z",
             "received_at": "2026-09-25T03:00:01Z"} for item in _DEMO_NEW]


def _managed(workspace, kind, config):
    return config["conversations"] if kind == "whatsapp_bridge" else demo_scope(workspace) if kind == "demo" else []


def _revision(workspace, kind, config):
    if kind == "demo":
        path = workspace.root / DEMO_FILE
        data = sync._read_json(path, 2_000_000) if path.exists() or path.is_symlink() else demo_scope(workspace)
    else:
        data = config
    return sync._hash({"workspace": str(workspace.root.resolve()), "source_type": kind,
                       "binding": sync._binding(workspace), "config": data})


def _identity(workspace, kind, config, identifier):
    return sync._hash({"source": sync._source_id(workspace, kind, config), "identifier": identifier})


def _scan_cache(workspace, kind, config):
    path = workspace.root / SCAN_FILE
    if path.parent.is_symlink():
        raise ValueError("客户扫描目录不可用")
    if not path.exists() and not path.is_symlink():
        return None
    value = sync._read_json(path, 4_000_000)
    if (value.get("schema_version") != 1 or value.get("revision") != _revision(workspace, kind, config)
            or not isinstance(value.get("scan_id"), str) or not value["scan_id"]
            or _ACTIVE_SCANS.get(str(workspace.root.resolve())) != value["scan_id"]):
        return None
    scanned_at = _timestamp(value.get("scanned_at"))
    if not scanned_at:
        raise ValueError("客户扫描时间不可用，请重新扫描")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(scanned_at)).total_seconds()
    if age < 0 or age > SCAN_TTL_SECONDS:
        return None
    if not isinstance(value.get("items"), list) or len(value["items"]) > MAX_CANDIDATES:
        raise ValueError("客户扫描记录不可用，请重新扫描")
    allowed_demo = ({key[1] for key in seed_labels(workspace)} | {item["jid"] for item in _DEMO_NEW}) if kind == "demo" else set()
    seen = set()
    for item in value["items"]:
        if (not isinstance(item, dict) or set(item) != {"identifier", "display_name", "available", "last_message_at"}
                or not isinstance(item["identifier"], str) or item["identifier"] in seen
                or not (sync._JID.fullmatch(item["identifier"]) if kind == "whatsapp_bridge" else item["identifier"] in allowed_demo)
                or not isinstance(item["display_name"], str) or not item["display_name"]
                or _name(item["display_name"], "") != item["display_name"]
                or not isinstance(item["available"], bool)
                or (item["last_message_at"] is not None and _timestamp(item["last_message_at"]) is None)):
            raise ValueError("客户扫描记录不可用，请重新扫描")
        seen.add(item["identifier"])
    return value


def _view(workspace, kind, config, cache=None, managed_scope=None):
    scope = _managed(workspace, kind, config) if managed_scope is None else managed_scope
    managed = {item["jid"]: item for item in scope}
    items = {item["identifier"]: dict(item) for item in cache["items"]} if cache else {}
    for identifier, saved in managed.items():
        items.setdefault(identifier, {"identifier": identifier, "available": False, "last_message_at": None})
    for identifier, item in items.items():
        saved = managed.get(identifier)
        selected = bool(saved and saved.get("enabled", True))
        item.update(id=_identity(workspace, kind, config, identifier),
                    display_name=saved["display_name"] if saved else item["display_name"],
                    kind="group" if identifier.endswith("@g.us") else "individual",
                    status="active" if selected else "paused" if saved else "new", selected=selected)
    return {"configured": kind != "none", "source_type": kind,
            "source_label": sync._DEMO_LABEL if kind == "demo" else sync._SOURCE_LABEL if kind == "whatsapp_bridge" else "尚未配置聊天来源",
            "connection_state": "unknown", "note": _DEMO_NOTE if kind == "demo" else _NOTE if kind != "none" else "尚未配置聊天来源，请由负责人先连接本机 WhatsApp 来源。",
            "scan_id": cache["scan_id"] if cache else None, "scanned_at": cache["scanned_at"] if cache else None,
            "items": sorted(items.values(), key=lambda item: (item["display_name"].casefold(), item["identifier"])),
            "selected_count": sum(row["selected"] for row in items.values()), "managed_count": len(managed),
            "max_selected": MAX_SELECTED, "max_candidates": MAX_CANDIDATES}


def customer_status(workspace):
    """Return last scan and current selection; never open a bridge database."""
    workspace = sync._fresh(workspace)
    with sync._sync_lock(workspace):
        try:
            kind, config = sync._source(workspace)
            return _view(workspace, kind, config, _scan_cache(workspace, kind, config))
        except (OSError, ValueError):
            result = _view(workspace, "none", None)
            result.update(source_label="配置需要检查", note="本机客户配置或扫描记录未通过校验，请由负责人检查。")
            return result


def _bridge_metadata(config):
    path = Path(config["messages_db"])
    if path.is_symlink() or not path.is_file():
        raise ValueError("无法读取配置的桥接消息库，请检查本机桥接程序。")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(chats)")}
            if not {"jid", "name", "last_message_time"}.issubset(columns):
                raise ValueError("桥接会话库结构不受支持，未扫描客户。")
            count, chars = connection.execute("SELECT COUNT(*), COALESCE(SUM(length(jid)+COALESCE(length(name),0)+COALESCE(length(last_message_time),0)),0) FROM chats").fetchone()
            if count > MAX_CANDIDATES or chars > MAX_METADATA_CHARS:
                raise ValueError(f"桥接会话超过扫描上限（{MAX_CANDIDATES} 条或元数据容量上限），请由负责人处理；未截断列表。")
            rows = list(connection.execute("SELECT jid,name,last_message_time FROM chats ORDER BY jid,name,last_message_time"))
            connection.rollback()
    except sqlite3.Error as exc:
        raise ValueError("桥接会话库暂时不可读，扫描未完成，请稍后重试。") from exc
    merged = {}
    for jid, name, timestamp in rows:
        if not isinstance(jid, str) or not sync._JID.fullmatch(jid):
            continue  # Status/broadcast/service identifiers are not customer chats.
        item = {"identifier": jid, "display_name": _name(name, jid), "available": True,
                "last_message_at": _timestamp(timestamp)}
        previous = merged.get(jid)
        if previous is None or (item["last_message_at"] or "") > (previous["last_message_at"] or ""):
            merged[jid] = item
    from .whatsapp_names import resolve_names
    names = resolve_names(config, {jid: row["display_name"] for jid, row in merged.items()},
                          previous_names={row["jid"]: row["display_name"] for row in config["conversations"]})
    return [dict(row, display_name=names[jid]) for jid, row in merged.items()]


def scan_customers(workspace):
    """Refresh all candidate metadata, issuing a fresh workspace-bound token."""
    workspace = sync._fresh(workspace)
    with sync._sync_lock(workspace):
        scan_key = str(workspace.root.resolve())
        _ACTIVE_SCANS.pop(scan_key, None)
        kind, config = sync._source(workspace)
        if kind == "none":
            raise ValueError("尚未配置聊天来源，请由负责人先连接本机 WhatsApp 来源。")
        revision = _revision(workspace, kind, config)
        target = workspace.root / SCAN_FILE
        _mkdir(target.parent)
        # A failed rescan must not leave an earlier selection token saveable.
        _atomic_write(target, _json_bytes({"schema_version": 1, "revision": revision, "scan_id": None, "items": []}))
        if kind == "whatsapp_bridge":
            items = _bridge_metadata(config)
        else:
            items = [{"identifier": key[1], "display_name": value["title"], "available": True,
                      "last_message_at": None} for key, value in seed_labels(workspace).items()]
            items += [{"identifier": row["jid"], "display_name": row["display_name"], "available": True,
                       "last_message_at": "2026-09-25T03:00:00.000000+00:00"} for row in _DEMO_NEW]
        identifiers = {row["identifier"] for row in items}
        for row in _managed(workspace, kind, config):
            if row["jid"] not in identifiers:
                items.append({"identifier": row["jid"], "display_name": row["display_name"],
                              "available": False, "last_message_at": None})
        if len(items) > MAX_CANDIDATES:
            raise ValueError(f"候选及已管理客户超过 {MAX_CANDIDATES} 条扫描上限；未截断列表。")
        if kind == "whatsapp_bridge":
            config = sync._refresh_customer_names(workspace, config,
                        {row["identifier"]: row["display_name"] for row in items if row["available"]})
            revision = _revision(workspace, kind, config)
        cache = {"schema_version": 1, "revision": revision, "scan_id": secrets.token_urlsafe(24),
                 "scanned_at": sync._now(), "items": items}
        _atomic_write(target, _json_bytes(cache))
        _ACTIVE_SCANS[scan_key] = cache["scan_id"]
        while len(_ACTIVE_SCANS) > _MAX_ACTIVE_SCANS:
            _ACTIVE_SCANS.popitem(last=False)
        return _view(workspace, kind, config, cache)


def save_customers(workspace, scan_id, selected_ids):
    """Replace the active set atomically. Unchecked managed rows become paused."""
    workspace = sync._fresh(workspace)
    with sync._sync_lock(workspace):
        kind, config = sync._source(workspace)
        if kind == "none":
            raise ValueError("尚未配置聊天来源，请由负责人先连接本机 WhatsApp 来源。")
        if (not isinstance(selected_ids, list) or len(selected_ids) > MAX_SELECTED
                or not all(isinstance(item, str) for item in selected_ids)
                or len(set(selected_ids)) != len(selected_ids)):
            raise ValueError(f"请提交不重复的客户勾选列表，最多 {MAX_SELECTED} 位。")
        cache = _scan_cache(workspace, kind, config)
        if not cache or not isinstance(scan_id, str) or scan_id != cache["scan_id"]:
            raise CustomerConflictError("客户扫描或名单已改变，请重新扫描后再保存。")
        view = _view(workspace, kind, config, cache)
        issued = {row["id"]: row for row in view["items"]}
        if not set(selected_ids).issubset(issued):
            raise ValueError("勾选包含本次扫描未提供的客户，请重新扫描。")
        selected = {issued[item]["identifier"] for item in selected_ids}
        managed = {row["jid"]: dict(row) for row in _managed(workspace, kind, config)}
        for identifier, row in managed.items():
            row["enabled"] = identifier in selected
        for identity in selected_ids:
            row = issued[identity]
            managed.setdefault(row["identifier"], {"jid": row["identifier"], "display_name": row["display_name"], "enabled": True})
        if len(managed) > sync.MAX_MANAGED:
            raise ValueError("已管理客户超过保存上限，请由负责人处理。")
        updated = dict(config) if config else {"schema_version": 1, **sync._binding(workspace)}
        updated.update(conversations=list(managed.values()), customer_revision=secrets.token_hex(16))
        if kind == "whatsapp_bridge":
            sync._validate_config(workspace, updated)
        # Prepare the response before committing; a readback failure must not
        # turn an already-saved selection into an apparent failed operation.
        current_config = updated if kind == "whatsapp_bridge" else None
        result = _view(workspace, kind, current_config, cache, list(managed.values()))
        result["scan_id"] = None
        target = workspace.root / (sync.CONFIG_FILE if kind == "whatsapp_bridge" else DEMO_FILE)
        try:
            _mkdir(target.parent)
            _atomic_write(target, _json_bytes(updated))
        except OSError as exc:
            _ACTIVE_SCANS.pop(str(workspace.root.resolve()), None)
            # A durability error can occur after os.replace has committed.
            raise ValueError("保存结果暂无法确认，请重新打开客户管理核对名单；若仍有问题，请由负责人检查。") from exc
        _ACTIVE_SCANS.pop(str(workspace.root.resolve()), None)
        return result
