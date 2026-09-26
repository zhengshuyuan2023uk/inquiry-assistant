"""HTTP lifecycle checks with deterministic local runners; no model calls."""
import http.client
import json
import threading
import time
import unittest
from unittest.mock import patch

from inquiry_product.core.engine import EngineError
from inquiry_product.jobs import JobStore
from inquiry_product.telemetry import start_run, list_runs
from inquiry_product.web_server import create_server
import test_web_server as fixtures

StubRunner = fixtures.StubRunner


class StreamingTests(unittest.TestCase):
    setUp = fixtures.WebServerTests.setUp
    tearDown = fixtures.WebServerTests.tearDown
    start_server = fixtures.WebServerTests.start_server
    stop_server = fixtures.WebServerTests.stop_server
    record = fixtures.WebServerTests.record
    request = fixtures.WebServerTests.request
    analyze = fixtures.WebServerTests.analyze
    wait_job = fixtures.WebServerTests.wait_job

    def fake_app_server(self, fail=False):
        entered, release = threading.Event(), threading.Event()
        self.blockers.append(release)
        clients, calls = [], []
        class Client:
            def __init__(inner):
                clients.append(inner)
            def close(inner):
                release.set()
        class Runner(StubRunner):
            name = 'codex_app_server'
            def __init__(inner, client, on_event=None, cancel_event=None):
                inner.event, inner.cancel = on_event, cancel_event
            def analyze(inner, context):
                calls.append(context)
                inner.event({'type': 'reply_delta', 'text': 'Hello <script>alert(1)</script>\n'})
                entered.set()
                deadline = time.monotonic() + 4
                while not release.is_set():
                    if inner.cancel.wait(.01):
                        raise EngineError('生成已停止', 'cancelled')
                    if time.monotonic() > deadline:
                        raise AssertionError('test runner was not released')
                if fail:
                    raise EngineError('网络连接中断', 'network')
                return super().analyze(context)
        self.server.runner_factory = None
        client_patch = patch('inquiry_product.app_server.AppServerClient', Client)
        runner_patch = patch('inquiry_product.app_server.AppServerRunner', Runner)
        client_patch.start()
        runner_patch.start()
        self.addCleanup(client_patch.stop)
        self.addCleanup(runner_patch.stop)
        return entered, release, clients, calls

    def terminal(self, identity):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            job = self.request('/api/jobs?id=' + identity)[1]['job']
            if job['status'] not in ('queued', 'running', 'cancelling'):
                return job
            time.sleep(.01)
        self.fail('job did not reach terminal state')

    def test_sse_snapshot_reconnect_does_not_repeat_generation(self):
        entered, release, clients, calls = self.fake_app_server()
        identity = self.analyze()
        self.assertTrue(entered.wait(2))
        for _ in range(2):
            connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
            connection.request('GET', '/api/jobs/events?id=' + identity, headers={'Cookie': self.cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn('text/event-stream', response.getheader('Content-Type'))
            frame = []
            while True:
                line = response.readline().decode('utf-8')
                if line == '\n':
                    break
                frame.append(line)
            job = json.loads(next(line[6:] for line in frame if line.startswith('data: ')))
            self.assertEqual(job['preview_reply'], 'Hello <script>alert(1)</script>\n')
            self.assertNotIn('draft_id', job)
            connection.close()
        self.assertEqual(len(calls), 1)
        release.set()
        self.assertEqual(self.terminal(identity)['status'], 'succeeded')
        self.assertEqual(len(clients), 1)

    def test_cancel_is_confirmed_and_never_creates_draft(self):
        entered, _, _, _ = self.fake_app_server()
        identity = self.analyze()
        self.assertTrue(entered.wait(2))
        code, value, _ = self.request('/api/jobs/cancel', {'id': identity})
        self.assertEqual(code, 200)
        self.assertEqual(value['job']['status'], 'cancelling')
        job = self.terminal(identity)
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(job['preview_reply'], '')
        self.assertEqual(self.request('/api/conversation?account=a&conversation=c')[1]['drafts'], [])
        run = self.request('/api/activity')[1]['runs'][0]
        self.assertEqual(run['runner'], 'codex_app_server')
        self.assertEqual(run['error_category'], 'cancelled')
        self.assertIsNone(self.server.active_job())

    def test_failed_preview_is_discarded_and_next_job_can_run(self):
        entered, release, _, _ = self.fake_app_server(fail=True)
        identity = self.analyze()
        self.assertTrue(entered.wait(2))
        release.set()
        self.assertEqual(self.terminal(identity)['status'], 'failed')
        self.assertEqual(self.server.job(identity)['preview_reply'], '')
        self.assertEqual(self.request('/api/conversation?account=a&conversation=c')[1]['drafts'], [])
        self.server.runner_factory = StubRunner
        self.wait_job(self.analyze())

    def test_model_process_is_reused_for_separate_jobs(self):
        entered, release, clients, calls = self.fake_app_server()
        first = self.analyze()
        self.assertTrue(entered.wait(2))
        release.set()
        self.terminal(first)
        second = self.analyze()
        self.terminal(second)
        self.assertNotEqual(first, second)
        self.assertEqual(len(clients), 1)
        self.assertEqual(len(calls), 2)

    def test_cancel_requires_csrf_and_stream_requires_cookie(self):
        self.assertEqual(self.request('/api/jobs/events?id=absent', cookie=False)[0], 401)
        self.assertEqual(self.request('/api/jobs/events?id=absent')[0], 404)
        self.assertEqual(self.request('/api/jobs/cancel', {'id': 'absent'}, headers={'X-CSRF-Token': None})[0], 403)
        self.assertEqual(self.request('/api/jobs/cancel', {'id': 'absent'})[0], 404)

    def test_launcher_shutdown_refuses_to_interrupt_an_active_reply(self):
        entered, release, _, _ = self.fake_app_server()
        identity = self.analyze()
        self.assertTrue(entered.wait(2))
        instance = self.request('/api/bootstrap')[1]['instance_id']
        self.assertEqual(self.request('/api/shutdown', {'instance_id': instance})[0], 409)
        self.assertFalse(self.server.closing.is_set())
        self.assertTrue(self.thread.is_alive())
        release.set()
        self.assertEqual(self.terminal(identity)['status'], 'succeeded')

    def test_finished_task_survives_server_restart(self):
        identity = self.analyze()
        draft = self.wait_job(identity)['draft_id']
        self.stop_server()
        self.start_server()
        job = self.request('/api/jobs?id=' + identity)[1]['job']
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(job['draft_id'], draft)
        self.assertIsNone(self.server.active_job())

    def test_unfinished_task_is_reported_interrupted_without_reexecution(self):
        self.stop_server()
        store = JobStore(self.ws)
        store.save({'id': 'unfinished', 'account_id': 'a', 'conversation_id': 'c',
                    'status': 'running', 'started_at': '2026-09-25T00:00:00Z', 'revision': 1})
        start_run(self.ws, 'c', {'messages': []}, run_id='unfinished', runner='codex_app_server')
        start_run(self.ws, 'c', {'messages': []}, run_id='independent-cli')
        self.start_server()
        job = self.request('/api/jobs?id=unfinished')[1]['job']
        self.assertEqual(job['status'], 'interrupted')
        self.assertIn('重新生成', job['error'])
        self.assertIsNone(self.server.active_job())
        self.assertEqual(self.request('/api/bootstrap')[1]['recent_jobs'][0]['id'], 'unfinished')
        runs = {run['id']: run for run in list_runs(self.ws)}
        self.assertEqual(runs['unfinished']['error_category'], 'interrupted')
        self.assertEqual(runs['independent-cli']['status'], 'running')

    def test_same_workspace_cannot_have_two_live_servers(self):
        with self.assertRaisesRegex(ValueError, '已有工作台'):
            create_server(self.ws, port=0, runner_factory=StubRunner)

    def test_telemetry_failure_does_not_hide_saved_draft(self):
        with patch('inquiry_product.web_server.finish_run', side_effect=OSError('offline failure')):
            identity = self.analyze()
            job = self.terminal(identity)
        self.assertEqual(job['status'], 'succeeded')
        self.assertIn('运行记录写入失败', job['progress_message'])
        drafts = self.request('/api/conversation?account=a&conversation=c')[1]['drafts']
        self.assertEqual(drafts[0]['id'], job['draft_id'])

    def test_job_metadata_failure_does_not_report_saved_reply_as_failed(self):
        original = self.server.job_store.save
        def save(job):
            if job['status'] == 'succeeded':
                raise OSError('offline metadata failure')
            return original(job)
        with patch.object(self.server.job_store, 'save', side_effect=save):
            job = self.terminal(self.analyze())
        self.assertEqual(job['status'], 'succeeded')
        self.assertIn('任务记录写入失败', job['progress_message'])
        drafts = self.request('/api/conversation?account=a&conversation=c')[1]['drafts']
        self.assertEqual(drafts[0]['id'], job['draft_id'])

    def test_late_cancel_cannot_claim_saved_reply_was_cancelled(self):
        self.fake_app_server()
        # Seal is called between model completion and draft persistence.
        original = self.server._seal_result
        sealed, release = threading.Event(), threading.Event()
        self.blockers.append(release)
        def seal(identity):
            original(identity)
            sealed.set()
            release.wait(3)
        self.server._seal_result = seal
        # Let the fake model finish; keep the save boundary blocked.
        self.blockers[0].set()
        identity = self.analyze()
        self.assertTrue(sealed.wait(2))
        self.assertEqual(self.request('/api/jobs/cancel', {'id': identity})[0], 409)
        release.set()
        self.assertEqual(self.terminal(identity)['status'], 'succeeded')


if __name__ == '__main__':
    unittest.main()
