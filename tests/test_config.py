import json
import tempfile
import unittest
from pathlib import Path

from inquiry_product.core.config import ProjectConfig, parse_day


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = {"id": "a", "mode": "simulation", "name": "模拟 A", "industry": "test",
                        "required_fields": ["quantity"], "rules": ["不承诺"], "knowledge": [
                            {"id": "current", "version": "1", "title": "当前", "content": "MOQ 100",
                             "valid_from": "2026-09-25", "valid_until": "2026-09-26"},
                            {"id": "expired", "version": "1", "title": "旧价", "content": "SECRET OLD PRICE",
                             "valid_from": "2026-09-01", "valid_until": "2026-09-24"}]}
        self.write()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self):
        (self.root / "a.json").write_text(json.dumps(self.project), encoding="utf-8")

    def test_expired_content_excluded_and_digest_changes_only_with_effective_context(self):
        first = ProjectConfig.load(self.root, "a").context("2026-09-25")
        second = ProjectConfig.load(self.root, "a").context("2026-09-26")
        third = ProjectConfig.load(self.root, "a").context("2026-09-27")
        self.assertNotIn("SECRET OLD PRICE", json.dumps(first))
        self.assertEqual(first["knowledge_digest"], second["knowledge_digest"])
        self.assertNotEqual(first["knowledge_digest"], third["knowledge_digest"])

    def test_changed_policy_or_document_invalidates_digest(self):
        before = ProjectConfig.load(self.root, "a").context("2026-09-25")
        self.project["rules"].append("新增销售规则")
        self.write()
        after = ProjectConfig.load(self.root, "a").context("2026-09-25")
        self.assertNotEqual(before["knowledge_digest"], after["knowledge_digest"])

    def test_invalid_project_paths_real_mode_and_dates_rejected(self):
        with self.assertRaises(ValueError):
            ProjectConfig.load(self.root, "../a")
        self.project["mode"] = "production"
        self.write()
        with self.assertRaises(ValueError):
            ProjectConfig.load(self.root, "a")
        for value in ["2026-02-30", "20260925", "2026-09-25T00:00:00"]:
            with self.assertRaises(ValueError):
                parse_day(value)

    def test_duplicate_document_identity_and_empty_fields_rejected(self):
        self.project["knowledge"].append(self.project["knowledge"][0].copy())
        self.write()
        with self.assertRaises(ValueError):
            ProjectConfig.load(self.root, "a")


if __name__ == "__main__":
    unittest.main()
