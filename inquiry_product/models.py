"""Read the executing CLI's available model catalog without creating a turn.

Only initialize and model/list are sent over a short-lived stdio app server.
No prompts, source messages, session files, or credential files are read here.
Do not substitute another CLI version's models_cache.json when this fails.
"""
from copy import deepcopy
import json
import os
import select
import subprocess
import tempfile
import threading
import time

from .core.engine import MODEL_SLUG, _CODEX, _execution_environment


_QUERY_TIMEOUT_SECONDS = 10
_CACHE_SECONDS = 300
_ERROR_CACHE_SECONDS = 1
_MAX_OUTPUT_BYTES = 2_000_000
_MAX_PAGES = 5
_cache_lock = threading.Lock()
_cache = None
_cache_until = 0


class _CatalogError(ValueError):
    pass


class _RpcReader:
    """Read bounded JSONL without blocking on partial or buffered lines."""

    def __init__(self, process, deadline):
        self.process = process
        self.deadline = deadline
        self.buffer = b""
        self.total = 0

    def receive(self, identity):
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise _CatalogError("AI 配置目录返回格式异常") from exc
                if not isinstance(value, dict) or value.get("id") != identity:
                    continue
                if "error" in value or "result" not in value:
                    raise _CatalogError("AI 服务未能返回可用配置，请联系负责人检查服务状态")
                return value["result"]
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise _CatalogError("读取 AI 配置目录超时，请稍后重试")
            ready, _, _ = select.select([self.process.stdout], [], [], min(remaining, 0.25))
            if not ready:
                if self.process.poll() is not None:
                    raise _CatalogError("AI 配置目录服务已退出，请联系负责人检查服务状态")
                continue
            chunk = os.read(self.process.stdout.fileno(), 65_536)
            if not chunk:
                raise _CatalogError("AI 配置目录连接已关闭")
            self.total += len(chunk)
            if self.total > _MAX_OUTPUT_BYTES:
                raise _CatalogError("AI 配置目录超过读取上限")
            self.buffer += chunk


def _send(process, value):
    process.stdin.write(json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n")
    process.stdin.flush()


def _public_models(rows):
    if not isinstance(rows, list):
        raise _CatalogError("AI 配置目录返回格式异常")
    models, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise _CatalogError("AI 配置目录返回格式异常")
        if row.get("hidden") is True or row.get("visibility") == "hide":
            continue
        identity = row.get("model", row.get("id"))
        if not isinstance(identity, str) or not MODEL_SLUG.fullmatch(identity):
            raise _CatalogError("AI 配置目录包含不支持的模型标识")
        if identity in seen:
            continue
        seen.add(identity)
        efforts = row.get("supportedReasoningEfforts", [])
        if not isinstance(efforts, list):
            raise _CatalogError("AI 推理选项返回格式异常")
        values = []
        for item in efforts:
            effort = item.get("reasoningEffort") if isinstance(item, dict) else None
            if not isinstance(effort, str) or not MODEL_SLUG.fullmatch(effort):
                raise _CatalogError("AI 推理选项返回格式异常")
            if effort not in values:
                values.append(effort)
        name = row.get("displayName", identity)
        default_effort = row.get("defaultReasoningEffort")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise _CatalogError("AI 型号名称返回格式异常")
        if default_effort is not None and (not isinstance(default_effort, str)
                                            or default_effort not in values):
            raise _CatalogError("AI 默认推理选项不匹配")
        models.append({"id": identity, "name": name, "default": row.get("isDefault") is True,
                       "reasoning_efforts": values, "default_reasoning_effort": default_effort})
    if not models:
        raise _CatalogError("AI 服务暂未返回可用型号，请稍后重试或联系负责人")
    return models


def _query_catalog():
    deadline = time.monotonic() + _QUERY_TIMEOUT_SECONDS
    # A clean cwd avoids treating a customer workspace as app-server context.
    with tempfile.TemporaryDirectory(prefix="inquiry-model-catalog-") as directory:
        process = subprocess.Popen([_CODEX, "app-server", "--stdio"], cwd=directory,
                                   env=_execution_environment(), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   bufsize=0)
        try:
            reader = _RpcReader(process, deadline)
            _send(process, {"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "inquiry_model_catalog", "version": "1.0"},
                "capabilities": {"experimentalApi": True}}})
            reader.receive(1)
            _send(process, {"method": "initialized"})
            rows, cursor, seen_cursors = [], None, set()
            for page in range(_MAX_PAGES):
                params = {"limit": 100, "includeHidden": False}
                if cursor is not None:
                    params["cursor"] = cursor
                identity = page + 2
                _send(process, {"id": identity, "method": "model/list", "params": params})
                result = reader.receive(identity)
                if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise _CatalogError("AI 配置目录返回格式异常")
                rows.extend(result["data"])
                cursor = result.get("nextCursor")
                if cursor is None:
                    return _public_models(rows)
                if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                    raise _CatalogError("AI 配置目录分页返回异常")
                seen_cursors.add(cursor)
            raise _CatalogError("AI 配置目录超过分页读取上限")
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            process.stdin.close()
            process.stdout.close()


def get_model_catalog():
    """Return a cached catalog, or an empty list with a safe actionable error.

    Successful metadata is cached for five minutes, failures for one second.
    Concurrent callers share one lookup. A failed refresh never claims stale
    account/model data is available, and no inference is performed.
    """
    global _cache, _cache_until
    with _cache_lock:
        if _cache is not None and time.monotonic() < _cache_until:
            return deepcopy(_cache)
        try:
            value = {"models": _query_catalog(), "source": "codex_cli"}
        except _CatalogError as exc:
            value = {"models": [], "source": "codex_cli", "error": str(exc)}
        except (OSError, ValueError, subprocess.SubprocessError):
            value = {"models": [], "source": "codex_cli",
                     "error": "AI 服务配置暂不可用，请联系负责人检查连接和授权"}
        _cache = deepcopy(value)
        _cache_until = time.monotonic() + (_ERROR_CACHE_SECONDS if "error" in value else _CACHE_SECONDS)
        return deepcopy(value)
