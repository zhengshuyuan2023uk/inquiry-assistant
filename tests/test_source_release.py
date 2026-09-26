"""A source release must rebuild itself without carrying local runtime state."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile


class SourceReleaseTests(unittest.TestCase):
    def test_source_zip_has_build_inputs_and_licenses_but_no_runtime_state(self):
        script = Path(__file__).resolve().parents[1] / "package_release.py"
        spec = importlib.util.spec_from_file_location("source_release_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            essential = {
                "LICENSE", "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md", ".gitignore",
                "scripts/build_mac_bridge.py", "scripts/prepare_mac_components.py",
                "deploy/macos/bridge_main.go", "deploy/macos/.运行入口.command",
                "third_party/whatsapp-mcp/LICENSE",
                "third_party/whatsapp-mcp/PROVENANCE.json",
                "third_party/whatsapp-mcp/whatsapp-bridge/go.mod",
                "third_party/whatsapp-mcp/whatsapp-bridge/go.sum",
                "third_party/whatsapp-mcp/whatsapp-bridge/main.go",
            }
            for name in set(module.FILES) | essential:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic source fixture\n")
            for name in ("workspaces/customer/auth.json", "output/secrets.json", "reports/private.md"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("DO_NOT_DISTRIBUTE")
            module.ROOT = root
            target = root / "release.zip"
            module.package(target)
            with zipfile.ZipFile(target) as archive:
                self.assertTrue(essential <= set(archive.namelist()), essential - set(archive.namelist()))
                for name in archive.namelist():
                    self.assertNotIn(b"DO_NOT_DISTRIBUTE", archive.read(name))


if __name__ == "__main__":
    unittest.main()
