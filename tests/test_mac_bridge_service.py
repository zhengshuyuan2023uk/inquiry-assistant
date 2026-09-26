"""Bridge lifecycle uses only a synthetic local process, never WhatsApp login."""
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Deliberately implements the public packaged-bridge protocol independently.
FAKE = r'''#!PYTHON
import http.server, json, os, pathlib, threading, time
root = pathlib.Path(os.environ['INQUIRY_BRIDGE_ROOT'])
token = os.environ['INQUIRY_BRIDGE_TOKEN']
instance = os.environ['INQUIRY_BRIDGE_INSTANCE']
record_path = root / 'bridge_run/runtime.json'
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer ' + token:
            self.send_error(401); return
        if self.path != '/status': self.send_error(404); return
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({'install_root': str(root), 'instance_id': instance, 'state': 'waiting_login'}).encode())
    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer ' + token:
            self.send_error(401); return
        if self.path != '/shutdown': self.send_error(404); return
        self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
        threading.Thread(target=self.server.shutdown).start()
server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
(root / 'bridge_run/store/login-qr.png').write_bytes(b'FAKE QR ONLY')
(root / 'bridge_run/store/messages.db').write_bytes(b'SYNTHETIC ONLY')
record_path.write_text(json.dumps({'install_root': str(root), 'instance_id': instance, 'token': token, 'port': server.server_port, 'pid': os.getpid()}))
os.chmod(record_path, 0o600)
server.serve_forever(); server.server_close()
if (root / 'delay-cleanup').exists():
    (root / 'port-closed').touch()
    deadline = time.monotonic() + 3
    while not (root / 'allow-cleanup').exists() and time.monotonic() < deadline: time.sleep(0.01)
if json.loads(record_path.read_text())['instance_id'] == instance:
    (root / 'bridge_run/store/login-qr.png').unlink(missing_ok=True)
    record_path.unlink()
'''


class BridgeServiceTests(unittest.TestCase):
    def setUp(self):
        try:
            self.service = importlib.import_module('deploy.macos.bridge_service')
        except ModuleNotFoundError:
            self.service = None
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-service-test-')
        self.root = Path(self.temp.name) / '安装 根'
        (self.root / 'bin').mkdir(parents=True)
        binary = self.root / 'bin/whatsapp-bridge'
        binary.write_text(FAKE.replace('PYTHON', sys.executable, 1))
        binary.chmod(0o700)

    def tearDown(self):
        if self.service:
            try: self.service.stop(self.root)
            except ValueError: pass
        self.temp.cleanup()

    def available(self):
        self.assertIsNotNone(self.service, 'bridge lifecycle wrapper is not implemented')
        return self.service

    def test_detached_start_waits_only_for_local_readiness_and_reuses_instance(self):
        service = self.available()
        command = [sys.executable, '-B', '-c',
                   'from deploy.macos.bridge_service import ensure_started; import sys,json; print(json.dumps(ensure_started(sys.argv[1])))', str(self.root)]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result['state'], 'waiting_login')
        self.assertTrue(result['qr_available'])
        self.assertFalse(result['reused'])
        self.assertNotIn('token', result)
        first = json.loads((self.root / 'bridge_run/runtime.json').read_text())
        self.assertTrue(service.ensure_started(self.root)['reused'])
        self.assertEqual(json.loads((self.root / 'bridge_run/runtime.json').read_text())['instance_id'], first['instance_id'])
        self.assertEqual(service.stop(self.root)['state'], 'stopped')
        self.assertEqual(service.status(self.root)['state'], 'stopped')

    def test_stop_waits_for_delayed_cleanup_before_allowing_restart(self):
        service = self.available()
        (self.root / 'delay-cleanup').touch()
        service.ensure_started(self.root)
        completed = threading.Event()
        errors = []
        def stopping():
            try: service.stop(self.root)
            except Exception as exc: errors.append(str(exc))
            finally: completed.set()
        worker = threading.Thread(target=stopping)
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while not (self.root / 'port-closed').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / 'port-closed').exists())
            self.assertFalse(completed.wait(0.15), 'stop returned before old QR/runtime cleanup finished')
        finally:
            (self.root / 'allow-cleanup').touch()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse((self.root / 'bridge_run/runtime.json').exists())
        self.assertFalse((self.root / 'bridge_run/store/login-qr.png').exists())
        self.assertEqual(service.ensure_started(self.root)['state'], 'waiting_login')
        self.assertTrue((self.root / 'bridge_run/store/login-qr.png').exists())

    def test_stop_ignores_forged_pid_and_never_signals_it(self):
        service = self.available()
        service.ensure_started(self.root)
        record = self.root / 'bridge_run/runtime.json'
        value = json.loads(record.read_text()); value['pid'] = os.getpid(); record.write_text(json.dumps(value))
        self.assertEqual(service.stop(self.root)['state'], 'stopped')

    def test_wrong_instance_is_not_stopped_or_reused(self):
        service = self.available()
        service.ensure_started(self.root)
        record = self.root / 'bridge_run/runtime.json'
        original = record.read_text(); value = json.loads(original)
        value['instance_id'] = 'not-the-running-instance'; record.write_text(json.dumps(value))
        try:
            with self.assertRaises(ValueError): service.stop(self.root)
            with self.assertRaises(ValueError): service.ensure_started(self.root)
        finally: record.write_text(original)
        self.assertEqual(service.status(self.root)['state'], 'waiting_login')

    def test_other_install_record_does_not_attach_to_running_bridge(self):
        service = self.available()
        service.ensure_started(self.root)
        other = Path(self.temp.name) / 'other'; (other / 'bridge_run').mkdir(parents=True)
        (other / 'bridge_run/runtime.json').write_bytes((self.root / 'bridge_run/runtime.json').read_bytes())
        with self.assertRaises(ValueError): service.stop(other)
        self.assertEqual(service.status(self.root)['state'], 'waiting_login')

    def test_status_on_fresh_install_is_read_only(self):
        service = self.available()
        self.assertEqual(service.status(self.root)['state'], 'stopped')
        self.assertFalse((self.root / 'bridge_run').exists())

    def test_missing_binary_does_not_create_runtime(self):
        service = self.available()
        (self.root / 'bin/whatsapp-bridge').unlink()
        with self.assertRaises(ValueError): service.ensure_started(self.root)
        self.assertFalse((self.root / 'bridge_run/runtime.json').exists())

    def test_runtime_symlink_does_not_read_or_replace_target(self):
        service = self.available()
        (self.root / 'bridge_run').mkdir()
        unrelated = Path(self.temp.name) / 'untouched'; unrelated.write_text('private')
        (self.root / 'bridge_run/runtime.json').symlink_to(unrelated)
        with self.assertRaises(ValueError): service.ensure_started(self.root)
        self.assertEqual(unrelated.read_text(), 'private')

    def test_concurrent_starts_publish_one_instance(self):
        service = self.available()
        results = []; errors = []
        def start():
            try: results.append(service.ensure_started(self.root))
            except Exception as exc: errors.append(str(exc))
        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(item['reused'] for item in results), [False, True])
        self.assertEqual(service.status(self.root)['state'], 'waiting_login')

if __name__ == '__main__': unittest.main()
