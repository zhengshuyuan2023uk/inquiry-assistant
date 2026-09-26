"""Shutdown ownership checks with synthetic messages and local stub runners."""
import threading
import unittest

from inquiry_product.telemetry import list_runs
from inquiry_product.web_server import create_server
import test_web_server as web_fixture

StubRunner = web_fixture.StubRunner


class ShutdownTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting and rerunning its HTTP test suite.
    setUp = web_fixture.WebServerTests.setUp
    tearDown = web_fixture.WebServerTests.tearDown
    start_server = web_fixture.WebServerTests.start_server
    stop_server = web_fixture.WebServerTests.stop_server
    record = web_fixture.WebServerTests.record
    request = web_fixture.WebServerTests.request
    analyze = web_fixture.WebServerTests.analyze

    def close_in_background(self):
        self.server.shutdown()
        completed, errors = threading.Event(), []

        def close():
            try:
                self.server.server_close()
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()

        closer = threading.Thread(target=close, name='test-workbench-close', daemon=True)
        closer.start()
        self.assertTrue(self.server.closing.wait(2))
        return closer, completed, errors

    def assert_workspace_locked(self):
        unexpected = None
        try:
            with self.assertRaisesRegex(ValueError, '已有工作台'):
                unexpected = create_server(self.ws, port=0, runner_factory=StubRunner)
        finally:
            if unexpected is not None:
                unexpected.server_close()

    def assert_workspace_reusable(self):
        replacement = create_server(self.ws, port=0, runner_factory=StubRunner)
        replacement.server_close()

    def draft_count(self):
        with self.server.service() as service:
            return service.store.connection.execute('SELECT COUNT(*) FROM drafts').fetchone()[0]

    def test_shutdown_keeps_workspace_lock_until_sealed_save_finishes(self):
        sealed, release = threading.Event(), threading.Event()
        self.blockers.append(release)
        original = self.server._seal_result

        def seal(identity):
            original(identity)
            sealed.set()
            if not release.wait(10):
                raise AssertionError('test did not release sealed save')

        self.server._seal_result = seal
        identity = self.analyze()
        self.assertTrue(sealed.wait(2))
        with self.server.job_lock:
            worker = self.server.job_controls[identity]['thread']
        closer, completed, errors = self.close_in_background()
        try:
            # Exceed the old three-second join: elapsed time cannot transfer
            # workspace ownership while this worker is still able to commit.
            self.assertFalse(completed.wait(3.25))
            self.assertTrue(worker.is_alive())
            self.assertEqual(self.draft_count(), 0)
            self.assert_workspace_locked()
        finally:
            release.set()
            closer.join(5)
        self.assertTrue(completed.is_set())
        self.assertFalse(closer.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.server.job(identity)['status'], 'succeeded')
        self.assertEqual(self.draft_count(), 1)
        self.assertEqual(self.server.job_controls, {})
        self.assert_workspace_reusable()

    def test_shutdown_drains_legacy_runner_and_discards_its_late_result(self):
        entered, release = threading.Event(), threading.Event()
        self.blockers.append(release)

        class UninterruptibleRunner(StubRunner):
            def analyze(inner, context):
                entered.set()
                if not release.wait(10):
                    raise AssertionError('test did not release legacy runner')
                return super().analyze(context)

        self.server.runner_factory = UninterruptibleRunner
        identity = self.analyze()
        self.assertTrue(entered.wait(2))
        with self.server.job_lock:
            control = self.server.job_controls[identity]
            worker = control['thread']
        closer, completed, errors = self.close_in_background()
        try:
            self.assertTrue(control['cancel'].wait(2))
            self.assertFalse(completed.wait(.1))
            self.assertTrue(worker.is_alive())
            self.assert_workspace_locked()
        finally:
            release.set()
            closer.join(5)
        self.assertTrue(completed.is_set())
        self.assertFalse(closer.is_alive())
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.server.job(identity)['status'], 'cancelled')
        self.assertEqual(self.draft_count(), 0)
        self.assertEqual(self.server.job_controls, {})
        self.assertIsNone(self.server.active_job())
        run = next(run for run in list_runs(self.ws) if run['id'] == identity)
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['error_category'], 'cancelled')
        self.assert_workspace_reusable()


if __name__ == '__main__':
    unittest.main()
