"""Verify only approved runtimes can be extracted, and local data is untouched."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


class MacComponentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = Path(__file__).resolve().parents[1] / "scripts/prepare_mac_components.py"
        spec = importlib.util.spec_from_file_location("mac_components_test", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def fixture(self, corrupt=False):
        bundle = self.root / "fixture.zip"
        payload = {name: b"synthetic public component" for name in self.module.FILES}
        manifest = {"files": {n: hashlib.sha256(v).hexdigest() for n, v in payload.items()}}
        if corrupt:
            payload["bin/codex"] = b"modified component"
        with zipfile.ZipFile(bundle, "x") as archive:
            for name, value in payload.items():
                archive.writestr(name, value)
            archive.writestr("private/auth.json", "DO_NOT_COPY")
            archive.writestr("bundle-manifest.json", json.dumps(manifest))
        self.module.R2_SHA256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
        return bundle

    def test_extracts_only_selected_components(self):
        self.module.prepare(self.fixture(), self.root / "components")
        files = {str(p.relative_to(self.root / "components")) for p in (self.root / "components").rglob("*") if p.is_file()}
        self.assertEqual(files, set(self.module.FILES.values()))
        self.assertTrue((self.root / "components/codex").stat().st_mode & 0o100)

    def test_wrong_archive_hash_does_not_create_output(self):
        bundle = self.fixture()
        self.module.R2_SHA256 = "0" * 64
        with self.assertRaisesRegex(ValueError, "verified"):
            self.module.prepare(bundle, self.root / "components")
        self.assertFalse((self.root / "components").exists())

    def test_manifest_mismatch_does_not_create_output(self):
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.module.prepare(self.fixture(corrupt=True), self.root / "components")
        self.assertFalse((self.root / "components").exists())

    def test_existing_output_is_preserved(self):
        folder = self.root / "components"
        folder.mkdir()
        marker = folder / "existing"
        marker.write_text("preserve")
        with self.assertRaises(FileExistsError):
            self.module.prepare(self.fixture(), folder)
        self.assertEqual(marker.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
