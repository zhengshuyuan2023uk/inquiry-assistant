"""Durable local job metadata, excluding prompts, reply previews and secrets."""
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3

from .core.engine import MODEL_SLUG


STATUSES = frozenset(("queued", "running", "cancelling", "succeeded", "failed",
                      "cancelled", "interrupted"))
UNFINISHED = frozenset(("queued", "running", "cancelling"))
TERMINAL = STATUSES - UNFINISHED
RESTART_ERROR = "工作台已重新启动，上次生成未完成；请重新生成"
_STAGE = {"queued": 0, "running": 1, "cancelling": 2}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value, name, maximum=256, optional=False):
    if optional and (value is None or value == ""):
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} 必须是最多 {maximum} 字的非空文本")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError(f"{name} 包含不支持的控制字符")
    return value


def _timestamp(value, name, optional=False):
    if value is None and optional:
        return None
    _text(value, name, 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} 必须是带时区的 ISO 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} 必须带时区")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _request(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("request 必须是对象或空值")
    # Do not normalize the full generation request: that would manufacture other
    # input fields and risk retaining operator text in this operational table.
    result = {key: value[key] for key in ("mode", "model") if key in value}
    if "mode" in result and result["mode"] not in ("generate", "refine", "polish"):
        raise ValueError("request.mode 不受支持")
    model = result.get("model")
    if model is not None and (not isinstance(model, str) or not MODEL_SLUG.fullmatch(model)):
        raise ValueError("request.model 必须是安全模型标识或空值")
    return result


def _record(job, company_id):
    if not isinstance(job, dict):
        raise ValueError("job 必须是对象")
    for field in ("company_id", "project_id"):
        if field in job and job[field] != company_id:
            raise ValueError("任务不属于当前企业工作区")
    status = job.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError("任务状态不受支持")
    revision = job.get("revision", 0)
    if type(revision) is not int or not 0 <= revision <= 9_223_372_036_854_775_806:
        raise ValueError("任务 revision 必须是非负整数")
    result = {key: _text(job.get(key), key) for key in ("id", "account_id", "conversation_id")}
    result.update(status=status, started_at=_timestamp(job.get("started_at"), "started_at"),
                  finished_at=_timestamp(job.get("finished_at"), "finished_at", optional=True),
                  draft_id=_text(job.get("draft_id"), "draft_id", optional=True),
                  error=_text(job.get("error"), "error", 3000, optional=True),
                  warning=_text(job.get("warning"), "warning", 1000, optional=True),
                  progress_message=_text(job.get("progress_message"), "progress_message", 1000, optional=True),
                  revision=revision, request=_request(job.get("request")))
    if status in TERMINAL and result["finished_at"] is None:
        result["finished_at"] = _now()
    return result


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class JobStore:
    """One company's additive job table, separate from model run telemetry.

    Terminal records are immutable. Older revisions and backwards nonterminal
    transitions are ignored so delayed progress cannot revive a finished job.
    Every operation owns and closes its SQLite connection. Reads do not write.
    """

    def __init__(self, workspace):
        self.workspace = workspace
        self.company_id = workspace.company_id
        self.mode = workspace.mode
        self.created_at = workspace.manifest["created_at"]
        self.db_path = workspace.db_path
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS workbench_jobs (
                company_id TEXT NOT NULL, id TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, revision INTEGER NOT NULL, job_json TEXT NOT NULL,
                PRIMARY KEY(company_id,id)
            )""")
            connection.execute("""CREATE INDEX IF NOT EXISTS workbench_jobs_recent
                ON workbench_jobs(company_id,started_at DESC,id DESC)""")

    def _connect(self):
        current = self.workspace._fresh()
        if ((current.company_id, current.mode, current.manifest["created_at"])
                != (self.company_id, self.mode, self.created_at)):
            raise ValueError("任务工作区绑定已改变")
        # mode=rw refuses to manufacture an empty database if its file vanishes.
        connection = sqlite3.connect(self.db_path.absolute().as_uri() + "?mode=rw", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            if metadata.get("company_id") != self.company_id or metadata.get("mode") != self.mode:
                raise ValueError("任务数据库与企业工作区绑定不一致")
        except BaseException:
            connection.close()
            raise
        return connection

    def save(self, job):
        record = _record(job, self.company_id)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT job_json FROM workbench_jobs WHERE company_id=? AND id=?",
                                     (self.company_id, record["id"])).fetchone()
            if row:
                prior = json.loads(row["job_json"])
                if any(prior[field] != record[field] for field in ("account_id", "conversation_id", "started_at", "request")):
                    raise ValueError("已有任务的会话、开始时间与请求标识不可替换")
                if prior["status"] in TERMINAL or record["revision"] < prior["revision"]:
                    return
                if (record["status"] in UNFINISHED
                        and _STAGE[record["status"]] < _STAGE[prior["status"]]):
                    return
            connection.execute("""INSERT INTO workbench_jobs
                (company_id,id,status,started_at,revision,job_json) VALUES (?,?,?,?,?,?)
                ON CONFLICT(company_id,id) DO UPDATE SET status=excluded.status,
                revision=excluded.revision,job_json=excluded.job_json""",
                (self.company_id, record["id"], record["status"], record["started_at"],
                 record["revision"], _json(record)))

    def get(self, identity):
        _text(identity, "id")
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT job_json FROM workbench_jobs WHERE company_id=? AND id=?",
                                     (self.company_id, identity)).fetchone()
            return json.loads(row["job_json"]) if row else None

    def recent(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit 必须是 1–1000 的整数")
        with closing(self._connect()) as connection:
            rows = connection.execute("""SELECT job_json FROM workbench_jobs WHERE company_id=?
                ORDER BY started_at DESC,id DESC LIMIT ?""", (self.company_id, limit)).fetchall()
            return [json.loads(row["job_json"]) for row in rows]

    def recover_interrupted(self):
        finished_at = _now()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("""SELECT job_json FROM workbench_jobs WHERE company_id=?
                AND status IN ('queued','running','cancelling')""", (self.company_id,)).fetchall()
            for row in rows:
                job = json.loads(row["job_json"])
                job.update(status="interrupted", error=RESTART_ERROR, progress_message=RESTART_ERROR,
                           finished_at=finished_at, revision=job["revision"] + 1)
                connection.execute("""UPDATE workbench_jobs SET status=?,revision=?,job_json=?
                    WHERE company_id=? AND id=?""",
                    (job["status"], job["revision"], _json(job), self.company_id, job["id"]))
            return len(rows)
