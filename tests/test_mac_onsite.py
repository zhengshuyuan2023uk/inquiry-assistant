"""Mac deployment contracts, using empty workspaces and disposable fake executables."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from inquiry_product import manual_sync
from inquiry_product.workspace import Workspace


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "deploy" / "macos" / "onsite.py"


class MacOnsiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="inquiry-onsite-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bundle = self.base / "套件 有空格"
        self.install = self.base / "安装 '$安全"
        self.desktop = self.base / "Desktop"
        self.desktop.mkdir()

    def api(self):
        self.assertTrue(MODULE.is_file(), "Mac 现场安装入口尚未实现")
        spec = importlib.util.spec_from_file_location("onsite_test_module", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def bundle_fixture(self):
        (self.bundle / "app").mkdir(parents=True)
        shutil.copytree(ROOT / "inquiry_product", self.bundle / "app" / "inquiry_product",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.bundle / "app" / "start_workbench.py").write_text(
            "import json,os,sys\nfrom pathlib import Path\n"
            "Path(os.environ['RECORD']).write_text(json.dumps({'args':sys.argv[1:],"
            "'home':os.environ['CODEX_HOME'],'path':os.environ['PATH']}))\n")
        (self.bundle / "bin").mkdir()
        for name in ("codex", "whatsapp-bridge"):
            target = self.bundle / "bin" / name
            target.write_text("#!/bin/sh\nexit 0\n")
            target.chmod(0o755)
        (self.bundle / "deploy" / "macos").mkdir(parents=True)
        shutil.copy2(MODULE, self.bundle / "deploy" / "macos" / "onsite.py")
        (self.bundle / "deploy" / "macos" / "bridge_service.py").write_text(
            "def status(root):\n return {'state':'stopped','qr_available':False}\n"
            "def ensure_started(root):\n return status(root)\n")
        (self.bundle / "runtime").mkdir()
        (self.bundle / "runtime" / "python-macos.pkg").write_bytes(b"test-package")
        self.write_manifest()

    def write_manifest(self):
        files = {str(p.relative_to(self.bundle)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.bundle.rglob("*") if p.is_file() and p.name != "bundle-manifest.json"}
        manifest = {"schema_version": 1, "product": "inquiry-assistant-macos",
                    "version": "onsite-test", "files": files}
        (self.bundle / "bundle-manifest.json").write_text(json.dumps(manifest))

    def installed(self):
        api = self.api()
        self.bundle_fixture()
        with patch.object(api, "validate_platform"):
            result = api.install_suite(self.bundle, self.install, self.desktop, "测试企业甲", "贸易")
        return api, result

    def test_install_creates_empty_customer_workspace_not_demo(self):
        api, result = self.installed()
        from inquiry_product.workspace import Workspace
        ws = Workspace.load(self.install / "workspace")
        self.assertEqual((ws.company_id, ws.mode), ("customer-01", "customer"))
        config = json.loads((ws.configs_dir / "customer-01.json").read_text())
        self.assertEqual(config["name"], "测试企业甲")
        self.assertEqual(config["knowledge"], [])
        db = sqlite3.connect(ws.db_path)
        try:
            self.assertEqual(db.execute("select count(*) from messages").fetchone()[0], 0)
        finally:
            db.close()
        self.assertFalse((self.install / "runtime").exists())
        self.assertFalse((self.install / "workspace/connections/whatsapp.json").exists())
        self.assertTrue((self.install / "codex-home").is_dir())
        self.assertFalse(result["reused"])

    def test_reinstall_preserves_workspace_and_auth_without_overwriting(self):
        api, _ = self.installed()
        marker = self.install / "workspace" / "keep.txt"
        marker.write_text("customer state")
        auth = self.install / "codex-home" / "auth.json"
        auth.write_text("do not read or copy this")
        with patch.object(api, "validate_platform"):
            result = api.install_suite(self.bundle, self.install, self.desktop, "另一家企业", "其他")
        self.assertTrue(result["reused"])
        self.assertEqual(marker.read_text(), "customer state")
        self.assertEqual(auth.read_text(), "do not read or copy this")
        config = next((self.install / "workspace/knowledge/releases").glob("*/customer-01.json"))
        self.assertEqual(json.loads(config.read_text())["name"], "测试企业甲")

    def test_refuses_unmarked_or_symlink_destination(self):
        api = self.api()
        self.bundle_fixture()
        self.install.mkdir()
        (self.install / "keep").write_text("untouched")
        with patch.object(api, "validate_platform"):
            with self.assertRaisesRegex(ValueError, "已有|标识"):
                api.install_suite(self.bundle, self.install, self.desktop, "测试企业甲", "贸易")
        self.assertEqual((self.install / "keep").read_text(), "untouched")
        alias = self.base / "alias"
        alias.symlink_to(self.install, target_is_directory=True)
        with patch.object(api, "validate_platform"):
            with self.assertRaisesRegex(ValueError, "符号链接"):
                api.install_suite(self.bundle, alias, self.desktop, "测试企业甲", "贸易")

    def test_tampered_payload_is_rejected_before_install(self):
        api = self.api()
        self.bundle_fixture()
        (self.bundle / "bin/codex").write_text("changed")
        with patch.object(api, "validate_platform"):
            with self.assertRaisesRegex(ValueError, "校验"):
                api.install_suite(self.bundle, self.install, self.desktop, "测试企业甲", "贸易")
        self.assertFalse(self.install.exists())

    def test_manifest_cannot_smuggle_credentials_even_with_matching_hash(self):
        api = self.api()
        self.bundle_fixture()
        (self.bundle / "app/auth.json").write_text("forbidden")
        self.write_manifest()
        with patch.object(api, "validate_platform"):
            with self.assertRaisesRegex(ValueError, "发布|允许|运行资料"):
                api.install_suite(self.bundle, self.install, self.desktop, "测试企业甲", "贸易")
        self.assertFalse(self.install.exists())

    def test_existing_desktop_file_is_not_overwritten(self):
        api = self.api()
        self.bundle_fixture()
        target = self.desktop / "打开询盘助手.command"
        target.write_text("personal script")
        with patch.object(api, "validate_platform"):
            with self.assertRaisesRegex(ValueError, "桌面|已有"):
                api.install_suite(self.bundle, self.install, self.desktop, "测试企业甲", "贸易")
        self.assertEqual(target.read_text(), "personal script")
        self.assertFalse(self.install.exists())

    def test_initial_connection_waits_for_source_and_keeps_saved_selection(self):
        api, _ = self.installed()
        self.assertFalse(api.ensure_connection(self.install))
        db = self.install / "bridge_run/store/messages.db"
        db.parent.mkdir(parents=True)
        sqlite3.connect(db).close()
        self.assertTrue(api.ensure_connection(self.install))
        target = self.install / "workspace/connections/whatsapp.json"
        config = json.loads(target.read_text())
        self.assertEqual(config["conversations"], [])
        config["conversations"] = [{"jid": "100@s.whatsapp.net", "display_name": "授权客户", "enabled": True}]
        target.write_text(json.dumps(config))
        self.assertTrue(api.ensure_connection(self.install))
        self.assertEqual(json.loads(target.read_text())["conversations"], config["conversations"])

    def test_open_passes_fixed_customer_workspace_and_isolated_home(self):
        api, _ = self.installed()
        db = self.install / "bridge_run/store/messages.db"
        db.parent.mkdir(parents=True)
        sqlite3.connect(db).close()
        record = self.base / "launched.json"
        previous = os.environ.get("CODEX_HOME")
        with patch.dict(os.environ, {"RECORD": str(record)}):
            api.open_workbench(self.install, port=0)
        recorded = json.loads(record.read_text())
        self.assertIn("--background", recorded["args"])
        self.assertIn("--open", recorded["args"])
        self.assertEqual(recorded["args"][recorded["args"].index("--workspace") + 1], str(self.install / "workspace"))
        self.assertEqual(recorded["home"], str(self.install / "codex-home"))
        self.assertEqual(recorded["path"].split(os.pathsep)[0], str(self.install / "bin"))
        self.assertEqual(os.environ.get("CODEX_HOME"), previous)

    def test_platform_rejects_intel_and_older_macos(self):
        api = self.api()
        for system, machine, version in (("Linux", "aarch64", ""), ("Darwin", "x86_64", "14.5"), ("Darwin", "arm64", "13.6")):
            with patch.object(api.platform, "system", return_value=system), patch.object(api.platform, "machine", return_value=machine), patch.object(api.platform, "mac_ver", return_value=(version, (), "")):
                with self.assertRaises(ValueError):
                    api.validate_platform()

    def test_login_uses_only_customer_home_and_file_credential_option(self):
        api, _ = self.installed()
        record = self.base / "login.json"
        program = self.install / "bin/codex"
        program.write_text(f"#!{sys.executable}\nimport os,sys,json\nfrom pathlib import Path\n"
                           "Path(os.environ['RECORD']).write_text(json.dumps({'args':sys.argv[1:], 'home':os.environ['CODEX_HOME']}))\n"
                           "(Path(os.environ['CODEX_HOME'])/'auth.json').write_bytes(b'fake-auth')\n")
        program.chmod(0o755)
        previous = os.environ.get("CODEX_HOME")
        with patch.dict(os.environ, {"RECORD": str(record)}):
            api.login_ai(self.install)
        value = json.loads(record.read_text())
        self.assertEqual(value["args"], ["-c", 'cli_auth_credentials_store="file"', "login"])
        self.assertEqual(value["home"], str(self.install / "codex-home"))
        self.assertEqual(os.environ.get("CODEX_HOME"), previous)

    def test_check_never_runs_codex_or_parses_credential_contents(self):
        api, _ = self.installed()
        auth = self.install / "codex-home/auth.json"
        auth.write_bytes(b"\xff this is not JSON and must not be read")
        forbidden = self.base / "unexpected-model-call"
        program = self.install / "bin/codex"
        program.write_text(f"#!/bin/sh\ntouch '{forbidden}'\nexit 99\n")
        program.chmod(0o755)
        result = api.check_installation(self.install)
        self.assertTrue(result["workspace"]["ok"])
        self.assertTrue(result["ai_login_file_present"])
        self.assertFalse(result["ai_inference_tested"])
        self.assertFalse(result["new_message_sync_tested"])
        self.assertFalse(forbidden.exists())

    def test_credentials_symlink_rejected_before_starting_login(self):
        api, _ = self.installed()
        foreign = self.base / "other-account-auth.json"
        foreign.write_text("original")
        (self.install / "codex-home/auth.json").symlink_to(foreign)
        invoked = self.base / "unsafe-login-started"
        (self.install / "bin/codex").write_text(f"#!/bin/sh\ntouch '{invoked}'\n")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            api.login_ai(self.install)
        self.assertEqual(foreign.read_text(), "original")
        self.assertFalse(invoked.exists())

    def test_shell_entries_find_root_and_nested_scripts_preserving_arguments(self):
        api = self.api()
        source = ROOT / "deploy/macos"
        command_names = {"01-现场安装.command": "install", "02-连接AI.command": "login-ai",
                         "03-连接WhatsApp.command": "connect-whatsapp", "04-打开工作台.command": "open",
                         "05-检查安装.command": "check"}
        for filename, action in command_names.items():
            self.assertTrue((source / filename).is_file(), "缺少现场双击入口")
        for placement in ("root", "nested"):
            folder = self.base / placement / "有空格 $目录"
            scripts = folder / "deploy/macos"
            scripts.mkdir(parents=True)
            (scripts / "onsite.py").write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\n")
            entry_folder = folder if placement == "root" else scripts
            for path in source.glob("*.command"):
                shutil.copy2(path, entry_folder / path.name)
            for filename, action in command_names.items():
                result = subprocess.run(["/bin/zsh", str(entry_folder / filename), "--install-root", str(self.install)],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout.strip()), [action, "--install-root", str(self.install)])

    def test_allowed_root_instructions_are_verified_without_being_installed(self):
        api = self.api()
        self.bundle_fixture()
        (self.bundle / "01-现场安装.command").write_text("#!/bin/sh\nexit 0\n")
        (self.bundle / "先读我-现场安装.md").write_text("现场手册")
        self.write_manifest()
        with patch.object(api, "validate_platform"):
            api.install_suite(self.bundle, self.install, self.desktop, "测试企业甲", "贸易")
        self.assertFalse((self.install / "01-现场安装.command").exists())

    def test_reinstall_repairs_missing_shortcut_but_rejects_foreign_replacement(self):
        api, _ = self.installed()
        shortcut = self.desktop / "打开询盘助手.command"
        original = shortcut.read_bytes()
        shortcut.unlink()
        with patch.object(api, "validate_platform"):
            api.install_suite(self.bundle, self.install, self.desktop, "", "")
        self.assertEqual(shortcut.read_bytes(), original)
        shortcut.write_text("someone else's shortcut")
        with patch.object(api, "validate_platform"):
            with self.assertRaisesRegex(ValueError, "桌面|已有"):
                api.install_suite(self.bundle, self.install, self.desktop, "", "")
        self.assertEqual(shortcut.read_text(), "someone else's shortcut")

    def test_connect_waits_for_qr_after_waiting_login_is_reported(self):
        api, _ = self.installed()
        script = self.install / "deploy/macos/bridge_service.py"
        script.write_text("def ensure_started(root):\n return {'state':'waiting_login','qr_available':False}\n"
                          "def status(root):\n p=root/'bridge_run/store/login-qr.png'; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(b'fake-qr')\n return {'state':'waiting_login','qr_available':True}\n")
        with patch.object(api.subprocess, "run"):
            result = api.connect_whatsapp(self.install)
        self.assertTrue(result["qr_available"])
        self.assertTrue((self.install / "bridge_run/store/login-qr.png").is_file())

    def test_explicit_connect_restarts_failed_bridge_to_get_new_qr(self):
        api, _ = self.installed()
        script = self.install / "deploy/macos/bridge_service.py"
        script.write_text("def ensure_started(root):\n"
                          " if (root/'stopped-once').exists():\n  p=root/'bridge_run/store/login-qr.png'; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(b'new-qr')\n  return {'state':'waiting_login','qr_available':True}\n"
                          " return {'state':'error','qr_available':False}\n"
                          "def stop(root):\n (root/'stopped-once').write_text('stopped')\n")
        with patch.object(api.subprocess, "run"):
            result = api.connect_whatsapp(self.install)
        self.assertEqual(result["state"], "waiting_login")
        self.assertTrue(result["qr_available"])
        self.assertEqual((self.install / "bridge_run/store/login-qr.png").read_bytes(), b"new-qr")

    def test_connect_does_not_restart_connected_bridge(self):
        api, _ = self.installed()
        script = self.install / "deploy/macos/bridge_service.py"
        script.write_text("def ensure_started(root):\n return {'state':'connected','qr_available':False}\n"
                          "def stop(root):\n raise AssertionError('must keep connected bridge')\n")
        self.assertEqual(api.connect_whatsapp(self.install)["state"], "connected")


if __name__ == "__main__":
    unittest.main()
