"""Offline app-server protocol doubles; never invoke a model or real login."""
from copy import deepcopy
import json
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from inquiry_product import __version__
from inquiry_product.app_server import AppServerClient, AppServerRunner, ReplyJsonStream
from inquiry_product.core.engine import EngineError


def context():
    return {"mode": "simulation", "project_id": "demo", "project_name": "虚构公司",
            "industry": "test", "as_of": "2026-09-25", "required_fields": ["quantity"],
            "rules": ["人工核实价格。"], "knowledge": [], "excluded_knowledge": [],
            "messages": [{"mode": "simulation", "project_id": "demo", "account_id": "a",
                          "conversation_id": "c", "message_id": "m1", "direction": "inbound",
                          "body": "Please quote 10 cartons.", "sent_at": "2026-09-25T00:00:00Z",
                          "received_at": "2026-09-25T00:00:00Z"}]}


def result(reply='你好，"朋友"。\nPath: C:\\demo 😀'):
    return {"summary": "不得流出的摘要", "facts": [{"field": "quantity", "value": "10箱", "message_ids": ["m1"]}],
            "missing_fields": [], "uncertainties": [], "next_action": "人工核价", "reply": reply,
            "citations": [], "needs_human": True}


class ReadPipe:
    def __init__(self):
        self.queue = queue.Queue()
        self.closed = False

    def readline(self, limit=-1):
        return self.queue.get()

    def push(self, value):
        self.queue.put(value)

    def close(self):
        if not self.closed:
            self.closed = True
            self.queue.put(b"")


class WritePipe:
    def __init__(self, process):
        self.process = process

    def write(self, data):
        self.process.request(json.loads(data))
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass


class FakeProcess:
    def __init__(self, behavior=None):
        self.stdout = ReadPipe()
        self.stderr = ReadPipe()
        self.stdin = WritePipe(self)
        self.returncode = None
        self.requests = []
        self.behavior = behavior
        self.thread_count = 0
        self.interrupted = False

    def emit(self, message):
        self.stdout.push((json.dumps(message, ensure_ascii=False) + "\n").encode())

    def respond(self, request, result):
        self.emit({"id": request["id"], "result": result})

    def notify(self, method, params):
        self.emit({"method": method, "params": params})

    def request(self, request):
        self.requests.append(request)
        method = request.get("method")
        if method == "initialize":
            self.respond(request, {"userAgent": "offline-test"})
        elif method == "thread/start":
            self.thread_count += 1
            self.thread = f"thread-{self.thread_count}"
            self.turn = f"turn-{self.thread_count}"
            self.respond(request, {"thread": {"id": self.thread}, "sandbox": {"type": "readOnly"},
                                   "approvalPolicy": "never", "instructionSources": []})
        elif method == "turn/start":
            if self.behavior:
                self.behavior(self, request)
            else:
                self.answer(request)
        elif method == "turn/interrupt":
            self.interrupted = True
            self.respond(request, {})
            self.notify("turn/completed", {"threadId": self.thread,
                                          "turn": {"id": self.turn, "status": "interrupted", "items": []}})
        elif method == "thread/unsubscribe":
            self.respond(request, {})

    def answer(self, request, *, value=None, early=False, text=None):
        value = result() if value is None else value
        raw = json.dumps(value, ensure_ascii=True) if text is None else text
        if not early:
            self.respond(request, {"turn": {"id": self.turn, "status": "inProgress", "items": []}})
        item = {"id": "answer", "type": "agentMessage", "phase": "final_answer", "text": ""}
        self.notify("item/started", {"threadId": self.thread, "turnId": self.turn, "item": item})
        self.notify("item/reasoning/textDelta", {"threadId": self.thread, "turnId": self.turn, "delta": "PRIVATE_REASONING"})
        for offset in range(0, len(raw), 7):
            self.notify("item/agentMessage/delta", {"threadId": self.thread, "turnId": self.turn,
                                                    "itemId": "answer", "delta": raw[offset:offset + 7]})
        item["text"] = raw
        self.notify("item/completed", {"threadId": self.thread, "turnId": self.turn, "item": item})
        self.notify("turn/completed", {"threadId": self.thread,
                                      "turn": {"id": self.turn, "status": "completed", "items": [item]}})
        if early:
            self.respond(request, {"turn": {"id": self.turn, "status": "inProgress", "items": []}})

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.stdout.close()
        self.stderr.close()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


class ReplyStreamTests(unittest.TestCase):
    def test_only_top_level_reply_is_streamed_at_every_split(self):
        expected = '中文 "报价"\n\\path / 😀'
        raw = json.dumps({"summary": "隐藏摘要", "nested": {"reply": "隐藏嵌套"},
                          "rationale": ["隐藏理由"], "reply": expected}, ensure_ascii=True)
        for size in (1, 2, 7, 31, len(raw)):
            stream = ReplyJsonStream()
            observed = "".join(stream.feed(raw[i:i + size]) for i in range(0, len(raw), size))
            self.assertEqual(observed, expected)

    def test_raw_unicode_and_escaped_key_are_supported(self):
        stream = ReplyJsonStream()
        self.assertEqual(stream.feed('{"re\\u0070ly":"中文🙂"}'), "中文🙂")

    def test_incomplete_escape_does_not_emit_broken_character(self):
        stream = ReplyJsonStream()
        self.assertEqual(stream.feed('{"reply":"Hi \\uD83D'), "Hi ")
        self.assertEqual(stream.feed('\\uDE00'), "😀")
        self.assertEqual(stream.feed('"}'), "")

    def test_commentary_and_nested_reply_are_not_forwarded(self):
        self.assertEqual(ReplyJsonStream().feed('Here is reasoning with "reply": "secret"'), "")
        self.assertEqual(ReplyJsonStream().feed('{"nested":{"reply":"secret"}}'), "")

    def test_malformed_json_and_duplicate_reply_are_rejected(self):
        for raw in ('{"reply":12}', '{"reply":"a","reply":"b"}', '{"reply":"bad\\q"}', '{"reply":"\\uD800x"}'):
            with self.subTest(raw=raw), self.assertRaises(EngineError):
                ReplyJsonStream().feed(raw)


class AppServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source_home = Path(self.temp.name) / "original-home"
        self.source_home.mkdir()
        (self.source_home / "auth.json").write_text('synthetic-auth-not-used', encoding="utf-8")
        (self.source_home / "config.toml").write_text('private = "must not be inherited"', encoding="utf-8")
        self.client = AppServerClient(timeout=10, codex_home=self.source_home)
        self.processes = []
        self.behavior = None
        self.launches = []

        def launch(command, **kwargs):
            self.launches.append((command, kwargs))
            process = FakeProcess(self.behavior)
            self.processes.append(process)
            return process
        self.patch = patch("inquiry_product.app_server.subprocess.Popen", side_effect=launch)
        self.patch.start()

    def tearDown(self):
        self.client.close()
        self.patch.stop()
        self.temp.cleanup()

    def analyze(self, value=None, callback=None, cancel=None):
        return AppServerRunner(self.client, on_event=callback, cancel_event=cancel).analyze(value or context())

    def test_lazy_start_reuses_process_but_never_thread_history(self):
        self.assertEqual(self.processes, [])
        self.assertEqual(self.analyze(), result())
        self.assertEqual(self.analyze(), result())
        self.assertEqual(len(self.processes), 1)
        requests = self.processes[0].requests
        initialize = next(item for item in requests if item.get("method") == "initialize")
        self.assertEqual(initialize["params"]["clientInfo"]["version"], __version__)
        starts = [item for item in requests if item.get("method") == "thread/start"]
        turns = [item for item in requests if item.get("method") == "turn/start"]
        self.assertEqual(len(starts), 2)
        self.assertNotEqual(turns[0]["params"]["threadId"], turns[1]["params"]["threadId"])
        self.assertNotEqual(starts[0]["params"]["cwd"], starts[1]["params"]["cwd"])
        for item in starts:
            self.assertTrue(item["params"]["ephemeral"])
            self.assertEqual(item["params"]["environments"], [])
            self.assertEqual(item["params"]["dynamicTools"], [])
            self.assertEqual(item["params"]["sandbox"], "read-only")
        self.assertEqual(turns[0]["params"]["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        self.assertEqual(turns[0]["params"]["environments"], [])
        self.assertIn("outputSchema", turns[0]["params"])

    def test_isolated_home_only_references_auth_and_preserves_proxy(self):
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy", "WHATSAPP_TOKEN": "private", "OPENAI_API_KEY": "private"}):
            self.analyze()
        command, kwargs = self.launches[0]
        env = kwargs["env"]
        home = Path(env["CODEX_HOME"])
        self.assertNotEqual(home, self.source_home)
        self.assertTrue((home / "auth.json").is_symlink())
        self.assertEqual((home / "auth.json").readlink(), self.source_home.resolve() / "auth.json")
        self.assertFalse((home / "config.toml").exists())
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy")
        self.assertNotIn("WHATSAPP_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertIn('web_search="disabled"', command)
        self.assertIn("mcp_servers={}", command)
        self.assertIn("project_doc_max_bytes=0", command)
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "browser_use", "image_generation", "skill_search", "in_app_browser", "workspace_dependencies", "remote_control"):
            self.assertIn(["--disable", feature], [command[i:i + 2] for i in range(len(command) - 1)])
        self.client.close()
        self.assertFalse(home.exists())
        self.assertTrue((self.source_home / "auth.json").exists())

    def test_missing_auth_does_not_launch_or_copy_credentials(self):
        empty = Path(self.temp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(EngineError) as caught:
            AppServerRunner(AppServerClient(codex_home=empty)).analyze(context())
        self.assertEqual(caught.exception.category, "auth")
        self.assertEqual(self.processes, [])

    def test_only_reply_deltas_and_safe_status_are_sent(self):
        events = []
        self.analyze(callback=events.append)
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "reply_delta"), result()["reply"])
        self.assertTrue(any(e["type"] == "identity" and e.get("turn_id") == "turn-1" for e in events))
        rendered = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("PRIVATE_REASONING", rendered)
        self.assertNotIn("不得流出的摘要", rendered)
        self.assertNotIn('"facts"', rendered)

    def test_events_before_turn_start_response_are_correlated_and_replayed(self):
        self.behavior = lambda proc, request: proc.answer(request, early=True)
        events = []
        self.assertEqual(self.analyze(callback=events.append), result())
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "reply_delta"), result()["reply"])

    def test_wrong_turn_and_other_thread_events_are_ignored(self):
        def behavior(proc, request):
            for thread, turn in ((proc.thread, "wrong-turn"), ("foreign-thread", proc.turn)):
                proc.notify("item/agentMessage/delta", {"threadId": thread, "turnId": turn, "itemId": "evil", "delta": '{"reply":"FOREIGN"}'})
                proc.notify("turn/completed", {"threadId": thread, "turn": {"id": turn, "status": "failed", "items": [], "error": {"message": "FOREIGN"}}})
            proc.answer(request)
        self.behavior = behavior
        events = []
        self.assertEqual(self.analyze(callback=events.append), result())
        self.assertNotIn("FOREIGN", json.dumps(events))

    def test_cancel_interrupts_and_returns_no_result(self):
        cancel = threading.Event()
        def callback(event):
            if event["type"] == "reply_delta":
                cancel.set()
        with self.assertRaises(EngineError) as caught:
            self.analyze(callback=callback, cancel=cancel)
        self.assertEqual(caught.exception.category, "cancelled")
        self.assertTrue(self.processes[0].interrupted)

    def test_cancel_before_start_response_terminates_process(self):
        cancel = threading.Event()
        self.behavior = lambda proc, request: cancel.set()
        with self.assertRaises(EngineError) as caught:
            self.analyze(cancel=cancel)
        self.assertEqual(caught.exception.category, "cancelled")
        self.assertIsNotNone(self.processes[0].returncode)

    def test_pre_cancelled_request_never_launches_model(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(EngineError) as caught:
            self.analyze(cancel=cancel)
        self.assertEqual(caught.exception.category, "cancelled")
        self.assertEqual(self.processes, [])

    def test_timeout_terminates_and_next_request_reconnects_without_auto_retry(self):
        self.behavior = lambda proc, request: proc.respond(request, {"turn": {"id": proc.turn, "items": [], "status": "inProgress"}})
        self.client.timeout = 0.05
        with self.assertRaises(EngineError) as caught:
            self.analyze()
        self.assertEqual(caught.exception.category, "timeout")
        self.assertEqual(len(self.processes), 1)
        self.behavior = None
        self.client.timeout = 10
        self.assertEqual(self.analyze(), result())
        self.assertEqual(len(self.processes), 2)

    def test_failed_turn_has_safe_category_and_no_raw_error(self):
        def behavior(proc, request):
            proc.respond(request, {"turn": {"id": proc.turn, "items": [], "status": "inProgress"}})
            proc.notify("turn/completed", {"threadId": proc.thread, "turn": {"id": proc.turn, "items": [], "status": "failed",
                        "error": {"message": "DO_NOT_EXPOSE_TOKEN", "codexErrorInfo": "usageLimitExceeded"}}})
        self.behavior = behavior
        with self.assertRaises(EngineError) as caught:
            self.analyze()
        self.assertEqual(caught.exception.category, "rate_limit")
        self.assertNotIn("DO_NOT_EXPOSE_TOKEN", str(caught.exception))

    def test_bad_final_json_and_missing_required_fields_fail_without_fallback(self):
        for raw in ("not json", '{"reply":"only partial"}', json.dumps(result())[:-1] + ',"needs_human":false}'):
            self.behavior = lambda proc, request, raw=raw: proc.answer(request, text=raw)
            with self.subTest(raw=raw), self.assertRaises(EngineError):
                self.analyze()

    def test_public_close_prevents_lazy_launch_and_later_relaunch(self):
        self.client.close()
        with self.assertRaises(EngineError):
            self.analyze()
        self.assertEqual(self.processes, [])

    def test_close_while_waiting_for_execution_lock_does_not_launch(self):
        self.client._lock.acquire()
        errors = []
        def worker():
            try:
                self.analyze()
            except EngineError as exc:
                errors.append(exc)
        thread = threading.Thread(target=worker)
        thread.start()
        self.client.close()
        self.client._lock.release()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.processes, [])

    def test_public_close_of_reusable_process_never_launches_replacement(self):
        self.analyze()
        self.client.close()
        with self.assertRaises(EngineError):
            self.analyze()
        self.assertEqual(len(self.processes), 1)

    def test_process_death_and_malformed_protocol_fail_clearly(self):
        for behavior, category in ((lambda proc, request: proc.terminate(), "process"),
                                   (lambda proc, request: proc.stdout.push(b'not protocol\n'), "protocol")):
            self.behavior = behavior
            with self.subTest(category=category), self.assertRaises(EngineError) as caught:
                self.analyze()
            self.assertEqual(caught.exception.category, category)

    def test_tool_events_and_server_requests_are_not_silently_accepted(self):
        def request_tool(proc, request):
            proc.emit({"id": "tool-call", "method": "item/commandExecution/requestApproval", "params": {"command": "PRIVATE"}})
        def started_tool(proc, request):
            proc.respond(request, {"turn": {"id": proc.turn, "items": [], "status": "inProgress"}})
            proc.notify("item/started", {"threadId": proc.thread, "turnId": proc.turn,
                                        "item": {"type": "mcpToolCall", "id": "tool", "arguments": "PRIVATE"}})
        for behavior in (request_tool, started_tool):
            self.behavior = behavior
            with self.subTest(behavior=behavior), self.assertRaises(EngineError) as caught:
                self.analyze()
            self.assertEqual(caught.exception.category, "tool_access")
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_output_size_limit_stops_the_run(self):
        self.behavior = lambda proc, request: proc.answer(request, value=result("x" * 1500))
        with patch("inquiry_product.app_server._MAX_OUTPUT_BYTES", 1000), self.assertRaises(EngineError) as caught:
            self.analyze()
        self.assertEqual(caught.exception.category, "schema")
        self.assertIsNotNone(self.processes[0].returncode)

    def test_authoritative_completion_cannot_replace_a_different_streamed_prefix(self):
        def behavior(proc, request):
            proc.respond(request, {"turn": {"id": proc.turn, "items": [], "status": "inProgress"}})
            proc.notify("item/started", {"threadId": proc.thread, "turnId": proc.turn,
                                        "item": {"type": "agentMessage", "phase": "final_answer", "id": "answer", "text": ""}})
            proc.notify("item/agentMessage/delta", {"threadId": proc.thread, "turnId": proc.turn,
                                                    "itemId": "answer", "delta": '{"reply":"prefix"'})
            proc.notify("item/completed", {"threadId": proc.thread, "turnId": proc.turn,
                                          "item": {"type": "agentMessage", "phase": "final_answer", "id": "answer", "text": json.dumps(result())}})
        self.behavior = behavior
        with self.assertRaises(EngineError) as caught:
            self.analyze()
        self.assertEqual(caught.exception.category, "schema")

    def test_commentary_even_when_json_shaped_is_not_streamed(self):
        def behavior(proc, request):
            proc.respond(request, {"turn": {"id": proc.turn, "items": [], "status": "inProgress"}})
            payload = {"type": "agentMessage", "phase": "commentary", "id": "comment", "text": ""}
            proc.notify("item/started", {"threadId": proc.thread, "turnId": proc.turn, "item": payload})
            proc.notify("item/agentMessage/delta", {"threadId": proc.thread, "turnId": proc.turn,
                                                    "itemId": "comment", "delta": '{"reply":"HIDDEN_COMMENTARY"}'})
            payload["text"] = '{"reply":"HIDDEN_COMMENTARY"}'
            proc.notify("item/completed", {"threadId": proc.thread, "turnId": proc.turn, "item": payload})
            proc.answer(request)
        self.behavior = behavior
        events = []
        self.assertEqual(self.analyze(callback=events.append), result())
        self.assertNotIn("HIDDEN_COMMENTARY", json.dumps(events))

    def test_cancel_with_no_interrupt_ack_terminates_instead_of_hanging(self):
        cancel = threading.Event()
        def behavior(proc, request):
            proc.respond(request, {"turn": {"id": proc.turn, "items": [], "status": "inProgress"}})
        self.behavior = behavior
        def callback(event):
            if event["type"] == "identity" and event.get("turn_id"):
                cancel.set()
                original = self.processes[0].request
                def no_ack(request):
                    if request.get("method") == "turn/interrupt":
                        self.processes[0].interrupted = True
                    else:
                        original(request)
                self.processes[0].request = no_ack
        with patch("inquiry_product.app_server._INTERRUPT_SECONDS", 0.01), self.assertRaises(EngineError) as caught:
            self.analyze(callback=callback, cancel=cancel)
        self.assertEqual(caught.exception.category, "cancelled")
        self.assertTrue(self.processes[0].interrupted)
        self.assertIsNotNone(self.processes[0].returncode)

    def test_polish_schema_model_and_low_effort_preserve_existing_contract(self):
        current = context()
        current["request"] = {"mode": "polish", "original_text": "Hello", "language": "en", "instruction": "", "model": "example-model"}
        output = {"reply": "Hello.", "rationale": ["保留原意。"], "reply_language": "en", "warnings": [], "citations": [], "needs_human": True}
        self.behavior = lambda proc, request: proc.answer(request, value=output)
        value = self.analyze(current)
        self.assertEqual(value["reply"], "Hello.")
        self.assertEqual(value["facts"], [])
        turns = [r for r in self.processes[0].requests if r.get("method") == "turn/start"]
        self.assertEqual(turns[0]["params"]["model"], "example-model")
        self.assertEqual(turns[0]["params"]["effort"], "low")
        self.assertNotIn("facts", turns[0]["params"]["outputSchema"]["properties"])


if __name__ == "__main__":
    unittest.main()
