/* Sales/manager separation using the real UI controller and offline API fixtures. */
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
const exposed = "globalThis.ui={state,bindEvents,initialize,renderReply,renderJob,watchJob,requestAnalysis,loadModels,loadActivity};";

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
  const storage = new Map();
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
    focus() { context.document.activeElement = this; } select() {} scrollIntoView() {} close() { this.open = false; } showModal() { this.open = true; }
    async trigger(name, value = this.value) { this.value = value; return this.handlers.get(name)?.({target:this, preventDefault(){}}); }
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
    location: { port: "8765" }, localStorage: { getItem: key => storage.get(key) || null, setItem(key, value) { storage.set(key,value); } },
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
  const env = { ui, calls, storage, focused: () => context.document.activeElement?.id, streams, routes, copied, timers, element: id => elements.get(id), async timer(delay) {
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
const catalog = { default_model: "gpt-5.6-luna", models: [{ id: "gpt-5.6-luna", name: "Luna" }, { id: "gpt-6-sol", name: "Sol" }] };
const storageKey = 'inquiry-assistant:model:["demo","simulation","8765"]';

test("manager settings open from More without generating or reconnecting services", async () => {
  const env = harness(); ready(env);
  await env.element("more-open").trigger("click");
  assert.equal(env.element("more-dialog").open, true);
  assert(env.element("manager-settings-open"), "More must offer manager settings");
  await env.element("manager-settings-open").trigger("click");
  assert.equal(env.element("more-dialog").open, false);
  assert.equal(env.element("manager-settings-dialog").open, true);
  assert.equal(env.calls.length, 0);
});

test("closing manager settings restores focus to the visible entry point", async () => {
  const env = harness(); ready(env);
  assert(env.element("manager-settings-open"), "manager entry must exist");
  await env.element("manager-settings-open").trigger("click");
  await env.element("manager-settings-dialog").trigger("close");
  assert.equal(env.focused(), "more-open");
  env.ui.state.modelId = ""; env.ui.renderJob();
  await env.element("ai-settings-open").trigger("click");
  await env.element("manager-settings-dialog").trigger("close");
  assert.equal(env.focused(), "ai-settings-open");
  await env.element("ai-settings-open").trigger("click");
  env.ui.state.modelId = "test-model"; env.ui.renderJob();
  await env.element("manager-settings-dialog").trigger("close");
  assert.equal(env.focused(), "more-open");
});

test("unavailable AI has a visible business explanation and recovery controls", async () => {
  const env = harness(); ready(env);
  env.routes.set("GET /api/models", async () => ({httpError: 503, error: "AI 服务暂不可用，请联系负责人。"}));
  await env.ui.loadModels();
  assert(env.element("ai-service-status"), "daily work needs service status");
  assert.equal(env.element("ai-service-status").hidden, false);
  assert.match(env.element("ai-service-status-text").textContent, /AI.*负责人/);
  assert.equal(env.element("generate-button").disabled, true);
  assert.equal(env.element("ai-service-retry").disabled, false);
  await env.element("ai-settings-open").trigger("click");
  assert.equal(env.element("manager-settings-dialog").open, true);
  env.routes.set("GET /api/models", async () => catalog);
  await env.element("ai-service-retry").trigger("click");
  assert.equal(env.element("generate-button").disabled, false);
  assert.equal(env.element("ai-service-status").hidden, true);
  assert.equal(env.calls.filter(call => call.path === "/api/analyze").length, 0);
});

test("new browser defaults to Luna and configured choice remains explicit in requests", async () => {
  const env = harness(); ready(env);
  env.routes.set("GET /api/models", async () => catalog);
  await env.ui.loadModels();
  assert.equal(env.ui.state.modelId, "gpt-5.6-luna");
  await env.element("reply-model").trigger("change", "gpt-6-sol");
  assert.equal(env.storage.get(storageKey), "gpt-6-sol");
  env.routes.set("POST /api/analyze", async () => ({ job: job() }));
  env.ui.requestAnalysis("generate"); await settle();
  assert.equal(env.calls.find(call => call.path === "/api/analyze").body.request.model, "gpt-6-sol");
  assert.equal(env.element("reply-model").disabled, true);
  await env.element("reply-model").trigger("change", "gpt-5.6-luna");
  assert.equal(env.ui.state.modelId, "gpt-6-sol");
});

test("a removed saved model never silently falls back to another model", async () => {
  const env = harness(); ready(env);
  env.storage.set(storageKey, "removed-model");
  env.routes.set("GET /api/models", async () => catalog);
  await env.ui.loadModels();
  assert.equal(env.ui.state.modelId, "");
  assert.equal(env.element("generate-button").disabled, true);
  assert.equal(env.element("ai-service-status").hidden, false);
  env.ui.requestAnalysis("generate"); await settle();
  assert.equal(env.calls.filter(call => call.path === "/api/analyze").length, 0);
  await env.element("reply-model").trigger("change", "gpt-5.6-luna");
  assert.equal(env.element("generate-button").disabled, false);
});

test("missing configured default requires a choice instead of substituting catalog default", async () => {
  const env = harness(); ready(env);
  env.routes.set("GET /api/models", async () => ({default_model:"gpt-5.6-luna",models:[{id:"gpt-6-sol",name:"Sol",default:true}]}));
  await env.ui.loadModels();
  assert.equal(env.ui.state.modelId, "");
  assert.equal(env.element("generate-button").disabled, true);
});

test("daily draft hides model identity while manager run history retains it", async () => {
  const env = harness(); ready(env);
  assert(!env.element("reply-body").innerHTML.includes("test-model"), "reply composer must omit technical model identity");
  env.ui.state.page = "activity";
  env.routes.set("GET /api/sync", async () => ({configured:true,source_type:"demo"}));
  env.routes.set("GET /api/activity", async () => ({health:{ok:true},runs:[{started_at:"2026-09-25T00:00:00Z",runner:"codex_app_server",requested_model:"test-model",status:"succeeded",duration_ms:1200,input_messages:1}]}));
  await env.ui.loadActivity();
  assert(env.element("activity-content").innerHTML.includes("test-model"));
  assert(env.element("activity-content").innerHTML.includes("codex_app_server"));
});

test("customer data is gated by cloud processing consent before any generation", async () => {
  const env = harness(); ready(env);
  env.ui.state.bootstrap.company.mode = "customer";
  env.ui.state.bootstrap.capabilities.customer_model_allowed = false;
  env.ui.requestAnalysis("generate"); await settle();
  assert.equal(env.element("customer-model-dialog").open, true);
  assert.equal(env.calls.length, 0);
  env.routes.set("POST /api/analyze", async () => ({ job: job() }));
  await env.element("customer-model-confirm").trigger("click"); await settle();
  const sent = env.calls.find(call => call.path === "/api/analyze");
  assert.equal(sent.body.allow_customer_model, true);
  assert.equal(sent.body.request.model, "test-model");
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log("PASS " + name); }
    catch (error) { failed++; console.error("FAIL " + name + "\n" + error.stack); }
  }
  console.log(`${tests.length - failed}/${tests.length} sales UI checks passed`);
  if (failed) process.exitCode = 1;
})();
