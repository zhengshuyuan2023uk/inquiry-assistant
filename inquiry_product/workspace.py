"""Single-company local workspaces, immutable knowledge and verified backups.

The manifest is the sole current-release pointer.  Each immutable release is a
ProjectConfig-compatible directory; publishing switches one file atomically,
so a failed publication never exposes half a release.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile

from .core.config import ProjectConfig
from .core.engine import validate_result
from .core.service import InquiryService
from .core.store import Store, message_payload, normalize_message


SCHEMA_VERSION = 2
VERSION = "0.2.0"
_ID = re.compile(r"[a-zA-Z0-9_-]+\Z")
_RELEASE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ARCHIVE_BYTES = 2 * 1024 ** 3
_MAX_ARCHIVE_FILES = 10000
_AUDIT_SQL = """CREATE TABLE IF NOT EXISTS import_batches (
    batch_id TEXT PRIMARY KEY,
    source_label TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    inserted INTEGER NOT NULL,
    duplicates INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
)"""
_REQUIRED_COLUMNS = {
    "metadata": "key value",
    "messages": "project_id account_id conversation_id message_id direction body sent_at received_at mode sent_utc received_utc payload_hash",
    "drafts": "id project_id account_id conversation_id fingerprint knowledge_digest as_of result_json context_json runner status final_text reviewed_as_of created_at",
    "draft_states": "id draft_id status as_of final_text reviewed_as_of",
    "review_events": "id draft_id decision outcome reviewer final_text as_of fingerprint knowledge_digest created_at",
    "outbox": "id draft_id final_text as_of mode delivery_state created_at",
    "import_batches": "batch_id source_label record_count inserted duplicates imported_at payload_sha256",
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_bytes(value):
    try:
        return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("配置或数据必须是有效 JSON") from exc


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("无法读取有效 JSON 文件") from exc


def _valid_binding(company_id, mode):
    if not isinstance(company_id, str) or not _ID.fullmatch(company_id):
        raise ValueError("企业 ID 仅允许英文字母、数字、下划线和短横线")
    if mode not in ("simulation", "customer"):
        raise ValueError("工作区模式必须是 simulation 或 customer")


def _config(source, company_id, mode):
    data = source if isinstance(source, dict) else _read_json(Path(source))
    # A JSON round trip both makes a private copy and rejects non-JSON inputs.
    data = json.loads(_json_bytes(data))
    try:
        config = ProjectConfig(data)
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError("企业知识配置未通过校验") from exc
    if config.data["id"] != company_id or config.data["mode"] != mode:
        raise ValueError("配置企业 ID 和模式必须与工作区完全一致")
    if mode == "customer" and re.search(
            r"虚构|演练|\b(?:demo|simulation|synthetic|fictitious)\b", data["name"], re.I):
        raise ValueError("客户工作区不能使用名称标记为虚构或演练的样本")
    return data


def _regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("工作区缺少普通文件或包含不允许的符号链接")


def _directory(path):
    if path.is_symlink() or not path.is_dir():
        raise ValueError("工作区缺少目录或包含不允许的符号链接")


def _mkdir(path):
    if not path.parent.exists():
        _mkdir(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    _directory(path)
    path.chmod(0o700)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _exclusive_write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _atomic_write(path, content):
    fd, name = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _new_target(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
        raise ValueError("目标必须是不存在的路径或空目录；不会覆盖现有内容")
    return path


def _install_directory(stage, target):
    _new_target(target)
    # POSIX rename permits replacing only an empty directory; it cannot overwrite
    # a file or a nonempty directory created concurrently after the check.
    os.rename(stage, target)
    _fsync_dir(target.parent)


def _sqlite_readonly(path):
    return sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=10)


def _check_database(path, company_id, mode, integrity=False):
    _regular(path)
    try:
        with closing(_sqlite_readonly(path)) as conn:
            for table, columns in _REQUIRED_COLUMNS.items():
                actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if not set(columns.split()).issubset(actual):
                    raise ValueError("数据库缺少必要的表或字段")
            metadata = dict(conn.execute("SELECT key,value FROM metadata"))
            if (metadata.get("schema_version") != str(SCHEMA_VERSION)
                    or metadata.get("mode") != mode
                    or metadata.get("company_id") != company_id):
                raise ValueError("数据库与工作区绑定不一致")
            for table in ("messages", "drafts"):
                if conn.execute(f"SELECT 1 FROM {table} WHERE project_id<>? LIMIT 1", (company_id,)).fetchone():
                    raise ValueError("数据库包含其他企业的数据")
            for table in ("messages", "outbox"):
                if conn.execute(f"SELECT 1 FROM {table} WHERE mode<>? LIMIT 1", (mode,)).fetchone():
                    raise ValueError("数据库记录模式与工作区不一致")
            conn.execute("SELECT batch_id FROM import_batches LIMIT 1")
            if integrity and conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("数据库完整性校验失败")
            if integrity and conn.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("数据库外键完整性校验失败")
    except sqlite3.Error as exc:
        raise ValueError("工作区数据库结构或完整性校验失败") from exc


class _WorkspaceService(InquiryService):
    """Read the active pointer again on every config access, including reviews."""
    def __init__(self, workspace, runner):
        self.workspace = workspace
        self.workspace_root = workspace.root
        self.company_id = workspace.company_id
        super().__init__(workspace.open_store(), workspace.configs_dir, runner)

    def project(self, project_id):
        if project_id != self.company_id:
            raise ValueError("工作区仅允许访问其绑定企业")
        current = Workspace.load(self.workspace_root)
        if current.company_id != self.company_id or current.mode != self.store.mode:
            raise ValueError("工作区绑定已改变")
        self.configs_dir = current.configs_dir
        return super().project(project_id)

    def projects(self):
        return [self.project(self.company_id).data]

    def analyze(self, project_id, account_id, conversation_id, as_of, request=None):
        fingerprint = self.store.fingerprint(project_id, account_id, conversation_id)
        context = self.context(project_id, account_id, conversation_id, as_of, request=request)
        # A slow model call must not hold the publication lock.
        result = validate_result(self.runner.analyze(context), context)
        with self.workspace._locked():
            current = self.project(project_id).context(as_of)
            if current["knowledge_digest"] != context["knowledge_digest"]:
                raise ValueError("分析期间企业资料变化，请使用新资料重新分析")
            return self.store.save_draft(project_id, account_id, conversation_id, fingerprint,
                                         context["knowledge_digest"], as_of, result, self.runner.name,
                                         context=context)

    def review(self, draft_id, decision, reviewer, final_text, as_of):
        # Share the publication lock through the database commit; rereading the
        # config without this lock leaves a check-to-commit race.
        with self.workspace._locked():
            return super().review(draft_id, decision, reviewer, final_text, as_of)

    def adopt(self, draft_id, reviewer, final_text, as_of):
        """Save the selected wording without claiming clipboard or delivery success.

        An already adopted reply can be revised; each distinct revision gets a
        new draft so the original approval and wording are never overwritten.
        """
        if not isinstance(final_text, str) or not final_text.strip() or len(final_text) > 50_000:
            raise ValueError('最终回复必须是 1–50000 字的非空文本')
        with self.workspace._locked():
            draft = self.store.get_draft(draft_id)
            if draft['project_id'] != self.company_id:
                raise ValueError('工作区仅允许采用其绑定企业的回复')
            context = self.project(self.company_id).context(as_of)
            scope = (self.company_id, draft['account_id'], draft['conversation_id'])
            if (draft['status'] == 'stale' or draft['fingerprint'] != self.store.fingerprint(*scope)
                    or draft['knowledge_digest'] != context['knowledge_digest']):
                raise ValueError('回复依据已变化，请重新生成后复制')
            if draft['status'] == 'rejected':
                raise ValueError('该回复已经驳回，请重新生成后复制')
            if draft['status'] == 'approved' and draft['final_text'] == final_text:
                return draft
            if draft['status'] == 'approved':
                snapshot = json.loads(json.dumps(draft['context'])) if draft['context'] else None
                if snapshot is not None:
                    snapshot['manual_revision_of'] = draft['id']
                draft = self.store.save_draft(*scope, draft['fingerprint'], draft['knowledge_digest'],
                    as_of, draft['result'], draft['runner'], context=snapshot)
            return super().review(draft['id'], 'approve', reviewer, final_text, as_of)


class Workspace:
    def __init__(self, root, manifest):
        self.root = root
        self.manifest = manifest
        self.company_id = manifest["company_id"]
        self.mode = manifest["mode"]
        self.configs_dir = root / "knowledge" / "releases" / manifest["active_release"]
        self.db_path = root / "data" / "inquiries.sqlite3"

    @classmethod
    def init(cls, root, company_id, source_config, mode="simulation"):
        _valid_binding(company_id, mode)
        data = _config(source_config, company_id, mode)
        target = _new_target(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".workspace-init-", dir=target.parent))
        try:
            for relative in ("knowledge/releases", "data", "reports", "logs"):
                _mkdir(stage / relative)
            release_id = cls._write_release(stage, company_id, data)
            created_at = _now()
            manifest = {"schema_version": SCHEMA_VERSION, "version": VERSION,
                        "company_id": company_id, "mode": mode, "active_release": release_id,
                        "created_at": created_at, "knowledge_history": [
                            {"action": "init", "release_id": release_id, "previous_release": None,
                             "at": created_at}]}
            _exclusive_write(stage / "workspace.json", _json_bytes(manifest))
            store = Store(stage / "data" / "inquiries.sqlite3", mode=mode)
            try:
                with store.connection:
                    store.connection.execute("INSERT INTO metadata(key,value) VALUES ('company_id',?)", (company_id,))
                    store.connection.execute(_AUDIT_SQL)
            finally:
                store.close()
            (stage / "data" / "inquiries.sqlite3").chmod(0o600)
            cls.load(stage)
            _install_directory(stage, target)
            return cls.load(target)
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise

    @staticmethod
    def _write_release(root, company_id, data):
        content = _json_bytes(data)
        release_id = _sha(content)
        directory = root / "knowledge" / "releases" / release_id
        if directory.exists() or directory.is_symlink():
            _directory(directory)
            paths = list(directory.iterdir())
            if len(paths) != 1 or paths[0].name != company_id + ".json":
                raise ValueError("已有不可变发布包的文件不一致")
            _regular(paths[0])
            if paths[0].read_bytes() != content:
                raise ValueError("已有不可变发布包的内容不一致")
            return release_id
        # Staging is outside releases: interrupted preparation cannot damage the
        # active release inventory.  A complete unreferenced bundle is harmless.
        stage = Path(tempfile.mkdtemp(prefix=".release-", dir=root / "knowledge"))
        try:
            _exclusive_write(stage / (company_id + ".json"), content)
            _fsync_dir(stage)
            os.rename(stage, directory)
            _fsync_dir(directory.parent)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return release_id

    @classmethod
    def load(cls, root):
        root = Path(root).expanduser().absolute()
        _directory(root)
        for relative in ("knowledge", "knowledge/releases", "data", "reports", "logs"):
            _directory(root / relative)
        _regular(root / "workspace.json")
        manifest = _read_json(root / "workspace.json")
        if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("version") != VERSION:
            raise ValueError("不支持的工作区版本")
        company_id, mode = manifest.get("company_id"), manifest.get("mode")
        _valid_binding(company_id, mode)
        current = manifest.get("active_release")
        if not isinstance(current, str) or not _RELEASE.fullmatch(current):
            raise ValueError("非法知识发布编号")
        releases = {}
        for directory in (root / "knowledge" / "releases").iterdir():
            _directory(directory)
            if not _RELEASE.fullmatch(directory.name):
                raise ValueError("知识发布目录包含未知文件")
            paths = list(directory.iterdir())
            if len(paths) != 1 or paths[0].name != company_id + ".json":
                raise ValueError("工作区只允许绑定企业的一份项目配置")
            _regular(paths[0])
            content = paths[0].read_bytes()
            if _sha(content) != directory.name:
                raise ValueError("知识发布摘要不匹配")
            data = _config(_read_json(paths[0]), company_id, mode)
            if _json_bytes(data) != content:
                raise ValueError("知识发布不是规范化内容")
            releases[directory.name] = content
        if current not in releases:
            raise ValueError("当前知识发布不存在")
        history = manifest.get("knowledge_history")
        if not isinstance(history, list) or not history:
            raise ValueError("知识发布历史缺失")
        prior = None
        for index, event in enumerate(history):
            if (not isinstance(event, dict) or event.get("release_id") not in releases
                    or event.get("previous_release") != prior
                    or event.get("action") not in (("init",) if index == 0 else ("publish", "rollback"))
                    or not isinstance(event.get("at"), str)):
                raise ValueError("知识发布历史不一致")
            prior = event["release_id"]
        if prior != current:
            raise ValueError("当前知识发布与历史不一致")
        _check_database(root / "data" / "inquiries.sqlite3", company_id, mode)
        return cls(root, manifest)

    @contextmanager
    def _locked(self):
        lock_path = self.root / ".workspace.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("工作区锁文件类型不正确")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _fresh(self):
        current = self.load(self.root)
        if current.company_id != self.company_id or current.mode != self.mode:
            raise ValueError("工作区绑定已改变")
        return current

    def open_store(self):
        self._fresh()
        return Store(self.db_path, mode=self.mode)

    def service(self, runner):
        return _WorkspaceService(self, runner)

    def _switch(self, current, release_id, action):
        previous = current.manifest["active_release"]
        if release_id == previous:
            self.manifest = current.manifest
            self.configs_dir = current.configs_dir
            return {"release_id": release_id, "previous_release": previous, "changed": False}
        manifest = json.loads(_json_bytes(current.manifest))
        manifest["active_release"] = release_id
        manifest["knowledge_history"].append({"action": action, "release_id": release_id,
                                             "previous_release": previous, "at": _now()})
        _atomic_write(self.root / "workspace.json", _json_bytes(manifest))
        self.manifest = manifest
        self.configs_dir = self.root / "knowledge" / "releases" / release_id
        return {"release_id": release_id, "previous_release": previous, "changed": True}

    def publish(self, source_config, expected_release=None):
        data = _config(source_config, self.company_id, self.mode)
        with self._locked():
            current = self._fresh()
            if expected_release is not None and current.manifest['active_release'] != expected_release:
                raise ValueError('企业资料已在其他页面更新，请重新载入后保存')
            release_id = self._write_release(self.root, self.company_id, data)
            return self._switch(current, release_id, "publish")

    def rollback(self, release_id):
        if not isinstance(release_id, str) or not _RELEASE.fullmatch(release_id):
            raise ValueError("回滚编号必须是 64 位小写十六进制摘要")
        with self._locked():
            current = self._fresh()
            if not (self.root / "knowledge" / "releases" / release_id).is_dir():
                raise ValueError("所选知识发布不存在")
            return self._switch(current, release_id, "rollback")

    def import_messages(self, records, source_label):
        if not isinstance(records, list):
            raise ValueError("导入记录必须是列表")
        if (not isinstance(source_label, str) or not source_label.strip() or len(source_label) > 120
                or any(ord(char) < 32 for char in source_label)
                or any(char in source_label for char in ("/", "\\"))):
            raise ValueError("来源标签必须是简短逻辑名称，不能是文件路径")
        normalized = []
        for record in records:
            if (not isinstance(record, dict) or record.get("project_id") != self.company_id
                    or record.get("mode") != self.mode
                    or ("company_id" in record and record["company_id"] != self.company_id)):
                raise ValueError("整批记录必须显式匹配工作区企业和模式")
            normalized.append(normalize_message(record))
        payload_sha256 = _sha(_json_bytes([message_payload(record) for record in normalized]))
        batch_id = _sha(_json_bytes({"company_id": self.company_id, "mode": self.mode,
                                     "source_label": source_label, "payload_sha256": payload_sha256}))
        with self._locked():
            store = self.open_store()
            conn = store.connection
            try:
                conn.execute("BEGIN IMMEDIATE")
                prior = conn.execute("SELECT * FROM import_batches WHERE batch_id=?", (batch_id,)).fetchone()
                if prior:
                    result = dict(prior)
                    result.update(inserted=0, duplicates=len(normalized), replayed=True)
                else:
                    counts = store.ingest_many(normalized)
                    result = {"batch_id": batch_id, "source_label": source_label,
                              "record_count": len(normalized), "inserted": counts["inserted"],
                              "duplicates": counts["duplicates"], "imported_at": _now(),
                              "payload_sha256": payload_sha256}
                    conn.execute("""INSERT INTO import_batches VALUES
                        (:batch_id,:source_label,:record_count,:inserted,:duplicates,:imported_at,:payload_sha256)""", result)
                    result["replayed"] = False
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
            finally:
                store.close()

    def _backup_files(self):
        files = {"workspace.json": self.root / "workspace.json"}
        for directory in ("knowledge/releases", "reports", "logs"):
            for path in (self.root / directory).rglob("*"):
                if path.is_symlink():
                    raise ValueError("备份拒绝工作区中的符号链接")
                if path.is_file():
                    _regular(path)
                    files[path.relative_to(self.root).as_posix()] = path
                elif not path.is_dir():
                    raise ValueError("备份拒绝非常规文件")
        return files

    def backup(self, destination):
        destination = Path(destination).expanduser().absolute()
        if destination.exists() or destination.is_symlink():
            raise ValueError("备份目标已存在；不会覆盖")
        if destination == self.root or self.root in destination.parents:
            raise ValueError("备份文件必须保存在工作区之外")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._locked(), tempfile.TemporaryDirectory(prefix=".workspace-backup-", dir=destination.parent) as temporary:
            current = self._fresh()
            temporary = Path(temporary)
            database = temporary / "inquiries.sqlite3"
            with closing(_sqlite_readonly(self.db_path)) as source, closing(sqlite3.connect(database)) as target:
                source.backup(target)
            database.chmod(0o600)
            _check_database(database, self.company_id, self.mode, integrity=True)
            files = current._backup_files()
            files["data/inquiries.sqlite3"] = database
            manifest = {"backup_format": 1, "created_at": _now(), "company_id": self.company_id,
                        "mode": self.mode, "workspace_schema": SCHEMA_VERSION, "files": {}}
            archive_path = temporary / "backup.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, path in sorted(files.items()):
                    content = path.read_bytes()
                    manifest["files"][name] = {"sha256": _sha(content), "size": len(content)}
                    archive.writestr(name, content)
                archive.writestr("backup_manifest.json", _json_bytes(manifest))
            archive_path.chmod(0o600)
            with archive_path.open("rb") as ready:
                os.fsync(ready.fileno())
            # A hard link publishes the complete archive without replacing an
            # existing file, including one created after our initial check.
            try:
                os.link(archive_path, destination)
            except FileExistsError as exc:
                raise ValueError("备份目标已存在；不会覆盖") from exc
            _fsync_dir(destination.parent)
            return {"archive": str(destination), "sha256": _sha(archive_path.read_bytes()),
                    "file_count": len(files)}

    @staticmethod
    def _archive_members(archive):
        members = archive.infolist()
        if len(members) > _MAX_ARCHIVE_FILES or sum(item.file_size for item in members) > _MAX_ARCHIVE_BYTES:
            raise ValueError("备份超过安全恢复大小限制")
        seen = set()
        for item in members:
            name = item.filename
            path = PurePosixPath(name)
            mode = item.external_attr >> 16
            if (not name or "\\" in name or ":" in name or path.is_absolute()
                    or any(part in ("", ".", "..") for part in name.split("/"))
                    or path.as_posix() != name or name in seen or item.is_dir()
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or item.flag_bits & 1):
                raise ValueError("备份包含不安全路径、重复条目或非常规文件")
            if name not in ("workspace.json", "backup_manifest.json", "data/inquiries.sqlite3"):
                if not (name.startswith("knowledge/releases/")
                        or name.startswith("reports/") or name.startswith("logs/")):
                    raise ValueError("备份包含未允许的文件")
            seen.add(name)
        if not {"workspace.json", "backup_manifest.json", "data/inquiries.sqlite3"}.issubset(seen):
            raise ValueError("备份缺少必要文件")
        return {item.filename: item for item in members}

    @classmethod
    def restore(cls, archive, destination):
        target = _new_target(destination)
        archive_path = Path(archive).expanduser().absolute()
        _regular(archive_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".workspace-restore-", dir=target.parent))
        try:
            with zipfile.ZipFile(archive_path) as bundle:
                members = cls._archive_members(bundle)
                if members["backup_manifest.json"].file_size > 1024 * 1024:
                    raise ValueError("备份清单过大")
                manifest = json.loads(bundle.read("backup_manifest.json"))
                if (not isinstance(manifest, dict) or manifest.get("backup_format") != 1
                        or manifest.get("workspace_schema") != SCHEMA_VERSION
                        or not isinstance(manifest.get("files"), dict)):
                    raise ValueError("不支持的备份格式")
                expected = manifest["files"]
                if set(expected) != set(members) - {"backup_manifest.json"}:
                    raise ValueError("备份文件与摘要清单不一致")
                for relative in ("knowledge/releases", "data", "reports", "logs"):
                    _mkdir(stage / relative)
                for name, item in expected.items():
                    content = bundle.read(name)
                    if (not isinstance(item, dict) or item.get("sha256") != _sha(content)
                            or item.get("size") != len(content)):
                        raise ValueError("备份文件摘要或大小不匹配")
                    path = stage / name
                    _mkdir(path.parent)
                    _exclusive_write(path, content)
            workspace = cls.load(stage)
            if manifest.get("company_id") != workspace.company_id or manifest.get("mode") != workspace.mode:
                raise ValueError("备份清单与工作区绑定不一致")
            _check_database(workspace.db_path, workspace.company_id, workspace.mode, integrity=True)
            _install_directory(stage, target)
            return cls.load(target)
        except (zipfile.BadZipFile, RuntimeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("备份文件损坏或不可读取") from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def health(self):
        result = {"company_id": self.company_id, "mode": self.mode, "schema_version": SCHEMA_VERSION,
                  "config_valid": False, "database_integrity": False, "ok": False,
                  "readiness": "alpha_requires_pilot"}
        try:
            current = self._fresh()
            result["config_valid"] = True
            _check_database(current.db_path, current.company_id, current.mode, integrity=True)
            result["database_integrity"] = True
            result["ok"] = True
        except (OSError, ValueError, sqlite3.Error):
            result["error"] = "工作区完整性检查未通过"
        return result
