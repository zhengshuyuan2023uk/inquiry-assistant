"""Real detached processes using synthetic workspaces; never call a model."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from inquiry_product.web_demo import seed_workspace


ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="inquiry-launcher-test-")
        self.workspace = seed_workspace(Path(self.temporary.name) / "one")
        self.record = self.workspace.root / "connections" / "workbench-runtime.json"

    def cli(self, *arguments, workspace=None):
        return subprocess.run(
            [sys.executable, "-B", str(ROOT / "start_workbench.py"), "--workspace",
             str((workspace or self.workspace).root), *arguments],
            cwd=ROOT, capture_output=True, text=True, timeout=30)

    def tearDown(self):
        self.cli("--stop", "--port", "0")
        self.temporary.cleanup()

    def start(self):
        result = self.cli("--background", "--port", "0")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(self.record.read_text())

    def test_detached_survives_parent_and_reopens_same_instance(self):
        from inquiry_product.launcher import probe
        first = self.start()
        # subprocess.run has waited for the launcher parent to exit.
        time.sleep(0.1)
        self.assertEqual(probe(first["port"])[0]["instance_id"], first["instance_id"])
        again = self.cli("--background", "--port", "0")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("已运行", again.stdout)
        self.assertEqual(json.loads(self.record.read_text())["instance_id"], first["instance_id"])
        stopped = self.cli("--stop", "--port", "0")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn("已停止", stopped.stdout)
        self.assertFalse(self.record.exists())

    def test_other_application_port_is_not_reused_or_opened(self):
        import start_workbench
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            with patch("start_workbench.webbrowser.open") as opened:
                result = start_workbench.main(["--workspace", str(self.workspace.root),
                                               "--background", "--open", "--port", str(port)])
                self.assertEqual(result, 1)
                opened.assert_not_called()
        self.assertFalse(self.record.exists())

    def test_simultaneous_double_open_starts_one_instance(self):
        command = [sys.executable, "-B", str(ROOT / "start_workbench.py"), "--workspace",
                   str(self.workspace.root), "--background", "--port", "0"]
        first = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        outputs = [process.communicate(timeout=30) for process in (first, second)]
        self.assertEqual([first.returncode, second.returncode], [0, 0], outputs)
        self.assertEqual(sum("已在后台启动" in stdout for stdout, _ in outputs), 1)
        self.assertEqual(sum("已运行" in stdout for stdout, _ in outputs), 1)

    def test_foreground_cli_remains_supported_and_can_be_safely_stopped(self):
        from inquiry_product.launcher import probe
        child = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "start_workbench.py"), "--workspace",
             str(self.workspace.root), "--port", "0"], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not self.record.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            record = json.loads(self.record.read_text())
            self.assertIsNone(child.poll())
            self.assertEqual(probe(record["port"])[0]["instance_id"], record["instance_id"])
            stopped = self.cli("--stop", "--port", "0")
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            stdout, stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertIn("已停止", stdout)
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=10)

    def test_saved_pid_is_never_used_for_stop(self):
        first = self.start()
        changed = dict(first, pid=1)
        self.record.write_text(json.dumps(changed))
        stopped = self.cli("--stop", "--port", "0")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn("已停止", stopped.stdout)

    def test_old_cleanup_cannot_erase_new_instance_registration(self):
        from inquiry_product import launcher
        old = SimpleNamespace(instance_id="old", server_address=("127.0.0.1", 8760))
        new = SimpleNamespace(instance_id="new", server_address=("127.0.0.1", 8761))
        launcher.register(self.workspace, old, "app-server")
        read_old = threading.Event()
        continue_cleanup = threading.Event()
        original_read = launcher._read_runtime

        def paused_read(workspace):
            value = original_read(workspace)
            read_old.set()
            self.assertTrue(continue_cleanup.wait(5))
            return value

        with patch.object(launcher, "_read_runtime", side_effect=paused_read):
            cleanup = threading.Thread(target=launcher.unregister, args=(self.workspace, "old"))
            cleanup.start()
            self.assertTrue(read_old.wait(5))
            replacement = threading.Thread(target=launcher.register, args=(self.workspace, new, "app-server"))
            replacement.start()
            time.sleep(0.05)
            continue_cleanup.set()
            cleanup.join(5)
            replacement.join(5)
            self.assertFalse(cleanup.is_alive() or replacement.is_alive())
        self.assertEqual(json.loads(self.record.read_text())["instance_id"], "new")
        launcher.unregister(self.workspace, "new")

    def test_initial_demo_double_open_preserves_one_complete_seed(self):
        folder = Path(self.temporary.name) / "fresh-parent" / "demo"
        command = [sys.executable, "-B", "-c",
                   "from pathlib import Path; import sys; "
                   "from inquiry_product.launcher import default_workspace; "
                   "print(default_workspace(Path(sys.argv[1])).manifest['created_at'])", str(folder)]
        first = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        outputs = [process.communicate(timeout=10) for process in (first, second)]
        self.assertEqual([first.returncode, second.returncode], [0, 0], outputs)
        self.assertEqual(outputs[0][0], outputs[1][0])

    def test_other_workspace_port_is_not_reused(self):
        first = self.start()
        other = seed_workspace(Path(self.temporary.name) / "two")
        result = self.cli("--background", "--port", str(first["port"]), workspace=other)
        self.assertEqual(result.returncode, 1)
        self.assertIn("占用", result.stderr)
        self.assertFalse((other.root / "connections" / "workbench-runtime.json").exists())

    def test_forged_stop_record_never_stops_other_workspace(self):
        from inquiry_product.launcher import probe
        first = self.start()
        other = seed_workspace(Path(self.temporary.name) / "two")
        folder = other.root / "connections"
        folder.mkdir(exist_ok=True)
        (folder / "workbench-runtime.json").write_text(json.dumps(first))
        result = self.cli("--stop", "--port", "0", workspace=other)
        self.assertEqual(result.returncode, 1)
        self.assertIn("不匹配", result.stderr)
        self.assertEqual(probe(first["port"])[0]["instance_id"], first["instance_id"])

    def test_stale_instance_id_does_not_stop_current_instance(self):
        from inquiry_product.launcher import probe
        first = self.start()
        changed = dict(first, instance_id="old-instance")
        self.record.write_text(json.dumps(changed))
        result = self.cli("--stop", "--port", "0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("不匹配", result.stderr)
        self.assertEqual(probe(first["port"])[0]["instance_id"], first["instance_id"])
        self.record.write_text(json.dumps(first))

    def test_missing_workspace_failure_is_clear(self):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "start_workbench.py"), "--background",
             "--workspace", str(Path(self.temporary.name) / "missing"), "--port", "0"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("启动失败", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
