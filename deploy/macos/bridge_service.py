"""Private local bridge lifecycle. No saved PID is ever used for signalling."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
import threading
import urllib.error
import urllib.request
import uuid


def _root(value):
    root = Path(value).expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError('安装目录无效，请重新选择询盘助手安装目录')
    return root.resolve()


def _paths(root):
    for name in ('bridge_run', 'bridge_run/store', 'logs'):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError('连接目录无效，已拒绝启动 WhatsApp 连接')
    return root / 'bridge_run/runtime.json'


def _read(root):
    path = _paths(root)
    if path.is_symlink():
        raise ValueError('WhatsApp 运行记录无效')
    if not path.exists():
        return None
    try:
        with path.open() as file:
            record = json.loads(file.read(8193))
        if (not isinstance(record, dict) or record.get('install_root') != str(root)
                or not isinstance(record.get('token'), str) or len(record['token']) < 32
                or not isinstance(record.get('instance_id'), str) or not record['instance_id']
                or type(record.get('port')) is not int or not 0 < record['port'] < 65536):
            raise ValueError()
        return record
    except FileNotFoundError:
        return None  # The verified instance may finish cleanup during this read.
    except (ValueError, OSError, UnicodeError) as exc:
        raise ValueError('WhatsApp 运行记录与此安装不匹配，已拒绝操作') from exc


def _result(root, state, **extra):
    qr = root / 'bridge_run/store/login-qr.png'
    return dict(state=state, qr_path=str(qr),
                qr_available=state == 'waiting_login' and qr.is_file() and not qr.is_symlink(),
                messages_db=str(root / 'bridge_run/store/messages.db'), **extra)


def _occupied(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.3):
            return True
    except OSError:
        return False


def _request(record, path='/status'):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise urllib.error.URLError('禁止本机连接接口跳转')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(
        f"http://127.0.0.1:{record['port']}{path}",
        headers={'Authorization': 'Bearer ' + record['token']},
        method='GET' if path == '/status' else 'POST',
        data=None if path == '/status' else b'')
    try:
        with opener.open(request, timeout=2) as response:
            payload = json.loads(response.read(8193))
        if not isinstance(payload, dict):
            raise ValueError()
        if path == '/status' and (payload.get('instance_id') != record['instance_id']
                or payload.get('install_root') != record['install_root']
                or payload.get('state') not in ('starting', 'waiting_login', 'connected', 'disconnected', 'error')):
            raise ValueError()
        return payload
    except (OSError, ValueError) as exc:
        raise ValueError('无法核实 WhatsApp 连接实例，已拒绝接管或停止其他程序') from exc


@contextmanager
def _lock(root):
    _paths(root)
    for name in ('bridge_run', 'bridge_run/store', 'logs'):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    fd = os.open(root / 'bridge_run/control.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as file:
        deadline = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise ValueError('WhatsApp 连接正在启动或停止，请稍后重试')
                time.sleep(0.05)
        yield


def status(install_root):
    """Read-only status; never logs in, creates a directory, or returns credentials."""
    root = _root(install_root)
    record = _read(root)
    if not record or not _occupied(record['port']):
        return _result(root, 'stopped')
    return _result(root, _request(record)['state'])


def ensure_started(install_root, timeout=12):
    root = _root(install_root)
    binary = root / 'bin/whatsapp-bridge'
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError('WhatsApp 连接组件缺失，请重新安装交付套件')
    with _lock(root):
        record = _read(root)
        if record and _occupied(record['port']):
            return _result(root, _request(record)['state'], reused=True)
        instance = str(uuid.uuid4())
        environment = os.environ.copy()
        environment.update(INQUIRY_BRIDGE_ROOT=str(root), INQUIRY_BRIDGE_INSTANCE=instance,
                           INQUIRY_BRIDGE_TOKEN=secrets.token_urlsafe(32))
        fd = os.open(root / 'logs/whatsapp-bridge.log', os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as log:
            child = subprocess.Popen([str(binary)], cwd=root / 'bridge_run', env=environment,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True, close_fds=True, umask=0o077)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise ValueError('WhatsApp 连接启动失败，请负责人查看 logs/whatsapp-bridge.log')
            record = _read(root)
            if record and record['instance_id'] == instance:
                try:
                    state = _request(record)['state']
                    threading.Thread(target=child.wait, daemon=True).start()
                    return _result(root, state, reused=False)
                except ValueError:
                    pass
            time.sleep(0.05)
        # Only this invocation's newly-owned child may be signalled on failure.
        child.terminate()
        try: child.wait(timeout=3)
        except subprocess.TimeoutExpired: pass
        raise ValueError('WhatsApp 连接启动超时，请负责人检查连接日志')


def stop(install_root, timeout=8):
    root = _root(install_root)
    with _lock(root):
        record = _read(root)
        if not record:
            return _result(root, 'stopped')
        if _occupied(record['port']):
            _request(record)  # Validate live instance before requesting a shutdown.
            _request(record, '/shutdown')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = _read(root)
            cleanup_finished = not current or current['instance_id'] != record['instance_id']
            if not _occupied(record['port']) and cleanup_finished:
                return _result(root, 'stopped')
            time.sleep(0.05)
        raise ValueError('WhatsApp 连接已收到停止请求，仍在收尾，请稍后检查')
