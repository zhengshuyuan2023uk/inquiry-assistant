"""Catch accidental customer-state distribution and broken installer manifests."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class MacBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def module(self):
        script = ROOT / "scripts" / "package_mac_onsite.py"
        self.assertTrue(script.is_file(), "Mac onsite package builder is not implemented")
        spec = importlib.util.spec_from_file_location("mac_bundle_under_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fixture(self):
        product, components, bridge = [self.root / name for name in ("product", "components", "bridge")]
        files = {
            product / "inquiry_product" / "__init__.py": b'__version__ = "0.7.0a1"\n',
            product / "start_workbench.py": b"print('fixture')\n",
            product / "configure_whatsapp.py": b"print('fixture')\n",
            product / "README.md": b"fixture product\n",
            product / "LICENSE": b"fixture product MIT license\n",
            product / "THIRD_PARTY_NOTICES.md": b"fixture third-party scope\n",
            product / "docs" / "MAC_ONSITE.md": b"onsite instructions\n",
            product / "docs" / "MAC_COMPONENTS.md": b"redistributed component information\n",
            product / "deploy" / "macos" / "CUSTOMER_README.md": b"customer installation guide\n",
            product / "deploy" / "macos" / "onsite.py": b"# fixture installer\n",
            product / "deploy" / "macos" / "bridge_service.py": b"# fixture service\n",
            product / "deploy" / "macos" / "01-\u73b0\u573a\u5b89\u88c5.command": b"#!/bin/zsh\nexit 0\n",
            components / "codex": b"fixture codex binary",
            components / "python-macos.pkg": b"fixture signed pkg",
            components / "LICENSES" / "Codex-LICENSE.txt": b"fixture license",
            components / "LICENSES" / "Python-LICENSE.txt": b"fixture license",
            bridge / "bin" / "whatsapp-bridge": b"fixture bridge binary",
            bridge / "source" / "whatsapp-bridge-source.tar.gz": b"fixture source archive",
            bridge / "LICENSES" / "WhatsApp-LICENSE.txt": b"fixture license",
        }
        for path, value in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        (components / "components.json").write_text(json.dumps({
            "codex": {"version": "0.145.0", "sha256": hashlib.sha256(files[components / "codex"]).hexdigest()},
            "python": {"version": "3.13.15", "sha256": hashlib.sha256(files[components / "python-macos.pkg"]).hexdigest()},
        }))
        (bridge / "bridge-build.json").write_text(json.dumps({"binary_sha256": hashlib.sha256(files[bridge / "bin" / "whatsapp-bridge"]).hexdigest(), "platform": "darwin-arm64", "macos_minimum": "14.0"}))
        # Poison unrelated folders to catch broad directory copying.
        for path in (product / "workspaces" / "real" / "messages.db", components / "auth.json", bridge / "store" / "whatsapp.db"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE_SENTINEL_DO_NOT_SHIP")
        (product / "README.md").write_text("PRIVATE_SENTINEL_DO_NOT_SHIP developer workstation notes")
        (product / "docs" / "DELIVERY.md").write_text("PRIVATE_SENTINEL_DO_NOT_SHIP internal commercial arrangements")
        return product, components, bridge

    def test_bundle_contains_only_clean_payload_with_verified_manifest_and_executable_entry(self):
        module = self.module()
        product, components, bridge = self.fixture()
        target = self.root / "customer-kit.zip"
        result = module.package(product, components, bridge, target)
        self.assertEqual(result["credentials_included"], False)
        with zipfile.ZipFile(target) as archive:
            manifest = json.loads(archive.read("bundle-manifest.json"))
            self.assertEqual(manifest["product"], "inquiry-assistant-macos")
            self.assertIn("app/inquiry_product/__init__.py", manifest["files"])
            self.assertIn("bin/whatsapp-bridge", manifest["files"])
            self.assertIn("runtime/python-macos.pkg", manifest["files"])
            self.assertIn("LICENSES/InquiryAssistant-MIT.txt", manifest["files"])
            self.assertIn("LICENSES/InquiryAssistant-THIRD-PARTY.md", manifest["files"])
            self.assertEqual(set(archive.namelist()), set(manifest["files"]) | {"bundle-manifest.json"})
            for name, digest in manifest["files"].items():
                data = archive.read(name)
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
                self.assertNotIn(b"PRIVATE_SENTINEL_DO_NOT_SHIP", data)
            self.assertTrue((archive.getinfo("01-\u73b0\u573a\u5b89\u88c5.command").external_attr >> 16) & 0o100)

    def test_changed_binary_is_rejected_before_archive_is_created(self):
        module = self.module()
        product, components, bridge = self.fixture()
        (components / "codex").write_bytes(b"wrong executable")
        target = self.root / "customer-kit.zip"
        with self.assertRaises(ValueError):
            module.package(product, components, bridge, target)
        self.assertFalse(target.exists())

    def test_symlink_inside_product_payload_is_rejected(self):
        module = self.module()
        product, components, bridge = self.fixture()
        secret = self.root / "private.py"
        secret.write_text("PRIVATE_SENTINEL_DO_NOT_SHIP")
        (product / "inquiry_product" / "unexpected.py").symlink_to(secret)
        with self.assertRaises(ValueError):
            module.package(product, components, bridge, self.root / "customer-kit.zip")

    def test_existing_delivery_is_never_replaced(self):
        module = self.module()
        product, components, bridge = self.fixture()
        target = self.root / "customer-kit.zip"
        target.write_bytes(b"existing-delivery")
        with self.assertRaises(FileExistsError):
            module.package(product, components, bridge, target)
        self.assertEqual(target.read_bytes(), b"existing-delivery")


if __name__ == "__main__":
    unittest.main()
