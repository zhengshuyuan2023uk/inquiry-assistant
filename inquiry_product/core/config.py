"""Project-scoped, validity-aware knowledge with explicit input provenance."""
import hashlib
import json
import re
from datetime import date
from pathlib import Path


def parse_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期格式必须为 YYYY-MM-DD")
    return date.fromisoformat(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须为非空字符串")
    return value


class ProjectConfig:
    def __init__(self, data):
        self.data = data
        if not isinstance(data, dict) or data.get("mode") not in ("simulation", "customer"):
            raise ValueError("项目 mode 必须为 simulation 或 customer")
        for key in ("id", "name", "industry"):
            nonempty(data.get(key), key)
        strategy = data.get("reply_strategy", "")
        if not isinstance(strategy, str) or len(strategy) > 12000:
            raise ValueError("reply_strategy 必须是最多 12000 字的文本")
        for key in ("required_fields", "rules"):
            values = data.get(key)
            if not isinstance(values, list) or not values:
                raise ValueError(f"{key} 必须是非空列表")
            for value in values:
                nonempty(value, key)
            if len(set(values)) != len(values):
                raise ValueError(f"{key} 不能重复")
        if not isinstance(data.get("knowledge"), list):
            raise ValueError("knowledge 必须是列表")
        seen = set()
        for doc in data["knowledge"]:
            if not isinstance(doc, dict):
                raise ValueError("资料必须是对象")
            for key in ("id", "version", "title", "content"):
                nonempty(doc.get(key), f"knowledge.{key}")
            if doc["id"] in seen:
                raise ValueError("每份资料 ID 必须唯一（更版请更新原项）")
            seen.add(doc["id"])
            if parse_day(doc.get("valid_from")) > parse_day(doc.get("valid_until")):
                raise ValueError("资料生效日期不能晚于到期日")

    @classmethod
    def load(cls, directory, project_id):
        if not isinstance(project_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", project_id):
            raise ValueError("非法项目 ID")
        path = Path(directory) / f"{project_id}.json"
        if not path.is_file():
            raise ValueError(f"项目配置不存在：{project_id}")
        instance = cls(json.loads(path.read_text(encoding="utf-8")))
        if instance.data["id"] != project_id:
            raise ValueError("文件名与项目 ID 不符")
        return instance

    def context(self, as_of):
        day = parse_day(as_of)
        included, excluded = [], []
        for doc in self.data["knowledge"]:
            start, end = parse_day(doc["valid_from"]), parse_day(doc["valid_until"])
            if start <= day <= end:
                included.append(dict(doc))
            else:
                excluded.append({key: doc[key] for key in ("id", "version", "title")} |
                                {"exclusion": "expired" if end < day else "not_yet_valid"})
        context = {"mode": self.data["mode"], "project_id": self.data["id"],
                   "project_name": self.data["name"], "industry": self.data["industry"],
                   "required_fields": list(self.data["required_fields"]),
                   "rules": list(self.data["rules"]), "knowledge": included,
                   "excluded_knowledge": excluded}
        # Empty/absent strategy preserves pre-workbench draft digests. A real
        # strategy change is part of knowledge freshness, not a runtime setting.
        if self.data.get("reply_strategy", "").strip():
            context["reply_strategy"] = self.data["reply_strategy"]
        # As-of itself is not hashed: a new date with identical effective knowledge is valid.
        context["knowledge_digest"] = digest(context)
        context["as_of"] = as_of
        return context
