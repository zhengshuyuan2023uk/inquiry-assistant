"""Local HTTP integration checks use stub runners, never a real model account."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.cookiejar
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

from inquiry_product.core.engine import EngineError
from inquiry_product.web_server import create_server, MAX_BODY_BYTES, _explain
from inquiry_product.workspace import Workspace


class StubRunner:
    name = 'workbench_test_stub'

    def analyze(self, context):
        result = {'summary': '离线接口测试', 'facts': [], 'missing_fields': context['required_fields'],
                'uncertainties': ['非真实模型'], 'next_action': '补充数量', 'reply': '请补充采购数量。',
                'citations': [], 'needs_human': True}
        if context.get('request'):
            request = context['request']
            result.update(reply='Could you confirm the quantity?', rationale=['当前消息未说明数量。'],
                          reply_language=request['language'] if request['language'] != 'auto' else 'en', warnings=[])
            if request['mode'] == 'polish':
                result.update(missing_fields=[], reply='Thank you. We will verify the details.')
        return result


class WebServerTests(unittest.TestCase):
    def test_engine_errors_explain_business_action_without_backend_diagnostics(self):
        actions = {'auth': '负责人', 'config': '负责人', 'network': '重试',
                   'timeout': '重试', 'rate_limit': '稍后', 'cancelled': '停止',
                   'schema': '校验', 'tool_access': '校验'}
        for category, action in actions.items():
            with self.subTest(category=category):
                result = _explain(EngineError('Codex auth.json --stdio PRIVATE_DIAGNOSTIC', category))
                self.assertIn(action, result)
                self.assertNotIn('Codex', result)
                self.assertNotIn('PRIVATE_DIAGNOSTIC', result)
                self.assertNotIn('auth.json', result)
        self.assertNotIn('Codex', _explain(OSError('private path')))

    def test_instance_identity_survives_restart_but_stop_identity_does_not(self):
        first = self.request('/api/bootstrap')[1]
        self.assertRegex(first.get('workspace_instance', ''), r'^[0-9a-f]{64}$')
        self.assertTrue(first.get('instance_id'))
        self.stop_server()
        self.start_server()
        second = self.request('/api/bootstrap')[1]
        self.assertEqual(first['workspace_instance'], second['workspace_instance'])
        self.assertNotEqual(first['instance_id'], second['instance_id'])
        self.assertEqual(self.request('/api/shutdown', {'instance_id': first['instance_id']})[0], 409)
        self.assertTrue(self.thread.is_alive())

    def test_shutdown_is_authenticated_and_stops_only_the_current_idle_instance(self):
        identity = self.request('/api/bootstrap')[1].get('instance_id', 'unavailable')
        self.assertEqual(self.request('/api/shutdown', {'instance_id': identity}, cookie=False)[0], 401)
        self.assertEqual(self.request('/api/shutdown', {'instance_id': identity}, headers={'X-CSRF-Token': None})[0], 403)
        self.assertEqual(self.request('/api/shutdown', {'instance_id': identity})[0], 200)
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def test_shutdown_refuses_while_chat_update_is_being_saved(self):
        entered, release = threading.Event(), threading.Event()
        self.blockers.append(release)
        def updating(workspace):
            entered.set()
            release.wait(3)
            return {'status': 'succeeded', 'inserted': 0}
        identity = self.request('/api/bootstrap')[1]['instance_id']
        with patch('inquiry_product.manual_sync.refresh_messages', side_effect=updating):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.request, '/api/sync', {})
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(self.request('/api/shutdown', {'instance_id': identity})[0], 409)
                    self.assertFalse(self.server.closing.is_set())
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=2)[0], 200)

    def test_closing_rejects_new_mutations_without_changing_data(self):
        self.server.closing.set()
        with patch('inquiry_product.manual_sync.refresh_messages', side_effect=AssertionError('must not start an update')):
            self.assertEqual(self.request('/api/sync', {})[0], 503)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = {'id': 'alpha', 'name': '测试企业', 'mode': 'simulation', 'industry': 'test',
                       'required_fields': ['数量'], 'rules': ['未知信息不得承诺'],
                       'knowledge': [{'id': 'K1', 'version': '1', 'title': '报价规则', 'content': '补齐数量后再报价。',
                                      'valid_from': '2020-01-01', 'valid_until': '2099-12-31'}]}
        self.ws = Workspace.init(self.root / 'workspace', 'alpha', self.config)
        self.ws.import_messages([self.record()], 'test-seed')
        # Display labels are covered by the separate demonstration fixture tests.
        self.labels_patch = patch('inquiry_product.web_server.WorkbenchServer._labels', return_value={})
        self.labels_patch.start()
        self.blockers = []
        self.start_server()

    def start_server(self, factory=StubRunner):
        self.catalog = {'source': 'codex_cli', 'models': [
            {'id': 'gpt-test-a', 'name': 'Test A', 'default': True},
            {'id': 'gpt-test-b', 'name': 'Test B', 'default': False}]}
        self.server = create_server(self.ws, port=0, runner_factory=factory,
                                    model_catalog_provider=lambda: self.catalog)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = 'http://127.0.0.1:' + str(self.port)
        self.cookie = self.server.cookie_name + '=' + self.server.session_token

    def stop_server(self):
        for event in self.blockers:
            event.set()
        deadline = time.monotonic() + 3
        while self.server.active_job() and time.monotonic() < deadline:
            time.sleep(.01)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def tearDown(self):
        self.stop_server()
        self.labels_patch.stop()
        self.temporary.cleanup()

    def record(self, message_id='m1', conversation='c', **changes):
        now = datetime.now(timezone.utc).isoformat()
        record = {'project_id': 'alpha', 'account_id': 'a', 'conversation_id': conversation,
                  'message_id': message_id, 'body': '想采购一批货。', 'direction': 'inbound',
                  'sent_at': now, 'received_at': now, 'mode': self.ws.mode if hasattr(self, 'ws') else 'simulation'}
        return record | changes

    def request(self, path, data=None, headers=None, cookie=True, method=None, raw=None):
        method = method or ('POST' if data is not None else 'GET')
        supplied = {'Cookie': self.cookie} if cookie else {}
        if method == 'POST':
            supplied.update({'Content-Type': 'application/json', 'Origin': self.origin,
                             'X-CSRF-Token': self.server.csrf_token})
        if headers:
            for name, value in headers.items():
                if value is None:
                    supplied.pop(name, None)
                else:
                    supplied[name] = value
        body = raw if raw is not None else json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request(method, path, body, supplied)
            response = connection.getresponse()
            content = response.read()
            try:
                value = json.loads(content)
            except ValueError:
                value = content.decode('utf-8')
            return response.status, value, dict(response.getheaders())
        finally:
            connection.close()

    def analyze(self, **changes):
        status, value, _ = self.request('/api/analyze', {'account_id': 'a', 'conversation_id': 'c'} | changes)
        self.assertEqual(status, 202, value)
        return value['job']['id']

    def wait_job(self, job_id, expected='succeeded'):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            status, value, _ = self.request('/api/jobs?id=' + job_id)
            self.assertEqual(status, 200)
            if value['job']['status'] not in ('queued', 'running'):
                self.assertEqual(value['job']['status'], expected, value)
                return value['job']
            time.sleep(.01)
        self.fail('stub analysis job did not complete')

    def blocking_runner(self, fail=False):
        entered, release = threading.Event(), threading.Event()
        self.blockers.append(release)
        calls = []
        class Runner(StubRunner):
            def analyze(inner, context):
                calls.append(context)
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test barrier timeout')
                if fail:
                    raise EngineError('测试执行失败', category='network')
                return super().analyze(context)
        self.server.runner_factory = Runner
        return entered, release, calls

    def test_page_issues_http_only_cookie_and_static_files_cannot_traverse(self):
        status, body, headers = self.request('/', cookie=False)
        self.assertEqual(status, 200, body)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        for path in ('/../workspace.json', '/%2e%2e/workspace.json', '/workspace.json', '/web_server.py'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_bootstrap_and_conversation_read_contract(self):
        status, boot, _ = self.request('/api/bootstrap')
        self.assertEqual(status, 200, boot)
        self.assertEqual(boot['company'], {'id': 'alpha', 'name': '测试企业', 'mode': 'simulation'})
        self.assertFalse(boot['capabilities']['sending'])
        self.assertEqual(boot['stats']['messages'], 1)
        self.assertTrue(boot['operator'].startswith('local:'))
        status, values, _ = self.request('/api/conversations')
        self.assertEqual(values['items'][0]['title'], 'c')
        self.assertEqual(values['items'][0]['status'], 'needs_analysis')
        status, detail, _ = self.request('/api/conversation?account=a&conversation=c')
        self.assertEqual(status, 200)
        self.assertEqual(detail['required_fields'], ['数量'])
        self.assertEqual(len(detail['messages']), 1)
        self.assertEqual(detail['knowledge'][0]['id'], 'K1')
        self.assertEqual(self.request('/api/conversation?account=a&conversation=missing')[0], 404)

    def test_model_catalog_requires_session_and_has_no_side_effects(self):
        self.assertEqual(self.request('/api/models', cookie=False)[0], 401)
        status, catalog, _ = self.request('/api/models')
        self.assertEqual(status, 200)
        self.assertEqual(catalog, self.catalog | {'default_model': 'gpt-5.6-luna'})
        self.assertIsNone(self.server.active_job())
        self.assertEqual(self.request('/api/activity')[1]['runs'], [])

    def test_selected_model_reaches_context_draft_and_execution_record(self):
        entered, release, calls = self.blocking_runner()
        job_id = self.analyze(request={'mode': 'generate', 'model': 'gpt-test-b'})
        self.assertTrue(entered.wait(2))
        self.assertEqual(calls[0]['request']['model'], 'gpt-test-b')
        running = self.request('/api/activity')[1]['runs'][0]
        self.assertEqual(running['requested_model'], 'gpt-test-b')
        release.set()
        job = self.wait_job(job_id)
        draft = self.request('/api/conversation?account=a&conversation=c')[1]['drafts'][0]
        self.assertEqual(draft['id'], job['draft_id'])
        self.assertEqual(draft['request']['model'], 'gpt-test-b')
        self.assertEqual(self.request('/api/activity')[1]['runs'][0]['requested_model'], 'gpt-test-b')

    def test_unknown_or_unavailable_model_does_not_run_or_fall_back(self):
        payload = {'account_id': 'a', 'conversation_id': 'c',
                   'request': {'mode': 'generate', 'model': 'unavailable-model'}}
        self.assertEqual(self.request('/api/analyze', payload)[0], 400)
        self.catalog = {'source': 'codex_cli', 'models': [], 'error': '目录读取失败'}
        payload['request']['model'] = 'gpt-test-a'
        self.assertEqual(self.request('/api/analyze', payload)[0], 503)
        self.assertEqual(self.request('/api/models')[1]['error'], '目录读取失败')
        self.assertIsNone(self.server.active_job())
        self.assertEqual(self.request('/api/activity')[1]['runs'], [])

    def test_running_request_for_different_model_is_not_deduplicated(self):
        entered, release, calls = self.blocking_runner()
        job_id = self.analyze(request={'mode': 'generate', 'model': 'gpt-test-a'})
        self.assertTrue(entered.wait(2))
        payload = {'account_id': 'a', 'conversation_id': 'c',
                   'request': {'mode': 'generate', 'model': 'gpt-test-b'}}
        self.assertEqual(self.request('/api/analyze', payload)[0], 409)
        self.assertEqual(len(calls), 1)
        release.set()
        self.wait_job(job_id)

    def test_legacy_model_run_upgrade_preserves_rows_without_inventing_model(self):
        # Reproduce the previous telemetry schema, independently of the new code.
        with closing(sqlite3.connect(self.ws.db_path)) as db:
            db.execute('''CREATE TABLE model_runs (
                id TEXT PRIMARY KEY, company_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                runner TEXT NOT NULL, prompt_version TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, duration_ms INTEGER, status TEXT NOT NULL, draft_id TEXT,
                error_category TEXT, input_messages INTEGER NOT NULL, input_characters INTEGER NOT NULL)''')
            db.execute('''INSERT INTO model_runs
                (id,company_id,conversation_id,runner,prompt_version,started_at,status,input_messages,input_characters)
                VALUES ('old','alpha','c','codex_local','inquiry-v2','2026-09-25T00:00:00Z','succeeded',1,100)''')
            db.commit()
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.request('/api/activity'), range(4)))
        self.assertTrue(all(response[0] == 200 for response in responses), responses)
        first = responses[0][1]['runs'][0]
        second = self.request('/api/activity')[1]['runs'][0]
        self.assertEqual(first, second)
        self.assertEqual(first['id'], 'old')
        self.assertEqual(first['input_characters'], 100)
        self.assertIsNone(first['requested_model'])

    def test_api_requires_session_and_rejects_cross_origin_host(self):
        self.assertEqual(self.request('/api/bootstrap', cookie=False)[0], 401)
        self.assertEqual(self.request('/api/bootstrap', headers={'Cookie': self.server.cookie_name + '=wrong'})[0], 401)
        for headers in ({'Host': 'evil.example:' + str(self.port)},
                        {'Host': '127.0.0.1:1'}, {'Origin': 'https://evil.example'},
                        {'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'null'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('/api/bootstrap', headers=headers)[0], 403)
        # A localhost alias on the actual bound port is also valid.
        self.assertEqual(self.request('/api/bootstrap', headers={'Host': 'localhost:' + str(self.port)})[0], 200)

    def test_mutations_require_both_origin_and_csrf(self):
        message = {'account_id': 'a', 'conversation_id': 'c', 'body': '模拟消息'}
        for headers in ({'X-CSRF-Token': None}, {'X-CSRF-Token': 'wrong'},
                        {'Origin': None}, {'Origin': 'https://evil.example'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('/api/messages', message, headers=headers)[0], 403)
        self.assertEqual(self.request('/api/messages', message, cookie=False)[0], 401)
        self.assertEqual(self.request('/api/bootstrap')[1]['stats']['messages'], 1)

    def test_unknown_scope_and_path_arguments_are_rejected(self):
        for path in ('/api/bootstrap?workspace=/tmp/other', '/api/conversations?company_id=other',
                     '/api/conversation?account=a&account=b&conversation=c'):
            self.assertEqual(self.request(path)[0], 400)
        status, error, _ = self.request('/api/analyze', {'account_id': 'a', 'conversation_id': 'c', 'company_id': 'other'})
        self.assertEqual(status, 400)
        self.assertIsNone(self.server.active_job())
        self.assertEqual(self.request('/api/messages', {'account_id': 'a', 'conversation_id': 'c', 'body': 'x', 'workspace': '/tmp'})[0], 400)

    def test_body_content_type_json_and_size_guards(self):
        payload = {'records': [], 'source': 'test'}
        self.assertEqual(self.request('/api/import', payload, headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('/api/import', payload, raw=b'{invalid')[0], 400)
        self.assertEqual(self.request('/api/import', payload, raw=b'{"records":NaN,"source":"test"}')[0], 400)
        self.assertEqual(self.request('/api/import', payload, headers={'Content-Length': str(MAX_BODY_BYTES + 1)}, raw=b'')[0], 413)
        self.assertEqual(self.request('/api/import', payload, headers={'Transfer-Encoding': 'chunked'})[0], 400)
        self.assertEqual(self.request('/api/import', {'records': [{}] * 2001, 'source': 'test'})[0], 400)

    def test_simulation_message_is_bound_and_audited(self):
        status, value, _ = self.request('/api/messages', {'account_id': 'a', 'conversation_id': 'c', 'body': '50 件\n发往伦敦'})
        self.assertEqual(status, 200, value)
        self.assertEqual(value['message']['project_id'], 'alpha')
        self.assertEqual(value['message']['direction'], 'inbound')
        self.assertEqual(value['message']['mode'], 'simulation')
        self.assertEqual(value['import']['inserted'], 1)
        activity = self.request('/api/activity')[1]
        self.assertTrue(activity['health']['ok'])
        self.assertEqual(activity['imports'][0]['source_label'], 'workbench-simulation')
        self.assertEqual(activity['outbox'], [])

    def test_json_import_is_idempotent_and_foreign_batch_atomic(self):
        record = self.record('m2')
        data = {'records': [record], 'source': 'uploaded-json'}
        self.assertEqual(self.request('/api/import', data)[1]['inserted'], 1)
        repeated = self.request('/api/import', data)[1]
        self.assertEqual(repeated['inserted'], 0)
        self.assertTrue(repeated['replayed'])
        invalid = {'records': [self.record('m3'), self.record('m4', project_id='other')], 'source': 'wrong-company'}
        status, error, _ = self.request('/api/import', invalid)
        self.assertEqual(status, 400)
        self.assertIn('企业', error['error'])
        self.assertEqual(self.request('/api/bootstrap')[1]['stats']['messages'], 2)

    def test_background_job_deduplicates_without_blocking_other_reads(self):
        entered, release, calls = self.blocking_runner()
        job_id = self.analyze()
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        self.assertEqual(self.request('/api/bootstrap')[0], 200)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.analyze(), job_id)
        self.assertEqual(self.request('/api/analyze', {'account_id': 'a', 'conversation_id': 'another'})[0], 409)
        release.set()
        job = self.wait_job(job_id)
        self.assertEqual(len(calls), 1)
        self.assertTrue(job['draft_id'])
        activity = self.request('/api/activity')[1]
        self.assertEqual(activity['runs'][0]['status'], 'succeeded')
        self.assertIsNone(activity['active_job'])
        detail = self.request('/api/conversation?account=a&conversation=c')[1]
        self.assertNotIn('context', detail['drafts'][0])
        self.assertFalse(detail['drafts'][0]['requires_recheck'])
        self.assertEqual(detail['conversation']['status'], 'pending')

    def test_model_failure_is_observable_and_slot_can_retry(self):
        entered, release, calls = self.blocking_runner(fail=True)
        job_id = self.analyze()
        self.assertTrue(entered.wait(2))
        release.set()
        failed = self.wait_job(job_id, 'failed')
        self.assertIn('连接', failed['error'])
        self.assertNotIn('测试执行失败', failed['error'])
        activity = self.request('/api/activity')[1]
        self.assertEqual(activity['runs'][0]['status'], 'failed')
        self.assertEqual(activity['runs'][0]['error_category'], 'network')
        self.assertNotIn('想采购', json.dumps(activity['runs'], ensure_ascii=False))
        self.server.runner_factory = StubRunner
        self.wait_job(self.analyze())

    def test_edited_review_is_persistent_exactly_once_and_never_sends(self):
        job = self.wait_job(self.analyze())
        status, result, _ = self.request('/api/review', {'draft_id': job['draft_id'], 'decision': 'approve', 'final_text': 'Hello,\n人工修改：请提供数量。'})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['draft']['status'], 'approved')
        self.assertEqual(result['draft']['final_text'], 'Hello,\n人工修改：请提供数量。')
        outbox = self.request('/api/activity')[1]['outbox']
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]['delivery_state'], 'simulation_only')
        self.assertFalse(outbox[0]['tool_delivery_performed'])
        self.assertEqual(outbox[0]['delivery_status'], 'external_delivery_unknown')
        self.request('/api/review', {'draft_id': job['draft_id'], 'decision': 'approve', 'final_text': 'Hello,\n人工修改：请提供数量。'})
        self.assertEqual(len(self.request('/api/activity')[1]['outbox']), 1)
        store = self.ws.open_store()
        try:
            reviewer = store.connection.execute('SELECT reviewer FROM review_events').fetchone()[0]
            self.assertTrue(reviewer.startswith('local:'))
        finally:
            store.close()

    def test_new_message_invalidates_draft_and_refuses_approval(self):
        job = self.wait_job(self.analyze())
        self.request('/api/messages', {'account_id': 'a', 'conversation_id': 'c', 'body': '数量有变化'})
        detail = self.request('/api/conversation?account=a&conversation=c')[1]
        self.assertEqual(detail['conversation']['status'], 'stale')
        self.assertTrue(detail['drafts'][0]['requires_recheck'])
        status, error, _ = self.request('/api/review', {'draft_id': job['draft_id'], 'decision': 'approve', 'final_text': '旧文案'})
        self.assertEqual(status, 409)
        self.assertIn('过期', error['error'])
        self.assertEqual(self.request('/api/activity')[1]['outbox'], [])

    def test_knowledge_publish_is_scoped_and_invalidates_old_draft(self):
        job = self.wait_job(self.analyze())
        original = self.request('/api/knowledge')[1]
        config = json.loads(json.dumps(original['config']))
        config['id'] = 'other'
        self.assertEqual(self.request('/api/knowledge', {'config': config, 'base_release': original['active_release']})[0], 400)
        self.assertEqual(self.request('/api/knowledge')[1]['active_release'], original['active_release'])
        config['id'] = 'alpha'
        config['rules'].append('新版规则')
        status, result, _ = self.request('/api/knowledge', {'config': config, 'base_release': original['active_release']})
        self.assertEqual(status, 200)
        self.assertTrue(result['changed'])
        self.assertEqual(len(self.request('/api/knowledge')[1]['history']), 2)
        self.assertEqual(self.request('/api/conversations')[1]['items'][0]['status'], 'stale')
        self.assertEqual(self.request('/api/review', {'draft_id': job['draft_id'], 'decision': 'approve', 'final_text': '旧草稿'})[0], 409)

    def test_draft_citations_preserve_snapshot_when_same_version_content_is_changed(self):
        class CitingRunner(StubRunner):
            def analyze(inner, context):
                return super().analyze(context) | {'citations': [{'id': 'K1', 'version': '1'}]}
        self.server.runner_factory = CitingRunner
        job = self.wait_job(self.analyze())
        original = self.request('/api/conversation?account=a&conversation=c')[1]['drafts'][0]
        self.assertEqual(original['citation_sources'][0]['content'], '补齐数量后再报价。')
        knowledge = self.request('/api/knowledge')[1]
        config = knowledge['config']
        config['knowledge'][0]['content'] = '新增包装要求，先核实包装。'
        config['knowledge'].append(dict(config['knowledge'][0], id='K2', title='未引用资料'))
        self.assertEqual(self.request('/api/knowledge', {'config': config, 'base_release': knowledge['active_release']})[0], 200)
        detail = self.request('/api/conversation?account=a&conversation=c')[1]
        old_draft = detail['drafts'][0]
        self.assertTrue(old_draft['requires_recheck'])
        self.assertNotIn('context', old_draft)
        self.assertEqual(detail['knowledge'][0]['content'], '新增包装要求，先核实包装。')
        self.assertEqual(len(old_draft['citation_sources']), 1)
        self.assertEqual(old_draft['citation_sources'][0]['content'], '补齐数量后再报价。')
        self.assertEqual(old_draft['citation_sources'][0]['version'], '1')
        # Legacy drafts without a snapshot must not fall back to current data.
        with closing(sqlite3.connect(self.ws.db_path)) as connection:
            connection.execute('UPDATE drafts SET context_json=NULL WHERE id=?', (job['draft_id'],))
            connection.commit()
        missing = self.request('/api/conversation?account=a&conversation=c')[1]['drafts'][0]
        self.assertEqual(missing['citation_sources'], [])

    def test_approved_output_warns_when_basis_changes(self):
        job = self.wait_job(self.analyze())
        self.request('/api/review', {'draft_id': job['draft_id'], 'decision': 'approve', 'final_text': '人工确认内容'})
        self.request('/api/messages', {'account_id': 'a', 'conversation_id': 'c', 'body': '新增条件'})
        self.assertTrue(self.request('/api/activity')[1]['outbox'][0]['requires_recheck'])
        detail = self.request('/api/conversation?account=a&conversation=c')[1]
        self.assertEqual(detail['drafts'][0]['status'], 'approved')
        self.assertEqual(detail['conversation']['status'], 'stale')

    def test_messages_changing_during_analysis_fail_without_saving_draft(self):
        entered, release, _ = self.blocking_runner()
        job_id = self.analyze()
        self.assertTrue(entered.wait(2))
        self.request('/api/messages', {'account_id': 'a', 'conversation_id': 'c', 'body': '变化的询盘'})
        release.set()
        self.wait_job(job_id, 'failed')
        self.assertEqual(self.request('/api/conversation?account=a&conversation=c')[1]['drafts'], [])
        self.assertEqual(self.request('/api/activity')[1]['runs'][0]['status'], 'failed')

    def test_model_input_limit_prevents_invocation(self):
        self.ws.import_messages([self.record('extra-' + str(index)) for index in range(500)], 'many-messages')
        calls = []
        self.server.runner_factory = lambda: calls.append(True) or StubRunner()
        status, result, _ = self.request('/api/analyze', {'account_id': 'a', 'conversation_id': 'c'})
        self.assertEqual(status, 413)
        self.assertIn('限额', result['error'])
        self.assertEqual(calls, [])
        self.assertEqual(self.request('/api/activity')[1]['runs'], [])

    def test_customer_workspace_requires_model_permission_and_disallows_simulation(self):
        self.stop_server()
        self.config['mode'] = 'customer'
        self.ws = Workspace.init(self.root / 'customer', 'alpha', self.config, mode='customer')
        self.ws.import_messages([self.record()], 'customer-import')
        self.start_server()
        self.assertFalse(self.request('/api/bootstrap')[1]['capabilities']['customer_model_allowed'])
        self.assertEqual(self.request('/api/analyze', {'account_id': 'a', 'conversation_id': 'c'})[0], 403)
        self.assertEqual(self.request('/api/analyze', {'account_id': 'a', 'conversation_id': 'c', 'allow_customer_model': 'true'})[0], 400)
        self.assertEqual(self.request('/api/messages', {'account_id': 'a', 'conversation_id': 'c', 'body': '伪造客户消息'})[0], 403)
        self.wait_job(self.analyze(allow_customer_model=True))
        self.assertTrue(self.request('/api/bootstrap')[1]['capabilities']['customer_model_allowed'])
        self.wait_job(self.analyze())

    def test_health_failure_is_visible_and_knowledge_read_explains_failure(self):
        with closing(sqlite3.connect(self.ws.db_path)) as connection:
            connection.execute('DROP TABLE import_batches')
        status, activity, _ = self.request('/api/activity')
        self.assertEqual(status, 200)
        self.assertFalse(activity['health']['ok'])
        self.assertIn('error', activity['health'])
        self.assertEqual(activity['runs'], [])
        status, error, _ = self.request('/api/knowledge')
        self.assertEqual(status, 400)
        self.assertIn('数据库', error['error'])

    def test_two_customer_instances_preserve_independent_cookie_sessions(self):
        other_config = dict(self.config, id='beta', name='另一测试企业')
        other = Workspace.init(self.root / 'other', 'beta', other_config)
        second = create_server(other, port=0, runner_factory=StubRunner)
        second_thread = threading.Thread(target=second.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        second_thread.start()
        second_origin = 'http://127.0.0.1:' + str(second.server_address[1])
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            for origin in (self.origin, second_origin):
                with opener.open(origin + '/') as response:
                    self.assertEqual(response.status, 200)
            self.assertEqual(len(list(jar)), 2)
            with opener.open(self.origin + '/api/bootstrap') as response:
                self.assertEqual(json.load(response)['company']['id'], 'alpha')
            with opener.open(second_origin + '/api/bootstrap') as response:
                self.assertEqual(json.load(response)['company']['id'], 'beta')
            self.assertNotEqual(self.server.cookie_name, second.cookie_name)
            self.assertNotEqual(self.server.csrf_token, second.csrf_token)
        finally:
            second.shutdown()
            second.server_close()
            second_thread.join(2)

    def test_server_rejects_nonloopback_bind(self):
        with self.assertRaises(ValueError):
            create_server(self.ws, host='0.0.0.0')
        with self.assertRaises(ValueError):
            create_server(self.ws, host='localhost')

    def test_new_generation_preserves_request_and_chinese_explanation(self):
        job = self.wait_job(self.analyze(request={'mode': 'generate', 'language': 'en'}))
        detail = self.request('/api/conversation?account=a&conversation=c')[1]
        draft = next(item for item in detail['drafts'] if item['id'] == job['draft_id'])
        self.assertEqual(draft['request']['mode'], 'generate')
        self.assertEqual(draft['result']['reply_language'], 'en')
        self.assertEqual(draft['result']['rationale'], ['当前消息未说明数量。'])
        self.assertNotIn('context', draft)

    def test_polish_input_is_not_imported_as_chat_and_empty_input_fails_before_model(self):
        status, _, _ = self.request('/api/analyze', {'account_id': 'a', 'conversation_id': 'c',
                                                   'request': {'mode': 'polish', 'original_text': ''}})
        self.assertEqual(status, 400)
        self.assertIsNone(self.server.active_job())
        job = self.wait_job(self.analyze(request={'mode': 'polish', 'original_text': '谢谢，我们核实后答复。', 'language': 'en'}))
        detail = self.request('/api/conversation?account=a&conversation=c')[1]
        self.assertEqual(len(detail['messages']), 1)
        self.assertEqual(detail['drafts'][0]['id'], job['draft_id'])
        self.assertEqual(detail['drafts'][0]['request']['original_text'], '谢谢，我们核实后答复。')

    def test_refinement_cannot_borrow_another_conversation_draft(self):
        first = self.wait_job(self.analyze())['draft_id']
        self.ws.import_messages([self.record('other', conversation='other')], 'other')
        status, _, _ = self.request('/api/analyze', {'account_id': 'a', 'conversation_id': 'other',
            'request': {'mode': 'refine', 'base_draft_id': first, 'original_text': 'Hello', 'instruction': '更简短'}})
        self.assertEqual(status, 400)
        self.assertIsNone(self.server.active_job())

    def test_busy_same_conversation_different_mode_is_not_silently_reused(self):
        entered, release, calls = self.blocking_runner()
        job_id = self.analyze(request={'mode': 'generate'})
        self.assertTrue(entered.wait(2))
        status, _, _ = self.request('/api/analyze', {'account_id': 'a', 'conversation_id': 'c',
                                                   'request': {'mode': 'polish', 'original_text': '谢谢'}})
        self.assertEqual(status, 409)
        release.set()
        self.wait_job(job_id)
        self.assertEqual(len(calls), 1)

    def test_strategy_version_conflict_and_invalidation(self):
        draft_id = self.wait_job(self.analyze())['draft_id']
        before = self.request('/api/strategy')[1]
        status, published, _ = self.request('/api/strategy', {'text': '简短回复，每次优先问两个问题。',
                                                           'base_release': before['active_release']})
        self.assertEqual(status, 200, published)
        self.assertTrue(published['changed'])
        self.assertEqual(self.request('/api/strategy')[1]['text'], '简短回复，每次优先问两个问题。')
        status, _, _ = self.request('/api/strategy', {'text': '另一页面的过期修改', 'base_release': before['active_release']})
        self.assertEqual(status, 400)
        self.assertTrue(self.request('/api/conversation?account=a&conversation=c')[1]['drafts'][0]['requires_recheck'])
        self.assertEqual(self.request('/api/adopt', {'draft_id': draft_id, 'final_text': 'Hello'})[0], 400)

    def test_old_knowledge_editor_cannot_overwrite_new_strategy(self):
        old = self.request('/api/knowledge')[1]
        status, _, _ = self.request('/api/strategy', {'text': '新的中文回复偏好', 'base_release': old['active_release']})
        self.assertEqual(status, 200)
        old['config']['knowledge'][0]['title'] = '另一页面刚编辑的标题'
        status, _, _ = self.request('/api/knowledge', {'config': old['config'], 'base_release': old['active_release']})
        self.assertEqual(status, 400)
        self.assertEqual(self.request('/api/strategy')[1]['text'], '新的中文回复偏好')

    def test_adopt_is_idempotent_and_revisions_preserve_prior_words(self):
        draft_id = self.wait_job(self.analyze())['draft_id']
        data = {'draft_id': draft_id, 'final_text': 'Hello\nPlease confirm the quantity.'}
        status, first, _ = self.request('/api/adopt', data)
        self.assertEqual(status, 200, first)
        self.assertFalse(first['delivery_performed'])
        self.assertEqual(first['adoption'], 'saved')
        self.assertEqual(self.request('/api/adopt', data)[1]['draft']['id'], draft_id)
        status, revised, _ = self.request('/api/adopt', data | {'final_text': 'Hello, what quantity do you need?'})
        self.assertEqual(status, 200, revised)
        self.assertNotEqual(revised['draft']['id'], draft_id)
        self.assertEqual(revised['draft']['manual_revision_of'], draft_id)
        detail = self.request('/api/conversation?account=a&conversation=c')[1]
        self.assertEqual(next(d for d in detail['drafts'] if d['id'] == draft_id)['final_text'], data['final_text'])
        self.assertEqual(len(detail['messages']), 1)
        self.assertEqual(len(self.request('/api/activity')[1]['outbox']), 2)

    def test_even_adopted_reply_cannot_be_copied_again_after_new_messages(self):
        draft_id = self.wait_job(self.analyze())['draft_id']
        data = {'draft_id': draft_id, 'final_text': 'Hello'}
        self.assertEqual(self.request('/api/adopt', data)[0], 200)
        self.ws.import_messages([self.record('new')], 'new')
        self.assertEqual(self.request('/api/adopt', data)[0], 400)

    def test_sync_not_configured_and_paths_cannot_be_selected_by_browser(self):
        status, state, _ = self.request('/api/sync')
        self.assertEqual(status, 200)
        self.assertFalse(state['configured'])
        self.assertEqual(state['source_type'], 'none')
        self.assertEqual(self.request('/api/sync', {})[0], 400)
        self.assertEqual(self.request('/api/sync', {'path': '/private/messages.db'})[0], 400)

    def test_configured_contact_without_source_messages_is_visible_but_not_analyzable(self):
        self.server._labels = lambda: {('wa', 'waiting'): {'title': '授权待更新联系人', 'subtitle': 'WhatsApp'}}
        values = self.request('/api/conversations')[1]['items']
        self.assertEqual(len(values), 2)
        self.assertEqual(values[-1]['status'], 'needs_sync')
        status, detail, _ = self.request('/api/conversation?account=wa&conversation=waiting')
        self.assertEqual(status, 200)
        self.assertEqual(detail['messages'], [])
        self.assertEqual(self.request('/api/analyze', {'account_id': 'wa', 'conversation_id': 'waiting'})[0], 400)

    def test_new_write_routes_still_require_csrf(self):
        for path, body in [('/api/sync', {}), ('/api/adopt', {'draft_id': 'x', 'final_text': 'x'}),
                           ('/api/strategy', {'text': 'x', 'base_release': 'x'})]:
            self.assertEqual(self.request(path, body, headers={'X-CSRF-Token': None})[0], 403)


if __name__ == '__main__':
    unittest.main()
