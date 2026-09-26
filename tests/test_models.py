"""Offline tests of model metadata discovery; no real CLI or model is called."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from inquiry_product import models


def model_row(identity="gpt-test", default=True, hidden=False):
    return {"id": identity, "model": identity, "displayName": "Test model",
            "isDefault": default, "hidden": hidden, "defaultReasoningEffort": "low",
            "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.real_popen = subprocess.Popen
        self.children = []
        models._cache = None
        models._cache_until = 0

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
        models._cache = None
        models._cache_until = 0

    def fake_cli(self, script):
        def spawn(command, **kwargs):
            self.assertEqual(command, [models._CODEX, "app-server", "--stdio"])
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("CODEX_THREAD_ID", kwargs["env"])
            child = self.real_popen([sys.executable, "-u", "-c", script], **kwargs)
            self.children.append(child)
            return child
        return patch("inquiry_product.models.subprocess.Popen", side_effect=spawn)

    def test_protocol_only_initializes_and_lists_with_pagination(self):
        rows = [model_row(), model_row("gpt-second", default=False)]
        script = """
import json, sys
def read(): return json.loads(sys.stdin.readline())
def send(value): print(json.dumps(value), flush=True)
request = read()
assert request['method'] == 'initialize'
send({'method':'notice','params':{}})
send({'id':request['id'],'result':{}})
assert read()['method'] == 'initialized'
request = read()
assert request['method'] == 'model/list' and request['params']['includeHidden'] is False
send({'id':request['id'],'result':{'data':ROWS[:1], 'nextCursor':'second-page'}})
request = read()
assert request['method'] == 'model/list' and request['params']['cursor'] == 'second-page'
send({'id':request['id'],'result':{'data':ROWS[1:], 'nextCursor':None}})
sys.stdin.read()
""".replace("ROWS", repr(rows))
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-secret", "CODEX_THREAD_ID": "parent-thread"}), self.fake_cli(script):
            result = models.get_model_catalog()
        self.assertNotIn("error", result)
        self.assertEqual([item["id"] for item in result["models"]], ["gpt-test", "gpt-second"])
        self.assertEqual(result["models"][0]["reasoning_efforts"], ["low", "high"])
        self.assertTrue(result["models"][0]["default"])
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def test_hidden_models_and_duplicates_are_not_offered(self):
        result = models._public_models([model_row(), model_row(), model_row("internal", hidden=True)])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "gpt-test")
        self.assertEqual(set(result[0]), {"id", "name", "default", "reasoning_efforts", "default_reasoning_effort"})

    def test_rejects_invalid_catalog_identity_and_capabilities(self):
        cases = [None, [], [{}], [model_row("--config")], [model_row("model/path")],
                 [model_row() | {"supportedReasoningEfforts": "low"}],
                 [model_row() | {"defaultReasoningEffort": "ultra"}],
                 [model_row() | {"displayName": ""}], [model_row(hidden=True)]]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(models._CatalogError):
                models._public_models(rows)

    def test_success_cache_is_copied_and_shared_by_concurrent_callers(self):
        def query():
            time.sleep(0.02)
            return models._public_models([model_row()])
        with patch("inquiry_product.models._query_catalog", side_effect=query) as query_mock:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: models.get_model_catalog(), range(4)))
            results[0]["models"][0]["id"] = "changed-by-caller"
            result = models.get_model_catalog()
        self.assertEqual(query_mock.call_count, 1)
        self.assertEqual(result["models"][0]["id"], "gpt-test")

    def test_failed_refresh_never_falls_back_to_stale_catalog(self):
        with patch("inquiry_product.models._CACHE_SECONDS", 0), patch(
                "inquiry_product.models._query_catalog", side_effect=[models._public_models([model_row()]), OSError("private diagnostics")]):
            self.assertTrue(models.get_model_catalog()["models"])
            result = models.get_model_catalog()
        self.assertEqual(result["models"], [])
        self.assertIn("error", result)
        self.assertNotIn("private diagnostics", result["error"])
        self.assertNotIn("Codex", result["error"])
        self.assertNotIn("CLI", result["error"])
        self.assertIn("负责人", result["error"])

    def test_failure_cache_is_short_and_retry_can_recover(self):
        with patch("inquiry_product.models._ERROR_CACHE_SECONDS", 0), patch(
                "inquiry_product.models._query_catalog", side_effect=[OSError("missing"), models._public_models([model_row()])]) as query:
            self.assertFalse(models.get_model_catalog()["models"])
            self.assertTrue(models.get_model_catalog()["models"])
        self.assertEqual(query.call_count, 2)
        self.assertEqual(models._ERROR_CACHE_SECONDS, 1)

    def test_hung_cli_times_out_and_is_stopped(self):
        started = time.monotonic()
        with patch("inquiry_product.models._QUERY_TIMEOUT_SECONDS", 0.05), self.fake_cli("import time; time.sleep(30)"):
            result = models.get_model_catalog()
        self.assertEqual(result["models"], [])
        self.assertIn("超时", result["error"])
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def test_non_json_and_oversized_output_are_bounded(self):
        for script, limit in (("print('not JSON', flush=True)", 1000),
                              ("print('x' * 10000, flush=True)", 100)):
            models._cache = None
            with self.subTest(script=script), patch("inquiry_product.models._MAX_OUTPUT_BYTES", limit), self.fake_cli(script):
                result = models.get_model_catalog()
            self.assertEqual(result["models"], [])
            self.assertIn("error", result)
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def test_rpc_error_does_not_expose_raw_diagnostics(self):
        script = "import json,sys; q=json.loads(sys.stdin.readline()); print(json.dumps({'id':q['id'],'error':{'message':'secret diagnostic'}}),flush=True)"
        with self.fake_cli(script):
            result = models.get_model_catalog()
        self.assertEqual(result["models"], [])
        self.assertNotIn("secret diagnostic", result["error"])

    def test_repeated_pagination_cursor_fails_without_partial_catalog(self):
        script = """
import json,sys
for line in sys.stdin:
    q=json.loads(line)
    if q['method']=='initialize':
        print(json.dumps({'id':q['id'],'result':{}}),flush=True)
    elif q['method']=='model/list':
        print(json.dumps({'id':q['id'],'result':{'data':ROWS,'nextCursor':'repeated'}}),flush=True)
""".replace("ROWS", repr([model_row()]))
        with self.fake_cli(script):
            result = models.get_model_catalog()
        self.assertEqual(result["models"], [])
        self.assertIn("分页", result["error"])


if __name__ == "__main__":
    unittest.main()
