"""Loopback-only, single-workspace HTTP workbench.

create_server(workspace, host='127.0.0.1', port=0, runner_factory=None)
returns a ThreadingHTTPServer; call serve_forever(), then server_close(). No
model starts until an authenticated, CSRF-protected analysis request arrives.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import getpass
import hashlib
import fcntl
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from .core.config import ProjectConfig
from .core.engine import CodexRunner, EngineError, normalize_request
from .core.store import StoreError
from .models import get_model_catalog
from .reply_settings import read_settings, updated_config
from .telemetry import start_run, finish_run, list_runs, recover_workbench_runs
from .workspace import Workspace
from .jobs import JobStore

MAX_BODY_BYTES = 5_000_000
MAX_RECORDS = 2000
DEFAULT_WORKBENCH_MODEL = 'gpt-5.6-luna'
_ACTIVE_STATUSES = ('queued', 'running', 'cancelling')
_COOKIE = 'inquiry_workbench_session'
_STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
           '/index.html': ('index.html', 'text/html; charset=utf-8'),
           '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
           '/styles.css': ('styles.css', 'text/css; charset=utf-8')}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _day():
    return datetime.now(timezone.utc).date().isoformat()


def _text(value, label, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise APIError(400, f'{label}必须是 1–{maximum} 字的非空文本')
    if any(ord(c) < 32 and not (label in ('消息正文', '最终回复', '回复策略') and c in '\n\r\t') for c in value):
        raise APIError(400, f'{label}包含不允许的控制字符')
    return value


def _shape(value, required, optional=()):
    if not isinstance(value, dict) or not set(required).issubset(value) or set(value) - set(required) - set(optional):
        raise APIError(400, '请求字段缺失或包含不支持的字段；工作区和企业由服务器固定绑定')
    return value


def _limit(context):
    if len(context['messages']) > 500 or len(json.dumps(context, ensure_ascii=False)) > 120_000:
        raise APIError(413, '当前会话或资料超出本版输入限额（500 条消息 / 120000 字符），请先人工整理；未截断或调用模型')


def _public_draft(draft):
    """Expose only cited evidence from the draft's immutable model snapshot."""
    result = {key: value for key, value in draft.items() if key != 'context'}
    context = draft.get('context') or {}
    result['request'] = context.get('request') if isinstance(context, dict) else None
    if isinstance(context, dict) and context.get('manual_revision_of'):
        result['manual_revision_of'] = context['manual_revision_of']
    documents = context.get('knowledge', []) if isinstance(context, dict) else []
    lookup = {(doc.get('id'), doc.get('version')): doc for doc in documents if isinstance(doc, dict)}
    sources = []
    for citation in draft.get('result', {}).get('citations', []):
        document = lookup.get((citation.get('id'), citation.get('version')))
        if document:
            sources.append({key: document[key] for key in
                            ('id', 'version', 'title', 'content', 'valid_from', 'valid_until') if key in document})
    result['citation_sources'] = sources
    return result


def _explain(exc):
    if isinstance(exc, EngineError):
        messages = {
            'auth': 'AI 服务需要重新授权，请联系负责人处理后重试',
            'config': 'AI 服务尚未配置完成，请联系负责人检查设置',
            'network': 'AI 服务连接中断，请检查网络后重试',
            'timeout': '本次回复生成超时，请稍后重试',
            'rate_limit': 'AI 服务暂时达到使用限制，请稍后重试或联系负责人',
            'quota': 'AI 服务可用额度不足，请联系负责人处理',
            'cancelled': '生成已停止，未保存草稿',
            'schema': '回复未通过格式校验，未保存草稿，请重新生成',
            'validation': '回复未通过业务校验，未保存草稿，请核对资料后重新生成',
            'tool_access': '本次生成未通过安全校验，已停止，请联系负责人',
        }
        return messages.get(exc.category, 'AI 服务暂不可用，请重试；持续失败时请联系负责人')
    if isinstance(exc, StoreError):
        text = str(exc)
        if 'stale' in text:
            return '草稿已过期：消息或企业知识发生变化，请重新分析后审核'
        if 'does not exist' in text:
            return '草稿不存在'
        if 'already' in text and ('review' in text or 'approved' in text or 'rejected' in text):
            return '草稿已有审核结果，请重新生成后再审核'
        return '数据操作未通过校验：' + text
    if isinstance(exc, (APIError, ValueError)):
        return str(exc)
    if isinstance(exc, sqlite3.Error):
        return '工作区数据库暂不可用，请检查工作区健康后重试'
    if isinstance(exc, OSError):
        return '工作台或 AI 服务暂不可用，请联系负责人检查配置'
    return '执行失败，请查看本机运行状态后重试'


class APIError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class _TrackedRunner:
    def __init__(self, runner, workspace, conversation_id, seal_result=None, job_id=None):
        self.runner = runner
        self.name = runner.name
        self.workspace = workspace
        self.conversation_id = conversation_id
        self.run_id = None
        self.started = None
        self.seal_result = seal_result
        self.job_id = job_id

    def analyze(self, context):
        # Check the actual model snapshot again, not just the HTTP preflight.
        _limit(context)
        self.run_id = start_run(self.workspace, self.conversation_id, context, runner=self.name, run_id=self.job_id)
        self.started = time.monotonic()
        result = self.runner.analyze(context)
        if self.seal_result:
            self.seal_result()
        return result

    def finish(self, *, draft_id=None, error=None):
        if self.run_id:
            finish_run(self.workspace, self.run_id, int((time.monotonic() - self.started) * 1000),
                       draft_id=draft_id, error_category=getattr(error, 'category', 'validation_or_runtime') if error else None)


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, workspace, host, port, runner_factory, model_catalog_provider):
        self.workspace = Workspace.load(workspace.root if isinstance(workspace, Workspace) else workspace)
        self.workspace_instance = hashlib.sha256(json.dumps(
            [str(self.workspace.root.resolve()), self.workspace.manifest['created_at']],
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.instance_id = uuid.uuid4().hex
        self.runner_factory = runner_factory
        self.model_catalog_provider = model_catalog_provider
        self.session_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.operator = 'local:' + getpass.getuser()
        self.jobs = {}
        self.job_lock = threading.Lock()
        self.job_changed = threading.Condition(self.job_lock)
        self.job_controls = {}
        self.active_mutations = 0
        self.closing = threading.Event()
        self.app_server_client = None
        self.active_job_id = None
        self.customer_model_allowed = self.workspace.mode == 'simulation'
        self._instance_lock = (self.workspace.root / 'logs' / 'workbench.lock').open('a')
        try:
            fcntl.flock(self._instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._instance_lock.close()
            raise ValueError('此工作区已有工作台运行，请使用原页面或先停止原进程') from exc
        try:
            super().__init__((host, port), WorkbenchHandler)
            self.job_store = JobStore(self.workspace)
            self.job_store.recover_interrupted()
            recover_workbench_runs(self.workspace)
            self.jobs = {job['id']: job for job in reversed(self.job_store.recent())}
        except BaseException:
            self._instance_lock.close()
            if hasattr(self, 'socket'):
                self.socket.close()
            raise
        # Cookies have no port scope; distinct names keep customer instances
        # on different loopback ports from replacing each other's session.
        self.cookie_name = _COOKIE + '_' + str(self.server_address[1])

    def current_workspace(self):
        current = Workspace.load(self.workspace.root)
        if (current.company_id, current.mode) != (self.workspace.company_id, self.workspace.mode):
            raise APIError(409, '工作区绑定已改变，请关闭并重新启动工作台')
        return current

    @contextmanager
    def service(self):
        # A service owns exactly one connection, created and closed on this thread.
        service = self.current_workspace().service(None)
        try:
            yield service
        finally:
            service.store.close()

    def active_job(self):
        with self.job_lock:
            job = self.jobs.get(self.active_job_id)
            return dict(job) if job and job['status'] in _ACTIVE_STATUSES else None

    def job(self, job_id):
        with self.job_lock:
            if job_id not in self.jobs:
                job = self.job_store.get(job_id)
                if job is None:
                    raise APIError(404, '当前工作区中没有这个分析任务')
                return job
            return dict(self.jobs[job_id])

    def _update_job_locked(self, job_id, *, persist=True, **changes):
        job = self.jobs[job_id]
        job.update(changes)
        job['revision'] = job.get('revision', 0) + 1
        if persist:
            self.job_store.save(job)
        self.job_changed.notify_all()

    def _generation_event(self, job_id, event):
        with self.job_changed:
            job = self.jobs[job_id]
            if job['status'] not in _ACTIVE_STATUSES or job['status'] == 'cancelling':
                return
            if event.get('type') == 'reply_delta' and isinstance(event.get('text'), str):
                preview = job.get('preview_reply', '') + event['text']
                self._update_job_locked(job_id, persist=False, preview_reply=preview[:50_000])
            elif event.get('type') == 'status':
                # Fixed product copy: model or server diagnostics are not exposed.
                self._update_job_locked(job_id, persist=False, progress_message='正在生成回复…')

    def _seal_result(self, job_id):
        # Serialize the final-result boundary with cancellation. Once this
        # boundary closes, a late stop returns 409 instead of falsely succeeding.
        with self.job_changed:
            control = self.job_controls[job_id]
            if control['cancel'].is_set():
                raise EngineError('生成已停止，未保存草稿', 'cancelled')
            control['can_cancel'] = False
            self._update_job_locked(job_id, can_cancel=False, progress_message='正在核对并保存回复…')

    def cancel_analysis(self, job_id):
        with self.job_changed:
            job = self.jobs.get(job_id)
            if job is None:
                prior = self.job_store.get(job_id)
                if prior is None:
                    raise APIError(404, '当前工作区中没有这个分析任务')
                return prior
            if job['status'] not in _ACTIVE_STATUSES:
                return dict(job)
            control = self.job_controls[job_id]
            if not control['can_cancel']:
                raise APIError(409, '回复正在完成校验，已无法停止，请等待结果')
            control['cancel'].set()
            self._update_job_locked(job_id, status='cancelling', can_cancel=False,
                                    progress_message='正在停止生成…')
            return dict(job)

    def server_close(self):
        self.closing.set()
        with self.job_changed:
            while self.active_mutations:
                self.job_changed.wait()
            for control in self.job_controls.values():
                control['cancel'].set()
            self.job_changed.notify_all()
        if self.app_server_client:
            self.app_server_client.close()
        # Do not release the workspace lease while a worker can still commit.
        # The explicit legacy exec fallback cannot be interrupted; it must finish
        # (or hit its runner timeout) before another instance may use this DB.
        for control in list(self.job_controls.values()):
            worker = control.get('thread')
            if worker and worker is not threading.current_thread():
                worker.join()
        super().server_close()
        if not self._instance_lock.closed:
            self._instance_lock.close()

    def start_analysis(self, account, conversation, allow_customer, request=None):
        ws = self.current_workspace()
        request = normalize_request(request)
        if request and request.get('model'):
            catalog = self.model_catalog_provider()
            if catalog.get('error') or not catalog.get('models'):
                raise APIError(503, 'AI 服务配置暂不可用，请重试或联系负责人检查设置')
            if request['model'] not in {item['id'] for item in catalog['models']}:
                raise APIError(400, '已配置的 AI 型号暂不可用，请联系负责人重新选择')
        if ws.mode == 'customer' and not self.customer_model_allowed and allow_customer is not True:
            raise APIError(403, '请先确认当前客户资料可交由企业配置的云端 AI 服务处理')
        with self.job_lock:
            prior = self.jobs.get(self.active_job_id)
            if self.closing.is_set():
                raise APIError(503, '工作台正在关闭')
            if prior and prior['status'] in _ACTIVE_STATUSES:
                if (prior['account_id'], prior['conversation_id']) == (account, conversation) and prior.get('request') == request:
                    return dict(prior)
                raise APIError(409, '已有回复正在生成，请等待完成后再发起新的请求')
            # Preflight is quick and never calls the model. Holding the job lock
            # reserves the single model slot across simultaneous POST requests.
            with self.service() as service:
                _limit(service.context(ws.company_id, account, conversation, _day(), request=request))
            if allow_customer is True:
                self.customer_model_allowed = True
            job_id = uuid.uuid4().hex
            job = {'id': job_id, 'status': 'queued', 'account_id': account,
                   'conversation_id': conversation, 'started_at': _now(), 'request': request,
                   'preview_reply': '', 'progress_message': '正在连接模型…', 'revision': 0,
                   'can_cancel': self.runner_factory is None}
            self.job_store.save(job)
            self.jobs[job_id] = job
            self.job_controls[job_id] = {'cancel': threading.Event(), 'can_cancel': self.runner_factory is None}
            self.active_job_id = job_id
            while len(self.jobs) > 100:
                self.jobs.pop(next(iter(self.jobs)))
            result = dict(job)
            worker = threading.Thread(target=self._run_analysis, args=(job_id, account, conversation, request),
                                      name='inquiry-analysis-' + job_id[:8], daemon=True)
            self.job_controls[job_id]['thread'] = worker
            worker.start()
            return result

    def _run_analysis(self, job_id, account, conversation, request=None):
        service = None
        tracked = None
        try:
            with self.job_changed:
                cancel = self.job_controls[job_id]['cancel']
                if cancel.is_set():
                    raise EngineError('生成已停止，未保存草稿', 'cancelled')
                self._update_job_locked(job_id, status='running')
            ws = self.current_workspace()
            if self.runner_factory is None:
                from .app_server import AppServerClient, AppServerRunner
                with self.job_lock:
                    if self.closing.is_set() or cancel.is_set():
                        raise EngineError('生成已停止，未保存草稿', 'cancelled')
                    if self.app_server_client is None:
                        self.app_server_client = AppServerClient()
                runner = AppServerRunner(self.app_server_client,
                                        on_event=lambda event: self._generation_event(job_id, event),
                                        cancel_event=cancel)
            else:
                runner = self.runner_factory()
            tracked = _TrackedRunner(runner, ws, conversation, lambda: self._seal_result(job_id), job_id)
            service = ws.service(tracked)
            draft = service.analyze(ws.company_id, account, conversation, _day(), request=request)
            warning = None
            try:
                tracked.finish(draft_id=draft['id'])
            except Exception:
                warning = '回复已保存，但运行记录写入失败，请检查工作区健康'
            with self.job_changed:
                try:
                    self._update_job_locked(job_id, status='succeeded', draft_id=draft['id'],
                                            finished_at=_now(), preview_reply='', can_cancel=False,
                                            warning=warning,
                                            progress_message=warning or '回复已完成，请核对后复制')
                except Exception:
                    # Saving auxiliary metadata must not turn an already saved
                    # reply into a reported generation failure and invite retries.
                    self._update_job_locked(job_id, persist=False, status='succeeded', draft_id=draft['id'],
                                            finished_at=_now(), preview_reply='', can_cancel=False,
                                            warning='回复已保存，但任务记录写入失败，请检查工作区健康',
                                            progress_message='回复已保存，但任务记录写入失败，请检查工作区健康')
        except Exception as exc:
            telemetry_error = False
            if tracked:
                try:
                    tracked.finish(error=exc)
                except Exception:
                    telemetry_error = True
            message = _explain(exc)
            if telemetry_error:
                message += '；运行记录写入失败，请检查工作区健康'
            cancelled = getattr(exc, 'category', None) == 'cancelled'
            with self.job_changed:
                changes = dict(status='cancelled' if cancelled else 'failed', error=message,
                               finished_at=_now(), preview_reply='', can_cancel=False,
                               progress_message='已停止生成' if cancelled else '本次生成未完成')
                try:
                    self._update_job_locked(job_id, **changes)
                except Exception:
                    changes['error'] += '；任务记录写入失败，请检查工作区健康'
                    self._update_job_locked(job_id, persist=False, **changes)
        finally:
            if service:
                service.store.close()
            with self.job_lock:
                self.job_controls.pop(job_id, None)

    def _labels(self):
        from .web_demo import conversation_labels
        from .manual_sync import conversation_labels as sync_labels
        ws = self.current_workspace()
        return conversation_labels(ws) | sync_labels(ws)

    def sync_status(self):
        from .manual_sync import sync_status
        return sync_status(self.current_workspace())

    def _drafts(self, service, account, conversation, knowledge_digest):
        store = service.store
        fingerprint = store.fingerprint(self.workspace.company_id, account, conversation)
        ids = store.connection.execute('SELECT id FROM drafts WHERE project_id=? AND account_id=? AND conversation_id=? ORDER BY created_at DESC,id DESC',
                                       (self.workspace.company_id, account, conversation))
        result = []
        for row in ids:
            draft = _public_draft(store.get_draft(row['id']))
            draft['requires_recheck'] = (draft['status'] == 'stale' or
                                        draft['fingerprint'] != fingerprint or
                                        draft['knowledge_digest'] != knowledge_digest)
            result.append(draft)
        return result

    def _conversation(self, service, account, conversation, context, labels):
        messages = service.store.messages(self.workspace.company_id, account, conversation)
        if not messages:
            label = labels.get((account, conversation))
            if not label:
                raise APIError(404, '当前工作区中没有这个会话')
            return {'account_id': account, 'conversation_id': conversation,
                    'title': label['title'], 'subtitle': label.get('subtitle', account),
                    'preview': '尚未读取到聊天记录', 'last_at': '', 'message_count': 0,
                    'status': 'needs_sync', 'draft_id': None}, [], []
        drafts = self._drafts(service, account, conversation, context['knowledge_digest'])
        last = messages[-1]
        label = labels.get((account, conversation), {})
        latest = drafts[0] if drafts else None
        status = latest['status'] if latest else 'needs_analysis'
        if latest and latest['requires_recheck'] and status != 'rejected':
            status = 'stale'
        item = {'account_id': account, 'conversation_id': conversation,
                'title': label.get('title', conversation), 'subtitle': label.get('subtitle', account),
                'preview': last['body'][:160], 'last_at': last['sent_at'],
                'message_count': len(messages), 'status': status,
                'draft_id': latest['id'] if latest else None}
        return item, messages, drafts

    def conversations(self):
        labels = self._labels()
        with self.service() as service:
            context = service.project(self.workspace.company_id).context(_day())
            result = [self._conversation(service, row['account_id'], row['conversation_id'], context, labels)[0]
                      for row in service.store.conversations(self.workspace.company_id)]
            existing = {(row['account_id'], row['conversation_id']) for row in result}
            for account, conversation in labels:
                if (account, conversation) not in existing:
                    result.append(self._conversation(service, account, conversation, context, labels)[0])
        result.sort(key=lambda row: datetime.fromisoformat(row['last_at'].replace('Z', '+00:00'))
                    if row['last_at'] else datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return result

    def outbox(self, service):
        current = service.project(self.workspace.company_id).context(_day())['knowledge_digest']
        result = service.store.outbox(self.workspace.company_id)
        for entry in result:
            basis = service.store.get_draft(entry['draft_id'])
            entry['requires_recheck'] = (current != basis['knowledge_digest'] or
                                        service.store.fingerprint(self.workspace.company_id, basis['account_id'], basis['conversation_id']) != basis['fingerprint'])
            entry.update(delivery_status='external_delivery_unknown', tool_delivery_performed=False)
        return result


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = 'InquiryWorkbench'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        # Do not put customer identifiers or message/query data in access logs.
        pass

    def _headers(self, status, content_type='application/json; charset=utf-8', length=0, cookie=False):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if cookie:
            self.send_header('Set-Cookie', f'{self.server.cookie_name}={self.server.session_token}; Path=/; HttpOnly; SameSite=Strict')
        self.end_headers()

    def _json(self, status, value):
        content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self._headers(status, length=len(content))
        self.wfile.write(content)

    def _security(self, api=False, mutation=False):
        hosts = self.headers.get_all('Host', [])
        port = self.server.server_address[1]
        if len(hosts) != 1 or hosts[0] not in (f'127.0.0.1:{port}', f'localhost:{port}'):
            raise APIError(403, '只接受本机工作台地址，Host 校验失败')
        origins = self.headers.get_all('Origin', [])
        expected = 'http://' + hosts[0]
        if len(origins) > 1 or (origins and origins[0] != expected):
            raise APIError(403, '不允许跨站访问工作台')
        if self.headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site'):
            raise APIError(403, '请从同一工作台页面发起请求')
        if api:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                value = cookie[self.server.cookie_name].value if self.server.cookie_name in cookie else ''
            except Exception:
                value = ''
            if not secrets.compare_digest(value.encode('utf-8'), self.server.session_token.encode('ascii')):
                raise APIError(401, '工作台会话无效，请刷新页面')
        if mutation:
            if origins != [expected]:
                raise APIError(403, '变更请求必须来自当前工作台页面')
            tokens = self.headers.get_all('X-CSRF-Token', [])
            if len(tokens) != 1 or not secrets.compare_digest(tokens[0].encode('utf-8'), self.server.csrf_token.encode('ascii')):
                raise APIError(403, '操作令牌无效，请刷新页面后重试')

    def _url(self, allowed_query=()):
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise APIError(400, '不支持的请求地址')
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=10)
        except ValueError as exc:
            raise APIError(400, '请求参数过多') from exc
        if set(query) - set(allowed_query) or any(len(value) != 1 for value in query.values()):
            raise APIError(400, '请求参数不受支持；不能选择其他企业或文件路径')
        return parsed.path, {key: value[0] for key, value in query.items()}

    def _body(self):
        if self.headers.get_all('Transfer-Encoding'):
            raise APIError(400, '不支持分块请求体')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise APIError(411, '需要有效的 Content-Length')
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            raise APIError(413, '单批请求上限为 5 MB，请拆分后导入')
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise APIError(415, '请求必须使用 application/json')
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete')
            return json.loads(raw.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON number')))
        except (ValueError, UnicodeError, TimeoutError) as exc:
            raise APIError(400, '请求体不是完整有效的 UTF-8 JSON') from exc

    def _dispatch(self, mutation=False):
        try:
            path = urlsplit(self.path).path
            self._security(api=path.startswith('/api/'), mutation=mutation)
            if mutation:
                if path == '/api/shutdown':
                    self._post()
                else:
                    with self.server.job_changed:
                        if self.server.closing.is_set():
                            raise APIError(503, '工作台正在关闭，请重新打开后再操作')
                        self.server.active_mutations += 1
                    try:
                        self._post()
                    finally:
                        with self.server.job_changed:
                            self.server.active_mutations -= 1
                            self.server.job_changed.notify_all()
            else:
                self._get()
        except APIError as exc:
            self._json(exc.status, {'error': str(exc)})
        except StoreError as exc:
            status = 404 if 'does not exist' in str(exc) else 409
            self._json(status, {'error': _explain(exc)})
        except (ValueError, EngineError) as exc:
            self._json(400, {'error': _explain(exc)})
        except (sqlite3.Error, OSError) as exc:
            self._json(503, {'error': _explain(exc)})
        except Exception as exc:
            self._json(500, {'error': _explain(exc)})

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch(mutation=True)

    def do_OPTIONS(self):
        self._json(405, {'error': '不支持跨站预检，请使用本机工作台页面'})

    def _job_events(self, job_id):
        # Full snapshots make reconnects idempotent; no user text enters SSE
        # framing. The same cookie/origin checks as every API run before this.
        snapshot = self.server.job(job_id)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        revision = None
        try:
            while not self.server.closing.is_set():
                current = snapshot.get('revision', 0)
                if revision != current:
                    payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
                    self.wfile.write(f'id: {current}\nevent: job\ndata: {payload}\n\n'.encode('utf-8'))
                    self.wfile.flush()
                    revision = current
                if snapshot['status'] not in _ACTIVE_STATUSES:
                    return
                with self.server.job_changed:
                    self.server.job_changed.wait_for(
                        lambda: self.server.closing.is_set() or
                        self.server.jobs.get(job_id, {}).get('revision', 0) != revision,
                        timeout=10)
                    snapshot = dict(self.server.jobs.get(job_id, snapshot))
                if snapshot.get('revision', 0) == revision:
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
        except (OSError, ValueError):
            # A closed browser must never cancel or duplicate model execution.
            return

    def _get(self):
        path = urlsplit(self.path).path
        allowed = ('account', 'conversation') if path == '/api/conversation' else ('id',) if path in ('/api/jobs', '/api/jobs/events') else ()
        path, query = self._url(allowed)
        if path in _STATIC:
            filename, content_type = _STATIC[path]
            static_root = Path(__file__).parent / 'web'
            target = static_root / filename
            if target.is_symlink() or not target.is_file():
                raise APIError(503, '工作台页面文件不完整，请使用完整交付包')
            content = target.read_bytes()
            self._headers(200, content_type, len(content), cookie=filename == 'index.html')
            self.wfile.write(content)
            return
        if path == '/api/bootstrap':
            ws = self.server.current_workspace()
            config = ProjectConfig.load(ws.configs_dir, ws.company_id).data
            items = self.server.conversations()
            self._json(200, {'company': {'id': ws.company_id, 'name': config['name'], 'mode': ws.mode},
                             'workspace_instance': self.server.workspace_instance, 'instance_id': self.server.instance_id,
                             'as_of': _day(), 'operator': self.server.operator, 'csrf_token': self.server.csrf_token,
                             'capabilities': {'model': 'codex_app_server' if self.server.runner_factory is None else 'codex_local',
                                              'customer_model_allowed': self.server.customer_model_allowed, 'sending': False},
                             'stats': {'conversations': len(items), 'messages': sum(row['message_count'] for row in items),
                                       'pending': sum(row['status'] == 'pending' for row in items),
                                       'approved': sum(row['status'] == 'approved' for row in items)},
                             'active_job': self.server.active_job(), 'recent_jobs': self.server.job_store.recent(10),
                             'sync': self.server.sync_status()})
            return
        if path == '/api/conversations':
            self._json(200, {'items': self.server.conversations()})
            return
        if path == '/api/models':
            self._json(200, self.server.model_catalog_provider() | {'default_model': DEFAULT_WORKBENCH_MODEL})
            return
        if path == '/api/conversation':
            _shape(query, ('account', 'conversation'))
            account, conversation = _text(query['account'], '账号'), _text(query['conversation'], '会话')
            labels = self.server._labels()
            with self.server.service() as service:
                context = service.project(self.server.workspace.company_id).context(_day())
                item, messages, drafts = self.server._conversation(service, account, conversation, context, labels)
            self._json(200, {'conversation': item, 'messages': messages, 'drafts': drafts,
                             'knowledge': context['knowledge'], 'required_fields': context['required_fields']})
            return
        if path == '/api/jobs':
            _shape(query, ('id',))
            self._json(200, {'job': self.server.job(_text(query['id'], '任务编号'))})
            return
        if path == '/api/jobs/events':
            _shape(query, ('id',))
            self._job_events(_text(query['id'], '任务编号'))
            return
        if path == '/api/knowledge':
            ws = self.server.current_workspace()
            self._json(200, {'config': ProjectConfig.load(ws.configs_dir, ws.company_id).data,
                             'active_release': ws.manifest['active_release'], 'history': ws.manifest['knowledge_history']})
            return
        if path == '/api/strategy':
            ws = self.server.current_workspace()
            config = ProjectConfig.load(ws.configs_dir, ws.company_id).data
            self._json(200, {'text': config.get('reply_strategy', ''), 'active_release': ws.manifest['active_release']})
            return
        if path == '/api/reply-settings':
            ws = self.server.current_workspace()
            config = ProjectConfig.load(ws.configs_dir, ws.company_id).data
            self._json(200, read_settings(config, ws.manifest['active_release']))
            return
        if path == '/api/sync':
            self._json(200, self.server.sync_status())
            return
        if path == '/api/customers':
            from .customer_management import customer_status
            from .manual_sync import SyncBusyError
            try:
                result = customer_status(self.server.current_workspace())
            except SyncBusyError as exc:
                raise APIError(409, str(exc)) from exc
            self._json(200, result)
            return
        if path == '/api/activity':
            health = self.server.workspace.health()
            if not health['ok']:
                self._json(200, {'health': health, 'runs': [], 'imports': [], 'outbox': [], 'active_job': self.server.active_job()})
                return
            with self.server.service() as service:
                imports = [dict(row) for row in service.store.connection.execute('SELECT * FROM import_batches ORDER BY imported_at DESC LIMIT 100')]
                outbox = self.server.outbox(service)
            self._json(200, {'health': health, 'runs': list_runs(self.server.current_workspace()), 'imports': imports,
                             'outbox': outbox, 'active_job': self.server.active_job(),
                             'recent_jobs': self.server.job_store.recent(10), 'sync': self.server.sync_status()})
            return
        raise APIError(404, '页面或接口不存在')

    def _post(self):
        path, _ = self._url()
        if path not in ('/api/messages', '/api/analyze', '/api/jobs/cancel', '/api/review', '/api/adopt', '/api/knowledge', '/api/strategy', '/api/reply-settings', '/api/sync', '/api/import', '/api/customers/scan', '/api/customers/selection', '/api/shutdown'):
            raise APIError(404, '接口不存在')
        data = self._body()
        if path == '/api/shutdown':
            _shape(data, ('instance_id',))
            with self.server.job_lock:
                if data['instance_id'] != self.server.instance_id:
                    raise APIError(409, '工作台实例已改变，请重新打开后再停止')
                if self.server.active_mutations:
                    raise APIError(409, '正在更新或保存资料，请操作完成后再关闭工作台')
                active = self.server.jobs.get(self.server.active_job_id)
                if active and active['status'] in _ACTIVE_STATUSES:
                    raise APIError(409, '仍有回复正在生成，请完成或停止生成后再关闭工作台')
                self.server.closing.set()
            self._json(200, {'status': 'stopping', 'instance_id': self.server.instance_id})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path in ('/api/customers/scan', '/api/customers/selection'):
            from .customer_management import scan_customers, save_customers, CustomerConflictError
            from .manual_sync import SyncBusyError
            try:
                if path == '/api/customers/scan':
                    _shape(data, ())
                    result = scan_customers(self.server.current_workspace())
                else:
                    _shape(data, ('scan_id', 'selected_ids'))
                    result = save_customers(self.server.current_workspace(),
                                            _text(data['scan_id'], '扫描编号', 128), data['selected_ids'])
            except (CustomerConflictError, SyncBusyError) as exc:
                raise APIError(409, str(exc)) from exc
            self._json(200, result)
            return
        if path == '/api/jobs/cancel':
            _shape(data, ('id',))
            self._json(200, {'job': self.server.cancel_analysis(_text(data['id'], '任务编号'))})
            return
        if path == '/api/sync':
            _shape(data, ())
            from .manual_sync import refresh_messages
            self._json(200, refresh_messages(self.server.current_workspace()))
            return
        if path == '/api/messages':
            _shape(data, ('account_id', 'conversation_id', 'body'))
            ws = self.server.current_workspace()
            if ws.mode != 'simulation':
                raise APIError(403, '只有演练工作区允许手动补充模拟消息')
            now = _now()
            message = {'project_id': ws.company_id, 'account_id': _text(data['account_id'], '账号'),
                       'conversation_id': _text(data['conversation_id'], '会话'), 'message_id': 'web-' + uuid.uuid4().hex,
                       'direction': 'inbound', 'body': _text(data['body'], '消息正文', 20_000),
                       'sent_at': now, 'received_at': now, 'mode': 'simulation'}
            result = ws.import_messages([message], 'workbench-simulation')
            self._json(200, {'message': message, 'import': result})
            return
        if path == '/api/analyze':
            _shape(data, ('account_id', 'conversation_id'), ('allow_customer_model', 'request'))
            if 'allow_customer_model' in data and not isinstance(data['allow_customer_model'], bool):
                raise APIError(400, 'allow_customer_model 必须为布尔值')
            job = self.server.start_analysis(_text(data['account_id'], '账号'), _text(data['conversation_id'], '会话'), data.get('allow_customer_model', False), data.get('request'))
            self._json(202, {'job': job})
            return
        if path == '/api/adopt':
            _shape(data, ('draft_id', 'final_text'))
            draft_id = _text(data['draft_id'], '草稿编号')
            final = _text(data['final_text'], '最终回复', 50_000)
            with self.server.service() as service:
                draft = service.adopt(draft_id, self.server.operator, final, _day())
            public = _public_draft(draft)
            public['requires_recheck'] = False
            self._json(200, {'draft': public, 'adoption': 'saved', 'delivery_performed': False})
            return
        if path == '/api/review':
            _shape(data, ('draft_id', 'decision'), ('final_text',))
            draft_id = _text(data['draft_id'], '草稿编号')
            if data['decision'] not in ('approve', 'reject'):
                raise APIError(400, '审核决定必须是 approve 或 reject')
            final = data.get('final_text')
            if data['decision'] == 'approve':
                final = _text(final, '最终回复', 50_000)
            elif final is not None and not isinstance(final, str):
                raise APIError(400, '最终回复必须是文本或 null')
            with self.server.service() as service:
                draft = service.store.get_draft(draft_id)
                if draft['project_id'] != self.server.workspace.company_id:
                    raise APIError(404, '当前工作区中没有这个草稿')
                result = _public_draft(service.review(draft_id, data['decision'], self.server.operator, final, _day()))
                current = service.project(self.server.workspace.company_id).context(_day())['knowledge_digest']
                result['requires_recheck'] = (result['knowledge_digest'] != current or
                    result['fingerprint'] != service.store.fingerprint(result['project_id'], result['account_id'], result['conversation_id']))
            self._json(200, {'draft': result})
            return
        if path == '/api/knowledge':
            _shape(data, ('config', 'base_release'))
            if not isinstance(data['config'], dict):
                raise APIError(400, '企业知识配置必须是完整 JSON 对象')
            base = _text(data['base_release'], '资料版本', 64)
            self._json(200, self.server.current_workspace().publish(data['config'], expected_release=base))
            return
        if path == '/api/strategy':
            _shape(data, ('text', 'base_release'))
            strategy = data['text']
            if not isinstance(strategy, str) or len(strategy) > 12_000:
                raise APIError(400, '回复策略必须是最多 12000 字的文本')
            if strategy:
                _text(strategy, '回复策略', 12_000)
            base = _text(data['base_release'], '资料版本', 64)
            ws = self.server.current_workspace()
            config = ProjectConfig.load(ws.configs_dir, ws.company_id).data
            if strategy.strip():
                config['reply_strategy'] = strategy.strip()
            else:
                config.pop('reply_strategy', None)
            self._json(200, ws.publish(config, expected_release=base))
            return
        if path == '/api/reply-settings':
            _shape(data, ('text', 'required_fields', 'rules', 'base_release'))
            base = _text(data['base_release'], '资料版本', 64)
            ws = self.server.current_workspace()
            config = ProjectConfig.load(ws.configs_dir, ws.company_id).data
            config = updated_config(config, data['text'], data['required_fields'], data['rules'])
            self._json(200, ws.publish(config, expected_release=base))
            return
        if path == '/api/import':
            _shape(data, ('records', 'source'))
            if not isinstance(data['records'], list) or not data['records'] or len(data['records']) > MAX_RECORDS:
                raise APIError(400, '导入记录必须为 1–2000 条标准消息数组')
            source = _text(data['source'], '来源标签', 120)
            self._json(200, self.server.current_workspace().import_messages(data['records'], source))


def create_server(workspace, host='127.0.0.1', port=0, runner_factory=None,
                  model_catalog_provider=get_model_catalog):
    """Create a server bound to exactly one workspace and an IPv4 loopback port."""
    if host != '127.0.0.1':
        raise ValueError('工作台仅允许监听 127.0.0.1')
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError('端口必须为 0–65535 的整数')
    return WorkbenchServer(workspace, host, port, runner_factory, model_catalog_provider)


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地询盘工作台 · 独立工作区 · 人工审核')
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--runner', choices=('app-server', 'exec'), default='app-server', help='模型调用方式；exec 为人工选择的兼容回退')
    args = parser.parse_args(argv)
    try:
        server = create_server(args.workspace, port=args.port,
                               runner_factory=CodexRunner if args.runner == 'exec' else None)
    except (ValueError, OSError) as exc:
        parser.exit(1, '工作台启动失败：' + _explain(exc) + '\n')
    print(f'询盘工作台：http://127.0.0.1:{server.server_address[1]} （Ctrl+C 停止）', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
