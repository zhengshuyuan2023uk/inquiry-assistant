/* Customer-selection behavior with synthetic metadata only; no network or model calls. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "inquiry_product/web/index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "inquiry_product/web/app.js"), "utf8");
const entrypoint = 'bindEvents();setPane("list");initialize();';
const exposed = "globalThis.ui={state,bindEvents,openCustomers,scanCustomers,saveCustomers,renderCustomers,toggleCustomer,requestCustomerRescan,closeCustomers,resolveCustomerDiscard,filteredCustomers,renderChat,renderReply,updateChats,refreshConversations,selectConversation};";
function deferred() { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; }
function harness() {
  const elements = new Map(), calls = [], routes = new Map();
  class Element {
    constructor(id) {
      this.id = id; this.value = ""; this.textContent = ""; this._html = ""; this.children = []; this.hidden = false; this.disabled = false;
      this.scrollTop = 0; this.scrollHeight = 100;
      this.open = false; this.dataset = {}; this.handlers = new Map(); this.attributes = new Map();
      this.classList = { toggle() {}, add() {}, remove() {} };
    }
    set innerHTML(value) {
      this._html = value;
      for (const id of this.children) elements.delete(id);
      this.children = [];
      for (const match of value.matchAll(/\bid="([^"<>]+)"/g)) {
        elements.set(match[1], new Element(match[1])); this.children.push(match[1]);
      }
      for (const match of value.matchAll(/<textarea\b[^>]*id="([^"<>]+)"[^>]*>([\s\S]*?)<\/textarea>/g)) elements.get(match[1]).value = match[2];
    }
    get innerHTML() { return this._html; }
    addEventListener(name, fn) { this.handlers.set(name, fn); }
    setAttribute(name, value) { this.attributes.set(name, value); }
    querySelectorAll() { return []; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    focus() {}
  }
  for (const match of html.matchAll(/\bid="([^"<>]+)"/g)) elements.set(match[1], new Element(match[1]));
  const context = {
    console, Map, Set, Intl, Date, URLSearchParams, AbortController,
    document: { getElementById: id => elements.get(id) || null, querySelectorAll: () => [], querySelector: () => new Element("grid") },
    location: { port: "offline" }, localStorage: { getItem: () => null, setItem() {} },
    setTimeout: () => 1, clearTimeout() {}, requestAnimationFrame(fn) { fn(); },
    fetch: async (url, options = {}) => {
      const method = options.method || "GET", request = { url, method, body: options.body ? JSON.parse(options.body) : null };
      calls.push(request); const handler = routes.get(method + " " + url);
      if (!handler) throw new Error("unexpected offline request");
      const result = await handler(request);
      return { ok: !result.httpError, status: result.httpError || 200, json: async () => result };
    },
  };
  vm.createContext(context); vm.runInContext(script.replace(entrypoint, exposed), context);
  context.ui.bindEvents();
  context.ui.state.bootstrap = { csrf_token: "offline", company: { mode: "simulation" } };
  context.ui.state.sync = { configured: true, source_type: "demo" };
  routes.set("GET /api/sync", async () => ({ configured: true, source_type: "demo" }));
  routes.set("GET /api/conversations", async () => ({ items: [] }));
  elements.get("status-filter").value = "all";
  return { ui: context.ui, calls, routes, element: id => elements.get(id) };
}
function snapshot(overrides = {}) {
  return { configured: true, source_type: "demo", source_label: "虚构演练", connection_state: "unknown", scan_id: "scan-one", scanned_at: "2026-09-25T02:00:00Z", max_selected: 100,
    items: [
      { id: "active", display_name: "Alice", identifier: "+1001", status: "active", kind: "individual", selected: true, available: true },
      { id: "new", display_name: "Bob", identifier: "+1002", status: "new", kind: "individual", selected: false, available: true },
      { id: "paused", display_name: "Team", identifier: "采购群", status: "paused", kind: "group", selected: false, available: true },
    ], selected_count: 1, managed_count: 2, ...overrides };
}
async function open(env, value = snapshot()) { env.routes.set("POST /api/customers/scan", async () => value); await env.ui.openCustomers(); }
const tests = [];
function test(name, run) { tests.push([name, run]); }

test("every open performs a fresh scan and leaves new customers unselected", async () => {
  const env = harness(); await open(env);
  assert.equal(env.calls.filter(call => call.url === "/api/customers/scan").length, 1);
  assert.deepEqual([...env.ui.state.customers.selected], ["active"]);
  assert(env.element("customer-source").textContent.includes("虚构"));
  env.ui.closeCustomers(); await env.ui.openCustomers();
  assert.equal(env.calls.filter(call => call.url === "/api/customers/scan").length, 2);
});
test("search and status filters preserve selections outside the visible rows", async () => {
  const env = harness(); await open(env); env.ui.toggleCustomer("new", true);
  env.element("customer-search").value = "team"; env.element("customer-filter").value = "paused"; env.ui.renderCustomers();
  assert.deepEqual([...env.ui.filteredCustomers()].map(item => item.id), ["paused"]);
  assert.deepEqual([...env.ui.state.customers.selected].sort(), ["active", "new"]);
  env.element("customer-search").value = ""; env.element("customer-filter").value = "all";
  env.ui.renderCustomers(); assert.equal(env.ui.filteredCustomers().length, 3);
});
test("dirty rescan requires an explicit discard and keep leaves edits intact", async () => {
  const env = harness(); await open(env); env.ui.toggleCustomer("new", true);
  env.ui.requestCustomerRescan();
  assert.equal(env.element("customer-discard").hidden, false);
  assert.equal(env.calls.filter(call => call.method === "POST").length, 1);
  env.ui.resolveCustomerDiscard(false);
  assert(env.ui.state.customers.selected.has("new"));
  env.ui.requestCustomerRescan(); await env.ui.resolveCustomerDiscard(true);
  assert.equal(env.calls.filter(call => call.url === "/api/customers/scan").length, 2);
  assert.deepEqual([...env.ui.state.customers.selected], ["active"]);
});
test("dirty close protects selection until explicitly discarded", async () => {
  const env = harness(); await open(env); env.ui.toggleCustomer("active", false); env.ui.closeCustomers();
  assert.equal(env.element("customer-dialog").open, true);
  assert.equal(env.element("customer-discard").hidden, false);
  await env.ui.resolveCustomerDiscard(true); assert.equal(env.element("customer-dialog").open, false);
});
test("pending scan disables duplicate scans, save, and row changes", async () => {
  const env = harness(); const pending = deferred(); env.routes.set("POST /api/customers/scan", async () => pending.promise);
  const request = env.ui.openCustomers();
  assert.equal(env.element("customer-rescan").disabled, true); assert.equal(env.element("customer-save").disabled, true);
  await env.ui.scanCustomers(); await env.ui.saveCustomers(); env.ui.toggleCustomer("new", true);
  assert.equal(env.calls.length, 1); pending.resolve(snapshot()); await request;
  assert.equal(env.element("customer-rescan").disabled, false);
});
test("failed rescan does not show or save an old successful list", async () => {
  const env = harness(); await open(env);
  env.routes.set("POST /api/customers/scan", async () => ({ httpError: 503, error: "聊天来源暂时不可用，请重试。" }));
  await env.ui.scanCustomers();
  assert(env.element("customer-error").textContent.includes("暂时不可用"));
  assert(!env.element("customer-list").innerHTML.includes("Alice"));
  assert.equal(env.element("customer-save").disabled, true);
  await env.ui.saveCustomers(); assert.equal(env.calls.filter(call => call.url === "/api/customers/selection").length, 0);
});
test("selection limits still allow unselecting and unavailable new rows cannot be chosen", async () => {
  const env = harness(); const data = snapshot({ max_selected: 1 }); data.items[2].available = false; await open(env, data);
  env.ui.toggleCustomer("new", true); assert.deepEqual([...env.ui.state.customers.selected], ["active"]);
  env.ui.toggleCustomer("active", false); env.ui.toggleCustomer("paused", true);
  assert.equal(env.ui.state.customers.selected.size, 0);
  env.ui.toggleCustomer("new", true); assert.deepEqual([...env.ui.state.customers.selected], ["new"]);
});
test("save submits the complete cross-filter selection and never automatically updates chats", async () => {
  const env = harness(); await open(env); env.ui.toggleCustomer("new", true); env.ui.toggleCustomer("paused", true);
  env.element("customer-filter").value = "new"; env.ui.renderCustomers();
  const saved = snapshot({ scan_id: null }); for (const item of saved.items) { item.selected = true; item.status = "active"; }
  env.routes.set("POST /api/customers/selection", async () => saved); await env.ui.saveCustomers();
  const call = env.calls.find(item => item.url === "/api/customers/selection");
  assert.deepEqual(call.body, { scan_id: "scan-one", selected_ids: ["active", "new", "paused"] });
  assert.equal(env.calls.filter(item => item.method === "POST" && item.url === "/api/sync").length, 0);
  assert.equal(env.element("customer-update").hidden, false);
  assert(env.element("customer-result").textContent.includes("尚未更新聊天"));
  assert.equal(env.element("customer-save").disabled, true);
  env.ui.toggleCustomer("active", false); assert(env.ui.state.customers.selected.has("active"));
  assert(env.element("customer-change-count").textContent.includes("重新扫描"));
});
test("zero selected is a valid pause-all save and history-retention message is visible", async () => {
  const env = harness(); await open(env); env.ui.toggleCustomer("active", false);
  assert.equal(env.element("customer-save").disabled, false);
  const saved = snapshot({ selected_count: 0 }); saved.items[0].selected = false; saved.items[0].status = "paused";
  env.routes.set("POST /api/customers/selection", async () => saved); await env.ui.saveCustomers();
  assert.deepEqual(env.calls.find(item => item.url === "/api/customers/selection").body.selected_ids, []);
  assert.equal(env.element("customer-update").hidden, true);
  assert(env.element("customer-result").textContent.includes("历史"));
});
test("save conflict blocks stale choices until a fresh scan", async () => {
  const env = harness(); await open(env); env.ui.toggleCustomer("new", true);
  env.routes.set("POST /api/customers/selection", async () => ({ httpError: 409, error: "客户选择已变化，请重新扫描。" }));
  await env.ui.saveCustomers();
  assert.equal(env.element("customer-save").disabled, true);
  assert(env.element("customer-error").textContent.includes("重新扫描"));
  await env.ui.saveCustomers(); assert.equal(env.calls.filter(item => item.url === "/api/customers/selection").length, 1);
});
test("pending save suppresses duplicate submission and escape closing", async () => {
  const env = harness(); await open(env); env.ui.toggleCustomer("new", true); const pending = deferred();
  env.routes.set("POST /api/customers/selection", async () => pending.promise);
  const request = env.ui.saveCustomers(); await env.ui.saveCustomers(); env.ui.closeCustomers();
  assert.equal(env.element("customer-dialog").open, true);
  assert.equal(env.element("customer-rescan").disabled, true);
  assert.equal(env.calls.filter(item => item.url === "/api/customers/selection").length, 1);
  pending.resolve(snapshot()); await request;
});
test("unconfigured or empty sources stay actionable without false online claims", async () => {
  const env = harness(); await open(env, snapshot({ configured: false, items: [], scan_id: null, source_type: null }));
  assert.equal(env.element("customer-save").disabled, true);
  assert(env.element("customer-list").innerHTML.includes("聊天来源"));
  env.ui.closeCustomers(); await open(env, snapshot({ source_type: "whatsapp_bridge", source_label: "WhatsApp", items: [], selected_count: 0 }));
  assert(env.element("customer-source").textContent.includes("在线状态未确认"));
  assert(env.element("customer-list").innerHTML.includes("没有可添加"));
});
test("untrusted customer labels render escaped", async () => {
  const env = harness(); const data = snapshot(); data.items[0].display_name = '<img src=x onerror="boom">';
  await open(env, data); assert(!env.element("customer-list").innerHTML.includes('<img'));
  assert(env.element("customer-list").innerHTML.includes("&lt;img"));
});
test("customer-facing labels hide technical WhatsApp and demo identifiers", async () => {
  const env = harness(); const data = snapshot({ source_type: "whatsapp_bridge" });
  data.items[0].identifier = "1234567@s.whatsapp.net"; data.items[0].display_name = data.items[0].identifier;
  data.items[2].identifier = "12345@g.us";
  await open(env, data);
  assert(env.element("customer-list").innerHTML.includes("+1234567"));
  assert(!env.element("customer-list").innerHTML.includes("@s.whatsapp.net"));
  assert(!env.element("customer-list").innerHTML.includes("@g.us"));
});
test("unavailable active customer can undo a local pause before saving", async () => {
  const env = harness(); const data = snapshot(); data.items[0].available = false;
  await open(env, data); env.ui.toggleCustomer("active", false); env.ui.toggleCustomer("active", true);
  assert(env.ui.state.customers.selected.has("active"));
});

function activeConversation(env, id = "active", title = "+1001") {
  const conversation = { account_id: "synthetic", conversation_id: id, title, subtitle: "WhatsApp", status: "needs_analysis", message_count: 1, preview: "Synthetic inquiry" };
  env.ui.state.selected = conversation;
  env.ui.state.conversations = [conversation];
  env.ui.state.detail = { conversation: { ...conversation }, messages: [{ message_id: "synthetic-1", body: "Synthetic inquiry", direction: "inbound", sent_at: "2026-09-25T02:00:00Z" }], drafts: [], required_fields: [] };
  env.ui.renderChat(); env.ui.renderReply();
  env.element("reply-editor").value = "请保留我尚未完成的回复";
  env.element("reply-editor").handlers.get("input")({ target: env.element("reply-editor") });
  env.element("message-list").scrollTop = 23;
  return conversation;
}
test("scan refreshes list and open-chat names without saving selection or replacing a manual reply", async () => {
  const env = harness(), original = activeConversation(env), editor = env.element("reply-editor");
  const fresh = { ...original, title: "迪拜王总" };
  env.routes.set("GET /api/conversations", async () => ({ items: [fresh] }));
  const data = snapshot({ source_type: "whatsapp_bridge" }); data.items[0].display_name = "迪拜王总";
  await open(env, data);
  assert(env.element("conversation-list").innerHTML.includes("迪拜王总"));
  assert.equal(env.element("chat-title").textContent, "迪拜王总");
  assert.equal(env.ui.state.detail.conversation.title, "迪拜王总");
  assert(env.element("message-list").innerHTML.includes("迪拜王总"));
  assert.equal(env.element("message-list").scrollTop, 23);
  assert.equal(env.ui.state.selected.conversation_id, "active");
  assert.strictEqual(env.element("reply-editor"), editor);
  assert.equal(editor.value, "请保留我尚未完成的回复");
  assert.deepEqual(env.calls.filter(call => call.method === "POST").map(call => call.url), ["/api/customers/scan"]);
  assert.equal(env.calls.filter(call => call.url.startsWith("/api/conversation?")).length, 0);
});
test("name-only chat update refreshes the heading and preserves unsaved customer choices and reply", async () => {
  const env = harness(); await open(env); env.ui.toggleCustomer("new", true);
  const original = activeConversation(env), editor = env.element("reply-editor");
  env.routes.set("GET /api/conversations", async () => ({ items: [{ ...original, title: "巴黎李总" }] }));
  env.routes.set("POST /api/sync", async () => ({ status: "succeeded", inserted: 0, affected_conversations: [], conflicts: [], finished_at: "2026-09-25T02:00:00Z" }));
  await env.ui.updateChats();
  assert.equal(env.element("chat-title").textContent, "巴黎李总");
  assert.deepEqual([...env.ui.state.customers.selected].sort(), ["active", "new"]);
  assert.strictEqual(env.element("reply-editor"), editor);
  assert.equal(editor.value, "请保留我尚未完成的回复");
  assert.equal(env.calls.filter(call => call.url.startsWith("/api/conversation?") || call.url === "/api/analyze").length, 0);
});
test("scan keeps usable choices when only the main-list refresh fails", async () => {
  const env = harness();
  env.routes.set("GET /api/conversations", async () => ({ httpError: 503, error: "临时读取失败" }));
  await open(env);
  assert.equal(env.ui.state.customers.valid, true);
  env.ui.toggleCustomer("new", true);
  assert.equal(env.element("customer-save").disabled, false);
  assert(env.element("toast").textContent.includes("刷新"));
  assert.equal(env.element("customer-error").hidden, true);
});

test("an older in-flight chat response cannot roll back a name refreshed by a scan", async () => {
  const env = harness(), original = activeConversation(env), pending = deferred();
  const detail = env.ui.state.detail;
  env.routes.set("GET /api/conversation?account=synthetic&conversation=active", async () => pending.promise);
  const reading = env.ui.selectConversation(original);
  env.routes.set("GET /api/conversations", async () => ({ items: [{ ...original, title: "扫描得到的新名字" }] }));
  await open(env);
  assert.equal(env.element("chat-title").textContent, "扫描得到的新名字");
  pending.resolve(detail); await reading;
  assert.equal(env.element("chat-title").textContent, "扫描得到的新名字");
  assert.equal(env.ui.state.detail.conversation.title, "扫描得到的新名字");
  assert.equal(env.element("reply-editor").value, "请保留我尚未完成的回复");
});

(async () => {
  for (const [name, run] of tests) { await run(); console.log("PASS " + name); }
  console.log(`${tests.length} offline customer UI checks passed.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
