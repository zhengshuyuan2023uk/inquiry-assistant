"""Workflow orchestration independent of a channel or model provider."""
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import ProjectConfig, parse_day
from .engine import normalize_request, validate_result
from .store import normalize_message


def utc_day(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc).date()


class InquiryService:
    def __init__(self, store, configs_dir, runner):
        self.store = store
        self.configs_dir = Path(configs_dir)
        self.runner = runner
        self.mode = store.mode
        self.projects()

    def project(self, project_id):
        project = ProjectConfig.load(self.configs_dir, project_id)
        if project.data["mode"] != self.store.mode:
            raise ValueError("项目 mode 与数据库 mode 不符")
        return project

    def projects(self):
        return [self.project(path.stem).data for path in sorted(self.configs_dir.glob("*.json"))]

    def add_message(self, message):
        message = normalize_message(message)
        self.project(message["project_id"])
        return self.store.ingest(message)

    def import_scenarios(self, file, through_day):
        """Replay all arrivals up to a logical UTC date, including late messages."""
        day = parse_day(through_day)
        source = json.loads(Path(file).read_text(encoding="utf-8"))
        batch = []
        # Validate the full selected batch before writes. SQLite dedup remains authoritative.
        for scenario in source["scenarios"]:
            for raw in scenario["messages"]:
                message = normalize_message(raw)
                if utc_day(message["received_at"]) > day:
                    continue
                self.project(message["project_id"])
                batch.append(message)
        batch.sort(key=lambda m: datetime.fromisoformat(m["received_at"].replace("Z", "+00:00")))
        imported = self.store.ingest_many(batch)
        return {"mode": self.mode, "through_day": through_day, **imported, "total_selected": len(batch)}

    def context(self, project_id, account_id, conversation_id, as_of, request=None):
        day = parse_day(as_of)
        context = self.project(project_id).context(as_of)
        context["messages"] = self.store.messages(project_id, account_id, conversation_id)
        if not context["messages"]:
            raise ValueError("会话没有消息；请先导入或新增一条当前模式的消息")
        if any(max(utc_day(m["sent_at"]), utc_day(m["received_at"])) > day for m in context["messages"]):
            raise ValueError("会话已有晚于指定日期的消息，请使用当前日期；历史依据可查看草稿快照")
        if request is not None:
            request = normalize_request(request)
            if request["mode"] == "refine":
                base = self.store.get_draft(request["base_draft_id"])
                if (base["project_id"], base["account_id"], base["conversation_id"]) != (
                        project_id, account_id, conversation_id):
                    raise ValueError("个性化调整只能使用当前客户会话的回复")
                if (base["status"] == "stale" or base["as_of"] > as_of
                        or base["fingerprint"] != self.store.fingerprint(project_id, account_id, conversation_id)
                        or base["knowledge_digest"] != context["knowledge_digest"]):
                    raise ValueError("原回复的聊天或企业资料已变化，请先重新生成回复")
                if not request["original_text"]:
                    request["original_text"] = base.get("final_text") or base["result"]["reply"]
                # Apply length checks to the persisted fallback as well.
                request = normalize_request(request)
            # An operator instruction is never ingested as a customer message or
            # turned into an enterprise fact. Its own snapshot preserves origin.
            context["request"] = request
        return context

    def analyze(self, project_id, account_id, conversation_id, as_of, request=None):
        fingerprint = self.store.fingerprint(project_id, account_id, conversation_id)
        context = self.context(project_id, account_id, conversation_id, as_of, request=request)
        result = validate_result(self.runner.analyze(context), context)
        current = self.project(project_id).context(as_of)
        if current["knowledge_digest"] != context["knowledge_digest"]:
            raise ValueError("分析期间企业资料变化，请使用新资料重新分析")
        return self.store.save_draft(project_id, account_id, conversation_id, fingerprint,
                                     context["knowledge_digest"], as_of, result, self.runner.name,
                                     context=context)

    def review(self, draft_id, decision, reviewer, final_text, as_of):
        parse_day(as_of)
        draft = self.store.get_draft(draft_id)
        current = self.project(draft["project_id"]).context(as_of)
        return self.store.review(draft_id, decision, reviewer, final_text,
                                 current["knowledge_digest"], as_of)

    def daily(self, project_id, day):
        self.project(project_id)
        parse_day(day)
        result = self.store.daily(project_id, day)
        result["interpretation"] = ("仅汇总模拟事件；未评估真实销售效果，未推断成交或话术因果。"
                                    if self.mode == "simulation" else
                                    "汇总已导入业务记录与人工审核事件；审核输出待人工发送，未验证销售效果或推断话术因果。")
        return result
