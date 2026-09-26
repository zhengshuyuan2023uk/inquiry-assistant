/* Offline UI state checks. No real browser, network, model, or chat data is used. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const appRoot = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(appRoot, "inquiry_product/web/index.html"), "utf8");
const script = fs.readFileSync(path.join(appRoot, "inquiry_product/web/app.js"), "utf8");
const entrypoint = 'bindEvents();setPane("list");initialize();';
assert(script.includes(entrypoint), "offline harness must suppress normal startup");
const exposed = "globalThis.ui={state,bindEvents,initialize,renderReply,renderJob,watchJob,acceptJobSnapshot,pollJob,cancelGeneration,reconnectJob,requestAnalysis,adoptReply,selectConversation};";

function decode(value) {
  return value.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function settle() { for (let index = 0; index < 35; index++) await Promise.resolve(); }

function harness({ eventSource = true } = {}) {
  const elements = new Map(), timers = new Map(), routes = new Map(), calls = [], streams = [], copied = [];
  let timerId = 0;
  class Element {
    constructor(id) {
      this.id = id; this.value = ""; this.textContent = ""; this._html = ""; this.children = [];
      this.hidden = false; this.disabled = false; this.readOnly = false; this.dataset = {}; this.attributes = new Map(); this.handlers = new Map();
      this.scrollTop = 0; this.scrollHeight = 100; this.clientHeight = 100;
      const classes = new Set();
      this.classList = { add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name), toggle(name, force) { const yes = force === undefined ? !classes.has(name) : force; if (yes) classes.add(name); else classes.delete(name); return yes; } };
    }
    set innerHTML(value) {
      this._html = value;
      for (const id of this.children) elements.delete(id);
      this.children = [];
      for (const match of value.matchAll(/\bid="([^"<>]+)"/g)) {
        const node = new Element(match[1]); elements.set(match[1], node); this.children.push(match[1]);
      }
      for (const match of value.matchAll(/<textarea\b[^>]*id="([^"<>]+)"[^>]*>([\s\S]*?)<\/textarea>/g)) {
        elements.get(match[1]).value = decode(match[2]);
      }
      for (const match of value.matchAll(/<input\b[^>]*id="([^"<>]+)"[^>]*value="([^"<>]*)"/g)) {
        elements.get(match[1]).value = decode(match[2]);
      }
    }
    get innerHTML() { return this._html; }
    addEventListener(name, handler) { this.handlers.set(name, handler); }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name); }
    removeAttribute(name) { this.attributes.delete(name); }
    querySelectorAll() { return []; }
    focus() {} select() {} scrollIntoView() {} close() {} showModal() {}
    input(value) { this.value = value; this.handlers.get("input")?.({ target: this }); }
  }
  for (const match of html.matchAll(/\bid="([^"<>]+)"/g)) elements.set(match[1], new Element(match[1]));
  const grid = new Element("inbox-grid");
  class MockEventSource {
    constructor(url) { this.url = url; this.handlers = new Map(); this.closed = false; streams.push(this); }
    addEventListener(name, handler) { this.handlers.set(name, handler); }
    open() { this.onopen?.(); }
    emit(job) { this.handlers.get("job")?.({ data: JSON.stringify(job) }); }
    raw(value) { this.handlers.get("job")?.({ data: value }); }
    fail() { this.onerror?.(); }
    close() { this.closed = true; }
  }
  const context = {
    console, Map, Set, Intl, Date, URLSearchParams, AbortController,
    document: { getElementById: id => elements.get(id) || null, querySelectorAll: () => [], querySelector: () => grid },
    location: { port: "8765" }, localStorage: { getItem: () => null, setItem() {} },
    setTimeout(fn, delay = 0) { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timers.delete(id); }, requestAnimationFrame(fn) { fn(); },
    navigator: { clipboard: { async writeText(value) { copied.push(value); } } },
    fetch: async (url, options = {}) => {
      const parsed = new URL(url, "http://example.test");
      const method = options.method || "GET";
      calls.push({ method, path: parsed.pathname, query: parsed.searchParams, body: options.body ? JSON.parse(options.body) : null, headers: options.headers });
      const handler = routes.get(method + " " + parsed.pathname);
      if (!handler) throw new Error("unexpected offline request: " + method + " " + parsed.pathname);
      const result = await handler(calls[calls.length - 1]);
      if (result && result.httpError) return { ok: false, status: result.httpError, json: async () => ({ error: result.error || "Request failed" }) };
      return { ok: true, status: 200, json: async () => result };
    },
  };
  if (eventSource) context.EventSource = MockEventSource;
  vm.createContext(context); vm.runInContext(script.replace(entrypoint, exposed), context);
  const ui = context.ui;
  const env = { ui, calls, streams, routes, copied, timers, element: id => elements.get(id), async timer(delay) {
    const item = [...timers.entries()].find(([, timer]) => timer.delay === delay);
    assert(item, `expected a ${delay} ms timer`); timers.delete(item[0]); item[1].fn(); await settle();
  } };
  ui.bindEvents();
  return env;
}

const contact = (id = "a") => ({ account_id: "sales", conversation_id: id, title: "客户 " + id, subtitle: "虚构记录", preview: "Hello", status: "pending", message_count: 1 });
const draft = (id = "draft-a", reply = "原始建议") => ({ id, account_id: "sales", conversation_id: "a", status: "pending", runner: "codex", requires_recheck: false, request: { model: "test-model", mode: "generate" }, result: { reply, rationale: [], warnings: [], facts: [], citations: [] } });
const detail = (id = "a", drafts = [draft()]) => ({ conversation: contact(id), messages: [{ message_id: "m-" + id, direction: "inbound", body: "Hello", sent_at: "2026-09-25T00:00:00Z" }], drafts, required_fields: [], knowledge: [] });
const job = (changes = {}) => ({ id: "job-a", status: "running", account_id: "sales", conversation_id: "a", revision: 1, can_cancel: true, preview_reply: "", progress_message: "正在生成回复。", ...changes });
function ready(env) {
  const state = env.ui.state;
  state.bootstrap = { company: { id: "demo", name: "演练企业", mode: "simulation" }, capabilities: { model: "codex_app_server", customer_model_allowed: true }, csrf_token: "offline-csrf" };
  state.models = [{ id: "test-model", name: "测试模型" }]; state.modelsLoaded = true; state.modelId = "test-model";
  state.selected = contact(); state.detail = detail(); state.conversations = [contact(), contact("b")];
  env.element("status-filter").value = "all";
  env.ui.renderReply(); env.ui.renderJob();
  env.routes.set("GET /api/conversations", async () => ({ items: state.conversations }));
  env.routes.set("GET /api/conversation", async request => detail(request.query.get("conversation")));
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

test("preview is separate, escaped as text, and cannot be adopted", async () => {
  const env = harness(); ready(env);
  env.element("reply-editor").input("人工修改不要覆盖");
  env.ui.watchJob(job()); env.streams[0].open();
  const payload = '<img src=x onerror="alert(1)"> & </textarea><script>bad()</script>';
  env.streams[0].emit(job({ revision: 2, preview_reply: payload }));
  assert.equal(env.element("job-preview-text").textContent, payload);
  assert.equal(env.element("job-preview-text").innerHTML, "");
  assert.equal(env.element("reply-editor").value, "人工修改不要覆盖");
  assert.equal(env.ui.state.replyEdits.get("draft-a").text, "人工修改不要覆盖");
  assert.equal(env.element("reply-editor").readOnly, true);
  assert.equal(env.element("adopt-button").disabled, true);
  await env.ui.adoptReply();
  assert.equal(env.calls.length, 0);
  assert.equal(env.element("job-preview").hidden, false);
});

test("switching customer never displays another customer's preview", async () => {
  const env = harness(); ready(env); env.ui.watchJob(job());
  env.streams[0].emit(job({ revision: 2, preview_reply: "客户 A 的预览" }));
  await env.ui.selectConversation(contact("b"));
  env.streams[0].emit(job({ revision: 3, preview_reply: "客户 A 后续预览" }));
  assert.equal(env.element("job-preview").hidden, true);
  assert.equal(env.element("job-preview-text").textContent, "");
  assert.equal(env.ui.state.selected.conversation_id, "b");
  assert(env.element("job-status-text").textContent.includes("另一位客户"));
  assert.equal(env.element("adopt-button").disabled, false);
});

test("late, equal-revision, wrong-scope and post-terminal updates are ignored", async () => {
  const env = harness(); ready(env); env.ui.watchJob(job()); const source = env.streams[0];
  source.emit(job({ revision: 3, preview_reply: "最新" }));
  source.emit(job({ revision: 2, preview_reply: "旧" }));
  source.emit(job({ revision: 3, preview_reply: "同版旧内容" }));
  source.emit(job({ revision: 4, conversation_id: "b", preview_reply: "跨客户" }));
  source.emit(job({ revision: 4, id: "other", preview_reply: "跨任务" }));
  assert.equal(env.element("job-preview-text").textContent, "最新");
  source.emit(job({ revision: 4, status: "failed", error: "模型连接暂时不可用，请检查网络后重试。" }));
  await settle();
  assert.equal(source.closed, true);
  assert.equal(env.element("generate-button").disabled, false);
  assert.equal(env.element("reply-editor").readOnly, false);
  assert(env.element("job-status-text").textContent.includes("请检查网络后重试"));
  source.emit(job({ revision: 5, preview_reply: "迟到" }));
  assert.equal(env.ui.state.activeJob.status, "failed");
  assert.equal(env.element("job-preview").hidden, true);
});

test("safe failure reason is shown as text in the banner and toast", async () => {
  const env = harness(); ready(env); env.element("reply-editor").input("原稿保持不变"); env.ui.watchJob(job());
  const error = '账号需要重新登录。<img src=x onerror="alert(1)">';
  env.streams[0].emit(job({ revision: 2, status: "failed", error })); await settle();
  assert(env.element("job-status-text").textContent.includes(error));
  assert(env.element("toast").textContent.includes(error));
  assert.equal(env.element("job-status-text").innerHTML, "");
  assert.equal(env.element("toast").innerHTML, "");
  assert.equal(env.element("reply-editor").value, "原稿保持不变");
  assert.equal(env.element("generate-button").disabled, false);
});

test("missing or non-text failure reason retains the actionable fallback", async () => {
  for (const error of [undefined, "", { internal: "not a user-facing error" }]) {
    const env = harness(); ready(env); env.ui.watchJob(job());
    env.streams[0].emit(job({ revision: 2, status: "failed", error })); await settle();
    assert(env.element("job-status-text").textContent.includes("可以重新生成"));
    assert(!env.element("job-status-text").textContent.includes("[object Object]"));
  }
});

test("SSE disconnect polls only the existing task and reconnect clears fallback", async () => {
  const env = harness(); ready(env); env.ui.watchJob(job()); const source = env.streams[0];
  env.routes.set("GET /api/jobs", async () => ({ job: job({ revision: 2, preview_reply: "读取原任务快照" }) }));
  source.fail(); assert(env.element("job-status-text").textContent.includes("连接暂时中断"));
  await env.timer(0);
  assert.equal(env.ui.state.activeJob.revision, 2);
  assert.equal(env.calls.length, 1); assert.equal(env.calls[0].query.get("id"), "job-a");
  assert([...env.timers.values()].some(timer => timer.delay === 3000));
  source.open();
  assert(![...env.timers.values()].some(timer => timer.delay === 3000));
  assert.equal(env.ui.state.jobConnection, "live");
  assert(!env.calls.some(call => call.path === "/api/analyze"));
});

test("unavailable stream and runner without preview still settle via snapshots", async () => {
  const env = harness({ eventSource: false }); ready(env);
  let count = 0;
  env.routes.set("GET /api/jobs", async () => ({ job: ++count === 1 ? job({ revision: 2, preview_reply: undefined, can_cancel: false }) : job({ revision: 3, status: "interrupted", preview_reply: undefined, can_cancel: false }) }));
  env.ui.watchJob(job({ can_cancel: false }));
  assert.equal(env.element("job-cancel").disabled, true);
  await env.ui.cancelGeneration(); assert.equal(env.calls.length, 0);
  await env.timer(0);
  assert(env.element("job-preview-text").textContent.includes("正在整理内容"));
  await env.timer(3000);
  assert.equal(env.ui.state.activeJob.status, "interrupted");
  assert(env.element("job-status-text").textContent.includes("生成已中断"));
  assert.equal(env.element("generate-button").disabled, false);
  assert.equal(env.element("polish-button").disabled, false);
});

test("stop remains disabled through cancelling and only confirms a final cancellation", async () => {
  const env = harness(); ready(env); env.element("reply-editor").input("原稿必须保留"); env.ui.watchJob(job());
  const accepted = deferred();
  env.routes.set("POST /api/jobs/cancel", async () => accepted.promise);
  env.routes.set("GET /api/jobs", async () => ({ job: job({ revision: 2, status: "cancelling", can_cancel: false }) }));
  const request = env.ui.cancelGeneration();
  assert.equal(env.element("job-cancel").disabled, true);
  await env.ui.cancelGeneration(); assert.equal(env.calls.length, 1);
  assert.equal(env.calls[0].headers["X-CSRF-Token"], "offline-csrf");
  assert.deepEqual(JSON.parse(JSON.stringify(env.calls[0].body)), { id: "job-a" });
  env.streams[0].emit(job({ revision: 2, status: "cancelling", can_cancel: false }));
  assert.equal(env.element("generate-button").disabled, true);
  assert(!env.element("job-status-text").textContent.includes("已停止生成"));
  accepted.resolve({ job: job({ revision: 2, status: "cancelling", can_cancel: false }) }); await request; await settle();
  env.streams[0].emit(job({ revision: 3, status: "cancelled", can_cancel: false })); await settle();
  assert.equal(env.element("job-cancel").hidden, true);
  assert(env.element("job-status-text").textContent.includes("已停止生成"));
  assert.equal(env.element("reply-editor").value, "原稿必须保留");
  assert.equal(env.element("reply-editor").readOnly, false);
  assert.equal(env.element("generate-button").disabled, false);
});

test("a late rejected stop does not claim cancellation", async () => {
  const env = harness(); ready(env); env.ui.watchJob(job());
  env.routes.set("POST /api/jobs/cancel", async () => ({ httpError: 409, error: "late stop" }));
  env.routes.set("GET /api/jobs", async () => ({ job: job({ revision: 2, can_cancel: false, progress_message: "正在检查回复结果。" }) }));
  await env.ui.cancelGeneration(); await settle();
  assert.equal(env.ui.state.activeJob.status, "running");
  assert.equal(env.element("job-cancel").disabled, true);
  assert(!env.element("job-status-text").textContent.includes("已停止生成"));
  env.streams[0].emit(job({ revision: 3, status: "failed", can_cancel: false })); await settle();
  assert.equal(env.element("generate-button").disabled, false);
});

test("successful final result must load before copy is enabled", async () => {
  const env = harness(); ready(env); env.element("reply-editor").input("保留的人工原稿"); env.ui.watchJob(job());
  const result = deferred(); env.routes.set("GET /api/conversation", async () => result.promise);
  env.streams[0].emit(job({ revision: 2, preview_reply: "尚未检查的预览" }));
  env.streams[0].emit(job({ revision: 3, status: "succeeded", draft_id: "new-draft", can_cancel: false }));
  await settle();
  assert.equal(env.streams[0].closed, true);
  assert.equal(env.element("adopt-button").disabled, true);
  assert.equal(env.ui.state.replyEdits.get("draft-a").text, "保留的人工原稿");
  result.resolve(detail("a", [draft("new-draft", "检查后的正式回复"), draft()])); await settle();
  assert.equal(env.element("reply-editor").value, "检查后的正式回复");
  assert.equal(env.element("job-preview").hidden, true);
  assert.equal(env.element("adopt-button").disabled, false);
  assert.equal(env.ui.state.jobResultError, false);
  assert.equal(env.ui.state.jobResultLoadingId, null);
  assert.equal(env.copied.length, 0);
});

test("failed final-result refresh keeps copy blocked and exposes a read-only retry", async () => {
  const env = harness(); ready(env); env.ui.watchJob(job());
  env.routes.set("GET /api/conversations", async () => ({ httpError: 503 }));
  env.streams[0].emit(job({ revision: 2, status: "succeeded", draft_id: "new-draft", can_cancel: false })); await settle();
  assert.equal(env.ui.state.jobResultError, true);
  assert.equal(env.element("adopt-button").disabled, true);
  assert.equal(env.element("generate-button").disabled, false);
  assert.equal(env.element("job-reconnect").hidden, false);
  assert.equal(env.element("job-reconnect").textContent, "重新读取结果");
  env.routes.set("GET /api/conversations", async () => ({ items: [contact()] }));
  env.routes.set("GET /api/conversation", async () => detail("a", [draft("new-draft", "完成回复")]));
  env.ui.reconnectJob(); await settle();
  assert.equal(env.element("reply-editor").value, "完成回复");
  assert.equal(env.ui.state.jobResultError, false);
  assert(!env.calls.some(call => call.path === "/api/analyze"));
});

test("saved-result warning stays visible without failing or regenerating the reply", async () => {
  const env = harness(); ready(env); env.ui.watchJob(job());
  const warning = '回复已保存，但运行记录写入失败，请检查工作区健康。<script>bad()</script>';
  const result = deferred(); env.routes.set("GET /api/conversation", async () => result.promise);
  env.streams[0].emit(job({ revision: 2, status: "succeeded", draft_id: "new-draft", can_cancel: false, warning })); await settle();
  assert(env.element("job-status-text").textContent.includes(warning));
  assert.equal(env.element("adopt-button").disabled, true);
  result.resolve(detail("a", [draft("new-draft", "已保存的正式回复")])); await settle();
  assert.equal(env.ui.state.activeJob.status, "succeeded");
  assert.equal(env.element("job-banner").hidden, false);
  assert(env.element("job-status-text").textContent.includes(warning));
  assert(env.element("toast").textContent.includes(warning));
  assert.equal(env.element("job-status-text").innerHTML, "");
  assert.equal(env.element("toast").innerHTML, "");
  assert.equal(env.element("reply-editor").value, "已保存的正式回复");
  assert.equal(env.element("adopt-button").disabled, false);
  assert.equal(env.element("generate-button").disabled, false);
  assert(!env.calls.some(call => call.path === "/api/analyze"));
  env.routes.set("GET /api/conversation", async request => detail(request.query.get("conversation")));
  await env.ui.selectConversation(contact("b"));
  assert.equal(env.element("job-banner").hidden, true);
});

test("bootstrap reconnects the existing task without generating a new one", async () => {
  const env = harness();
  env.element("status-filter").value = "all";
  env.routes.set("GET /api/bootstrap", async () => ({ company: { id: "demo", name: "演练企业", mode: "simulation" }, capabilities: { customer_model_allowed: true }, csrf_token: "token", sync: { configured: false }, active_job: job({ revision: 4, preview_reply: "刷新前已生成的部分" }) }));
  env.routes.set("GET /api/models", async () => ({ models: [{ id: "test-model", name: "测试模型", default: true }] }));
  env.routes.set("GET /api/conversations", async () => ({ items: [contact()] }));
  env.routes.set("GET /api/conversation", async () => detail());
  await env.ui.initialize(); await settle();
  assert.equal(env.streams.length, 1);
  assert.equal(env.streams[0].url, "/api/jobs/events?id=job-a");
  assert.equal(env.element("job-preview-text").textContent, "刷新前已生成的部分");
  assert(!env.calls.some(call => call.path === "/api/analyze"));
});

test("late fallback response cannot replace a newer watched task", async () => {
  const env = harness(); ready(env); env.ui.watchJob(job());
  const oldSnapshot = deferred();
  env.routes.set("GET /api/jobs", async () => oldSnapshot.promise);
  const oldRead = env.ui.pollJob();
  env.ui.watchJob(job({ id: "job-new", revision: 1, preview_reply: "新任务预览" }));
  oldSnapshot.resolve({ job: job({ status: "interrupted", revision: 10 }) });
  await oldRead; await settle();
  assert.equal(env.ui.state.activeJob.id, "job-new");
  assert.equal(env.ui.state.activeJob.status, "running");
  assert.equal(env.element("job-preview-text").textContent, "新任务预览");
  assert.equal(env.streams[0].closed, true);
  assert.equal(env.streams[1].closed, false);
});

test("late final-result refresh cannot overwrite a new task's view", async () => {
  const env = harness(); ready(env); env.element("reply-editor").input("保留的编辑"); env.ui.watchJob(job());
  const oldList = deferred();
  env.routes.set("GET /api/conversations", async () => oldList.promise);
  env.streams[0].emit(job({ status: "succeeded", revision: 2, draft_id: "completed-old", can_cancel: false }));
  await settle();
  env.ui.watchJob(job({ id: "job-new", preview_reply: "新生成" }));
  oldList.resolve({ items: [contact()] }); await settle();
  assert.equal(env.ui.state.activeJob.id, "job-new");
  assert.equal(env.element("reply-editor").value, "保留的编辑");
  assert.equal(env.element("job-preview-text").textContent, "新生成");
  assert(!env.calls.some(call => call.path === "/api/conversation"));
});

(async () => {
  for (const [name, run] of tests) { await run(); console.log("PASS " + name); }
  console.log(`${tests.length} offline streaming UI checks passed; no real network, model, or chat reads.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
