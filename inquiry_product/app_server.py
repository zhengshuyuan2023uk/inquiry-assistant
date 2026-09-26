"""Reusable, context-only Codex app-server transport.

Protocol fields were checked against local Codex 0.145 generate-json-schema.
Each operation gets an ephemeral thread and empty working directory. The server
uses an isolated CODEX_HOME containing only a symlink to the selected login's
auth.json; this module never reads, prints, or copies credential contents. Tool
entrypoints and inherited environments are disabled, not claimed to form a
complete security sandbox. A failed operation is never retried through exec.
"""
from collections import deque
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time

from . import __version__
from .core.engine import (
    EngineError, _CODEX, _DISABLED_FEATURES, _MAX_OUTPUT_BYTES,
    _current_context, _execution_environment, _expand_polish_result,
    _failure_category, _reject_constant, _result_schema, _unique_json_object,
    build_prompt, validate_result,
)


_MAX_PROTOCOL_BYTES = 8_000_000
_POLL_SECONDS = 0.05
_INTERRUPT_SECONDS = 2.0
_SAFE_ITEM_TYPES = {"userMessage", "agentMessage", "reasoning"}
_STATUS = {
    "connect": "正在连接本机模型服务。",
    "prepare": "正在准备本次独立会话。",
    "generate": "正在生成回复。",
    "validate": "正在核对回复结构与引用。",
    "cancel": "正在取消本次生成。",
    "retry": "模型服务正在恢复连接。",
}


def _emit(callback, event):
    if callback is not None:
        try:
            callback(event)
        except Exception:
            # A disconnected UI must not start a replacement inference.
            pass


def _status(callback, name):
    _emit(callback, {"type": "status", "message": _STATUS[name]})


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise EngineError("模型服务返回了无效的任务标识", "protocol")
    return value


def _remote_category(error):
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    known = {"usageLimitExceeded": "rate_limit", "sessionBudgetExceeded": "rate_limit",
             "unauthorized": "auth", "contextWindowExceeded": "schema",
             "badRequest": "config", "sandboxError": "config",
             "serverOverloaded": "network", "internalServerError": "network"}
    if isinstance(info, str) and info in known:
        return known[info]
    if isinstance(info, dict) and any(key in info for key in (
        "httpConnectionFailed", "responseStreamConnectionFailed",
        "responseStreamDisconnected", "responseTooManyFailedAttempts",
    )):
        return "network"
    category = _failure_category(json.dumps(error, ensure_ascii=True), "")
    if category == "unknown" and isinstance(error, dict) and error.get("code") in (-32600, -32601, -32602):
        return "config"
    return category


class ReplyJsonStream:
    """Incrementally decode only a top-level JSON reply string.

    Nested values are skipped using a JSON decoder. String escape state is kept
    between chunks, including UTF-16 surrogate pairs. Other fields, commentary,
    and unfinished escape sequences never become display text. Final validation
    remains mandatory: a streamed prefix is not an approved result.
    """

    def __init__(self):
        self.buffer = ""
        self.position = 0
        self.state = "root"
        self.key = None
        self.seen = set()
        self.escape = False
        self.unicode_digits = None
        self.high_surrogate = None
        self.decoder = json.JSONDecoder(object_pairs_hook=_unique_json_object, parse_constant=_reject_constant)

    def _character(self, character, output):
        code = ord(character)
        if self.high_surrogate is not None:
            if not 0xDC00 <= code <= 0xDFFF:
                raise EngineError("回复包含不完整的 Unicode 字符", "schema")
            code = 0x10000 + ((self.high_surrogate - 0xD800) << 10) + code - 0xDC00
            self.high_surrogate = None
            output.append(chr(code))
        elif 0xD800 <= code <= 0xDBFF:
            self.high_surrogate = code
        elif 0xDC00 <= code <= 0xDFFF:
            raise EngineError("回复包含无效的 Unicode 字符", "schema")
        else:
            output.append(character)

    def feed(self, text):
        if not isinstance(text, str):
            raise EngineError("回复增量不是文本", "protocol")
        self.buffer += text
        output = []
        while self.position < len(self.buffer):
            char = self.buffer[self.position]
            if self.state in ("ignored", "done"):
                break
            if self.state == "reply":
                self.position += 1
                if self.unicode_digits is not None:
                    if char not in "0123456789abcdefABCDEF":
                        raise EngineError("回复包含无效的 JSON 转义", "schema")
                    self.unicode_digits += char
                    if len(self.unicode_digits) == 4:
                        self._character(chr(int(self.unicode_digits, 16)), output)
                        self.unicode_digits = None
                elif self.escape:
                    self.escape = False
                    if char == "u":
                        self.unicode_digits = ""
                    elif char in '"\\/bfnrt':
                        self._character({'"': '"', "\\": "\\", "/": "/", "b": "\b",
                                         "f": "\f", "n": "\n", "r": "\r", "t": "\t"}[char], output)
                    else:
                        raise EngineError("回复包含无效的 JSON 转义", "schema")
                elif char == "\\":
                    self.escape = True
                elif char == '"':
                    if self.high_surrogate is not None:
                        raise EngineError("回复包含不完整的 Unicode 字符", "schema")
                    self.state = "separator"
                elif ord(char) < 0x20:
                    raise EngineError("回复包含无效的 JSON 控制字符", "schema")
                else:
                    self._character(char, output)
                continue
            if char.isspace():
                self.position += 1
                continue
            if self.state == "root":
                self.state = "key_or_end" if char == "{" else "ignored"
                self.position += 1
            elif self.state in ("key_or_end", "key"):
                if char == "}" and self.state == "key_or_end":
                    self.state = "done"
                    self.position += 1
                    continue
                if char != '"':
                    raise EngineError("回复 JSON 对象字段无效", "schema")
                try:
                    key, end = self.decoder.raw_decode(self.buffer, self.position)
                except json.JSONDecodeError:
                    break
                if key in self.seen:
                    raise EngineError("回复 JSON 包含重复字段", "schema")
                self.seen.add(key)
                self.key, self.position, self.state = key, end, "colon"
            elif self.state == "colon":
                if char != ":":
                    raise EngineError("回复 JSON 字段缺少分隔符", "schema")
                self.position += 1
                self.state = "value"
            elif self.state == "value":
                if self.key == "reply":
                    if char != '"':
                        raise EngineError("reply 必须是 JSON 字符串", "schema")
                    self.position += 1
                    self.state = "reply"
                else:
                    try:
                        _, end = self.decoder.raw_decode(self.buffer, self.position)
                    except json.JSONDecodeError:
                        break
                    if end == len(self.buffer):
                        break  # a split numeric/literal token may continue
                    self.position, self.state = end, "separator"
            elif self.state == "separator":
                if char == ",":
                    self.state = "key"
                elif char == "}":
                    self.state = "done"
                else:
                    raise EngineError("回复 JSON 对象分隔符无效", "schema")
                self.position += 1
        return "".join(output)


class _TurnOutput:
    def __init__(self, callback):
        self.callback = callback
        self.thread_id = None
        self.turn_id = None
        self.pending = []
        self.pending_bytes = 0
        self.items = {}
        self.output_bytes = 0
        self.streamed_item = None
        self.completed = False
        self.text = None

    def identity(self):
        event = {"type": "identity", "thread_id": self.thread_id}
        if self.turn_id is not None:
            event["turn_id"] = self.turn_id
        _emit(self.callback, event)

    def _item(self, identity):
        identity = _identifier(identity)
        return self.items.setdefault(identity, {"id": identity, "text": "", "phase": None,
                                               "started": False, "complete": False,
                                               "processed": 0, "parser": ReplyJsonStream()})

    def _append(self, item, text):
        if not isinstance(text, str):
            raise EngineError("模型服务返回了非文本回复", "protocol")
        try:
            self.output_bytes += len(text.encode("utf-8"))
        except UnicodeError as exc:
            raise EngineError("模型回复字符编码无效", "schema") from exc
        if self.output_bytes > _MAX_OUTPUT_BYTES:
            raise EngineError("模型回复超出本版输出限额", "schema")
        item["text"] += text
        self._stream(item)

    def _stream(self, item):
        if not item["started"] or item["phase"] == "commentary":
            return
        text = item["text"][item["processed"]:]
        item["processed"] = len(item["text"])
        delta = item["parser"].feed(text)
        if delta:
            if self.streamed_item is not None and self.streamed_item != item["id"]:
                raise EngineError("模型返回了多个回复正文，未保存草稿", "schema")
            self.streamed_item = item["id"]
            _emit(self.callback, {"type": "reply_delta", "text": delta})

    def _complete_item(self, payload):
        if payload.get("type") != "agentMessage":
            return
        item = self._item(payload.get("id"))
        text = payload.get("text")
        if not isinstance(text, str) or not text.startswith(item["text"]):
            raise EngineError("模型最终回复与已接收增量不一致", "schema")
        item.update(phase=payload.get("phase"), started=True, complete=True)
        self._append(item, text[len(item["text"]):])

    def notification(self, method, params):
        if not isinstance(params, dict) or params.get("threadId") != self.thread_id:
            return
        if self.turn_id is None:
            self.pending_bytes += len(json.dumps(params, ensure_ascii=True))
            if self.pending_bytes > _MAX_PROTOCOL_BYTES or len(self.pending) >= 1000:
                raise EngineError("模型服务在任务确认前发送了过多事件", "protocol")
            self.pending.append((method, params))
            return
        event_turn = params.get("turn", {}).get("id") if method in ("turn/started", "turn/completed") else params.get("turnId")
        if event_turn != self.turn_id:
            return
        if method == "item/started":
            payload = params.get("item", {})
            if payload.get("type") == "agentMessage":
                item = self._item(payload.get("id"))
                item.update(phase=payload.get("phase"), started=True)
                self._stream(item)
        elif method == "item/agentMessage/delta":
            self._append(self._item(params.get("itemId")), params.get("delta"))
        elif method == "item/completed":
            self._complete_item(params.get("item", {}))
        elif method == "error":
            if params.get("willRetry") is True:
                _status(self.callback, "retry")
            else:
                raise EngineError("模型服务未完成本次生成", _remote_category(params.get("error")))
        elif method == "turn/completed":
            turn = params["turn"]
            status = turn.get("status")
            if status == "interrupted":
                raise EngineError("本次生成已取消", "cancelled")
            if status == "failed":
                raise EngineError("模型服务未完成本次生成", _remote_category(turn.get("error")))
            if status != "completed":
                raise EngineError("模型服务返回了无效的结束状态", "protocol")
            for payload in turn.get("items", []):
                if payload.get("type") not in _SAFE_ITEM_TYPES:
                    raise EngineError("模型尝试使用未授权工具，本次生成已停止", "tool_access")
                self._complete_item(payload)
            candidates = [item for item in self.items.values() if item["complete"] and item["phase"] != "commentary"
                          and item["text"].lstrip().startswith("{")]
            if len(candidates) != 1:
                raise EngineError("模型未返回唯一的结构化回复", "schema")
            self.text = candidates[0]["text"]
            self.completed = True

    def bind_turn(self, turn):
        self.turn_id = _identifier(turn.get("id"))
        self.identity()
        pending, self.pending = self.pending, []
        for method, params in pending:
            self.notification(method, params)
        if not self.completed and turn.get("status") in ("completed", "failed", "interrupted"):
            self.notification("turn/completed", {"threadId": self.thread_id, "turn": turn})


class AppServerClient:
    """One lazy stdio process, serialized operations, independent thread per run.

    codex_home selects the existing login source only. Its auth.json is linked,
    not copied or opened here. Original config, rules, plugins and histories are
    not linked. Missing file-based authentication fails explicitly.
    """

    def __init__(self, timeout=180, codex_home=None):
        if type(timeout) is not int or timeout <= 0:
            raise ValueError("timeout 必须是正整数秒数")
        self.timeout = timeout
        self.codex_home = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._closed = False
        self._process = None
        self._temporary = None
        self._root = None
        self._queue = None
        self._stop_reader = None
        self._stderr_tail = deque(maxlen=8)
        self._sequence = 0

    def _send(self, message):
        with self._state_lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise EngineError("本机模型服务进程已退出", "process")
            try:
                process.stdin.write((json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise EngineError("无法向本机模型服务提交请求", "process") from exc

    def _request_id(self, method, params):
        self._sequence += 1
        identity = self._sequence
        self._send({"id": identity, "method": method, "params": params})
        return identity

    @staticmethod
    def _read_stdout(process, events, stopped):
        def push(event):
            while not stopped.is_set():
                try:
                    events.put(event, timeout=_POLL_SECONDS)
                    return
                except queue.Full:
                    pass
        try:
            while not stopped.is_set():
                line = process.stdout.readline(_MAX_PROTOCOL_BYTES + 1)
                if not line:
                    push(("eof", None))
                    return
                if len(line) > _MAX_PROTOCOL_BYTES:
                    push(("protocol", None))
                    return
                try:
                    event = json.loads(line, object_pairs_hook=_unique_json_object, parse_constant=_reject_constant)
                except (ValueError, UnicodeError, EngineError, RecursionError):
                    push(("protocol", None))
                    return
                if not isinstance(event, dict):
                    push(("protocol", None))
                    return
                push(("message", event))
        except (OSError, ValueError):
            push(("eof", None))

    @staticmethod
    def _read_stderr(process, tail, stopped):
        try:
            while not stopped.is_set():
                data = process.stderr.readline(4096)
                if not data:
                    return
                tail.append(data.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass

    def _launch(self, deadline, cancel, output):
        with self._state_lock:
            if self._closed:
                raise EngineError("本机模型服务客户端已关闭", "process")
            if self._process is not None and self._process.poll() is None:
                return
            self._reset()
            if not (self.codex_home / "auth.json").is_file():
                raise EngineError("未找到已登录的文件凭据；请先使用本机 Codex 登录", "auth")
            self._temporary = tempfile.TemporaryDirectory(prefix="inquiry-app-server-")
            self._root = Path(self._temporary.name)
            os.chmod(self._root, 0o700)
            runtime_home = self._root / "codex-home"
            user_home = self._root / "home"
            work = self._root / "work"
            for directory in (runtime_home, user_home, work):
                directory.mkdir(mode=0o700)
            (runtime_home / "auth.json").symlink_to(self.codex_home / "auth.json")
            env = _execution_environment()
            env.update(CODEX_HOME=str(runtime_home), HOME=str(user_home))
            command = [_CODEX, "app-server", "--stdio", "-c", 'web_search="disabled"',
                       "-c", "project_doc_max_bytes=0", "-c", "mcp_servers={}",
                       "-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"',
                       "-c", 'cli_auth_credentials_store="file"']
            for feature in _DISABLED_FEATURES:
                command.extend(("--disable", feature))
            self._queue = queue.Queue(maxsize=256)
            self._stop_reader = threading.Event()
            self._stderr_tail = deque(maxlen=8)
            try:
                self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE, cwd=work, env=env, start_new_session=True)
            except OSError as exc:
                self._reset()
                raise EngineError("无法启动本机模型服务", "config") from exc
            threading.Thread(target=self._read_stdout, args=(self._process, self._queue, self._stop_reader), daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(self._process, self._stderr_tail, self._stop_reader), daemon=True).start()
        self._request("initialize", {"clientInfo": {"name": "inquiry_assistant", "version": __version__},
                                     "capabilities": {"experimentalApi": True, "requestAttestation": False}},
                      deadline, cancel, output)
        self._send({"method": "initialized", "params": {}})

    def _interrupt(self, output):
        if output.thread_id is None or output.turn_id is None:
            return
        try:
            self._request_id("turn/interrupt", {"threadId": output.thread_id, "turnId": output.turn_id})
            limit = time.monotonic() + _INTERRUPT_SECONDS
            while time.monotonic() < limit:
                try:
                    kind, message = self._queue.get(timeout=min(_POLL_SECONDS, max(0.001, limit - time.monotonic())))
                except queue.Empty:
                    continue
                if kind != "message":
                    return
                params = message.get("params", {})
                if (message.get("method") == "turn/completed" and params.get("threadId") == output.thread_id
                        and params.get("turn", {}).get("id") == output.turn_id
                        and params["turn"].get("status") in ("completed", "interrupted", "failed")):
                    return
        except (EngineError, OSError):
            pass
        # Caller always terminates the process after cancellation/timeout, even
        # when no terminal notification confirms the interrupt.

    def _control(self, deadline, cancel, output):
        if self._closed:
            raise EngineError("本机模型服务客户端已关闭", "process")
        if cancel is not None and cancel.is_set():
            _status(output.callback, "cancel")
            self._interrupt(output)
            self._reset()
            raise EngineError("本次生成已取消，未保存草稿", "cancelled")
        if time.monotonic() >= deadline:
            self._interrupt(output)
            self._reset()
            raise EngineError("本次生成已超时，未保存草稿", "timeout")

    def _next(self, deadline, cancel, output):
        while True:
            self._control(deadline, cancel, output)
            try:
                kind, message = self._queue.get(timeout=min(_POLL_SECONDS, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                if self._process is None or self._process.poll() is not None:
                    raise EngineError("本机模型服务进程已退出，未保存草稿", "process")
                continue
            if kind == "protocol":
                raise EngineError("本机模型服务返回了无效协议数据", "protocol")
            if kind == "eof":
                category = _failure_category("", "".join(self._stderr_tail))
                raise EngineError("本机模型服务进程已退出，未保存草稿", "process" if category == "unknown" else category)
            return message

    def _notification(self, message, output):
        method = message.get("method")
        if not isinstance(method, str):
            return
        if "id" in message:
            self._send({"id": message["id"], "error": {"code": -32601, "message": "Tool requests are disabled"}})
            raise EngineError("模型请求了未授权操作，本次生成已停止", "tool_access")
        params = message.get("params", {})
        item = params.get("item", {}) if isinstance(params, dict) else {}
        if ((method in ("item/started", "item/completed") and item.get("type") not in _SAFE_ITEM_TYPES)
                or method.startswith(("process/", "command/", "fs/", "item/commandExecution/",
                                      "item/fileChange/", "item/mcpToolCall/", "item/tool/", "item/permissions/"))):
            raise EngineError("模型尝试使用未授权工具，本次生成已停止", "tool_access")
        output.notification(method, params)

    def _request(self, method, params, deadline, cancel, output):
        request_id = self._request_id(method, params)
        while True:
            message = self._next(deadline, cancel, output)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise EngineError("模型服务未接受本次请求", _remote_category(message["error"]))
                result = message.get("result")
                if not isinstance(result, dict):
                    raise EngineError("模型服务返回了无效响应", "protocol")
                return result
            self._notification(message, output)

    def generate(self, context, *, on_event=None, cancel_event=None):
        current = _current_context(context)
        prompt = build_prompt(current)
        output = _TurnOutput(on_event)
        deadline = time.monotonic() + self.timeout
        while not self._lock.acquire(timeout=_POLL_SECONDS):
            # A queued caller has no active turn to interrupt.
            if cancel_event is not None and cancel_event.is_set():
                raise EngineError("本次生成已取消，未保存草稿", "cancelled")
            if time.monotonic() >= deadline:
                raise EngineError("等待模型服务超时，未保存草稿", "timeout")
        try:
            self._control(deadline, cancel_event, output)
            _status(on_event, "connect")
            self._launch(deadline, cancel_event, output)
            with tempfile.TemporaryDirectory(prefix="request-", dir=self._root / "work") as directory:
                _status(on_event, "prepare")
                safety = {"web_search": "disabled", "project_doc_max_bytes": 0, "mcp_servers": {},
                          "features": {feature: False for feature in _DISABLED_FEATURES}}
                params = {"ephemeral": True, "cwd": directory, "sandbox": "read-only", "approvalPolicy": "never",
                          "environments": [], "dynamicTools": [], "selectedCapabilityRoots": [],
                          "runtimeWorkspaceRoots": [directory], "experimentalRawEvents": False,
                          "allowProviderModelFallback": False, "config": safety,
                          "developerInstructions": "只根据本次输入提供的企业资料和会话输出结构化回复；不要使用任何工具或读取其他上下文。"}
                selected_model = current.get("request", {}).get("model")
                if selected_model:
                    params["model"] = selected_model
                started = self._request("thread/start", params, deadline, cancel_event, output)
                if (started.get("sandbox", {}).get("type") != "readOnly"
                        or started.get("approvalPolicy") != "never" or started.get("instructionSources")):
                    raise EngineError("模型服务未应用预期的隔离配置", "config")
                output.thread_id = _identifier(started.get("thread", {}).get("id"))
                output.identity()
                turn_params = {"threadId": output.thread_id, "input": [{"type": "text", "text": prompt}],
                               "outputSchema": _result_schema(current), "approvalPolicy": "never",
                               "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                               "environments": [], "runtimeWorkspaceRoots": [directory]}
                if selected_model:
                    turn_params["model"] = selected_model
                if current.get("request", {}).get("mode") == "polish":
                    turn_params["effort"] = "low"
                _status(on_event, "generate")
                turn = self._request("turn/start", turn_params, deadline, cancel_event, output).get("turn")
                if not isinstance(turn, dict):
                    raise EngineError("模型服务未返回当前生成任务", "protocol")
                output.bind_turn(turn)
                while not output.completed:
                    self._notification(self._next(deadline, cancel_event, output), output)
                self._control(deadline, cancel_event, output)
                _status(on_event, "validate")
                try:
                    result = json.loads(output.text, object_pairs_hook=_unique_json_object, parse_constant=_reject_constant)
                except (ValueError, RecursionError) as exc:
                    raise EngineError("模型输出不是有效的严格 JSON，未保存草稿", "schema") from exc
                if current.get("request", {}).get("mode") == "polish":
                    result = _expand_polish_result(result)
                result = validate_result(result, current)
                self._control(deadline, cancel_event, output)
                self._request_id("thread/unsubscribe", {"threadId": output.thread_id})
                return result
        except EngineError:
            self._reset()
            raise
        except (OSError, UnicodeError, TypeError, KeyError, AttributeError) as exc:
            self._reset()
            raise EngineError("本机模型服务协议处理失败，未保存草稿", "protocol") from exc
        finally:
            self._lock.release()

    def close(self):
        """Permanently close this client, including a concurrently starting run."""
        with self._state_lock:
            self._closed = True
            self._reset()

    def _reset(self):
        """Discard failed transport state; only an explicit later run reconnects."""
        with self._state_lock:
            process, self._process = self._process, None
            if self._stop_reader is not None:
                self._stop_reader.set()
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                        process.wait(timeout=0.5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                for stream in (process.stdin, process.stdout, process.stderr):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
                self._root = None


class AppServerRunner:
    name = "codex_app_server"

    def __init__(self, client, on_event=None, cancel_event=None):
        self.client = client
        self.on_event = on_event
        self.cancel_event = cancel_event

    def analyze(self, context):
        return self.client.generate(context, on_event=self.on_event, cancel_event=self.cancel_event)
