"""Bounded local startup and identity-checked stop; never signal a saved PID."""
from contextlib import contextmanager
import fcntl
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .workspace import _atomic_write


def workspace_instance(workspace):
    value = [str(workspace.root.resolve()), workspace.manifest["created_at"]]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _runtime_path(workspace):
    return workspace.root / "connections" / "workbench-runtime.json"


def _read_runtime(workspace):
    path = _runtime_path(workspace)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16_384:
        raise ValueError("工作台运行记录无效，请负责人检查连接目录")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or not isinstance(value.get("port"), int)
                or isinstance(value["port"], bool) or not 0 < value["port"] <= 65535
                or not isinstance(value.get("instance_id"), str)
                or not isinstance(value.get("workspace_instance"), str)):
            raise ValueError()
        return value
    except (ValueError, UnicodeError) as exc:
        raise ValueError("工作台运行记录无效，请负责人检查连接目录") from exc


def register(workspace, server, runner):
    """Called only after the HTTP server and workspace lease are acquired."""
    folder = _runtime_path(workspace).parent
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("工作台连接目录无效")
    record = {"workspace_instance": workspace_instance(workspace),
              "instance_id": server.instance_id, "port": server.server_address[1],
              "pid": os.getpid(), "runner": runner}
    with _record_lock(workspace):
        _atomic_write(_runtime_path(workspace), json.dumps(record).encode())
    return record


def unregister(workspace, instance_id):
    """A closing process must never erase the successor's discovery record."""
    try:
        with _record_lock(workspace):
            record = _read_runtime(workspace)
            if record and record["instance_id"] == instance_id:
                _runtime_path(workspace).unlink()
    except (OSError, ValueError):
        pass


@contextmanager
def _file_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as handle:
        deadline = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("工作台正在启动或停止，请稍后再试")
                time.sleep(0.1)
        yield


def _launch_lock(workspace):
    return _file_lock(workspace.root / "logs" / "launcher.lock")


def _record_lock(workspace):
    # Separate from the parent-held launch lease: the child must publish its
    # record while the parent is still waiting for the authenticated handshake.
    return _file_lock(workspace.root / "logs" / "runtime-record.lock")


def default_workspace(folder):
    """Double-clicking twice on first launch must not race the initial seed."""
    from .web_demo import seed_workspace
    from .workspace import Workspace
    folder.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _file_lock(folder.parent / ".workbench-init.lock"):
        return Workspace.load(folder) if folder.exists() else seed_workspace(folder)


def _json_response(response):
    content = response.read(2_000_001)
    if len(content) > 2_000_000:
        raise ValueError("工作台响应超过上限")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("工作台响应格式不正确")
    return result


def probe(port):
    """Read authenticated bootstrap on loopback, ignoring environment proxies."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise urllib.error.URLError("工作台本机接口不允许跳转")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirect(),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    origin = f"http://127.0.0.1:{port}"
    with opener.open(origin + "/", timeout=1) as response:
        response.read(2_000_001)
    with opener.open(origin + "/api/bootstrap", timeout=2) as response:
        return _json_response(response), opener


def _occupied(port):
    if not port:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _matching(workspace, payload, record=None):
    if (payload.get("workspace_instance") != workspace_instance(workspace)
            or not isinstance(payload.get("instance_id"), str)
            or not payload["instance_id"]
            or (record and record["instance_id"] != payload["instance_id"])):
        raise ValueError("运行实例与此工作区不匹配，已拒绝操作；请负责人核对原工作台")


def _existing(workspace, port, runner):
    try:
        payload, _ = probe(port)
        _matching(workspace, payload)
    except (OSError, ValueError) as exc:
        raise ValueError(f"端口 {port} 已被其他工作台或应用占用；请负责人选择其他端口") from exc
    requested = "codex_app_server" if runner == "app-server" else "codex_local"
    if payload.get("capabilities", {}).get("model") != requested:
        raise ValueError("此工作区正使用另一种后台引擎；请先停止原工作台再切换")
    return payload


def start_background(workspace, port, runner, script, timeout=15):
    """Return (verified URL, reused); child inherits no terminal or parent lease."""
    with _launch_lock(workspace):
        record = _read_runtime(workspace)
        if record:
            if record["workspace_instance"] != workspace_instance(workspace):
                raise ValueError("运行记录与此工作区不匹配，请负责人检查")
            if _occupied(record["port"]):
                _existing(workspace, record["port"], runner)
                return f"http://127.0.0.1:{record['port']}/", True
        if _occupied(port):
            _existing(workspace, port, runner)
            return f"http://127.0.0.1:{port}/", True
        log_path = workspace.root / "logs" / "workbench-service.log"
        fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-B", str(Path(script).resolve()), "--workspace",
                 str(workspace.root.resolve()), "--port", str(port), "--runner", runner],
                cwd=Path(script).resolve().parent, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise ValueError(f"后台服务未能启动，请负责人查看：{log_path}")
            record = _read_runtime(workspace)
            if record and record.get("pid") == child.pid:
                try:
                    payload, _ = probe(record["port"])
                    _matching(workspace, payload, record)
                except (OSError, ValueError):
                    pass
                else:
                    return f"http://127.0.0.1:{record['port']}/", False
            time.sleep(0.1)
        # Only this invocation's freshly owned Popen is terminated on timeout;
        # persisted PID values are never used for signalling or stop requests.
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        raise ValueError(f"后台启动未在 {timeout:g} 秒内完成，请负责人查看：{log_path}")


def stop_background(workspace, port, timeout=10):
    """Stop only a live, matching HTTP instance; never signal saved PIDs."""
    with _launch_lock(workspace):
        record = _read_runtime(workspace)
        if record and record["workspace_instance"] != workspace_instance(workspace):
            raise ValueError("运行记录与此工作区不匹配，已拒绝停止")
        target_port = record["port"] if record else port
        if not _occupied(target_port):
            if record:
                unregister(workspace, record["instance_id"])
            return "工作台未运行。"
        try:
            payload, opener = probe(target_port)
        except (OSError, ValueError) as exc:
            raise ValueError("无法核实运行实例身份，已拒绝停止；请负责人检查") from exc
        _matching(workspace, payload, record)
        origin = f"http://127.0.0.1:{target_port}"
        request = urllib.request.Request(
            origin + "/api/shutdown", data=json.dumps({"instance_id": payload["instance_id"]}).encode(),
            headers={"Content-Type": "application/json", "Origin": origin,
                     "X-CSRF-Token": payload.get("csrf_token", "")}, method="POST")
        try:
            with opener.open(request, timeout=3) as response:
                _json_response(response)
        except urllib.error.HTTPError as exc:
            try:
                message = _json_response(exc).get("error", "工作台暂时无法停止")
            except (ValueError, OSError):
                message = "工作台暂时无法停止"
            raise ValueError(message) from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _occupied(target_port):
                unregister(workspace, payload["instance_id"])
                return "工作台已停止。"
            time.sleep(0.1)
        return "停止请求已确认，工作台仍在收尾；请稍后再次检查。"
