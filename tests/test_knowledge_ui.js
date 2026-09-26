/* Real knowledge UI controller, synthetic documents, and offline HTTP boundaries. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "inquiry_product/web/index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "inquiry_product/web/app.js"), "utf8");
const entrypoint = 'bindEvents();setPane("list");initialize();';
const exposed = "globalThis.ui={state,bindEvents,openKnowledge,loadKnowledge,publishKnowledge};";
const doc = (id = "delivery") => ({ id, title: "交付资料", content: "交付时间需确认。", version: "1", valid_from: "2026-01-01", valid_until: "2027-12-31" });
const snapshot = (documents = [doc()], release = "release-one") => ({ active_release: release, history: [], config: { id: "demo", name: "演练企业", mode: "simulation", industry: "trade", rules: ["不得猜测"], required_fields: ["product"], reply_strategy: "简短回复", knowledge: documents } });
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
async function settle() { for (let i = 0; i < 30; i++) await Promise.resolve(); }
function harness(data = snapshot()) {
  const nodes = new Map(), routes = new Map(), calls = [], events = new Map();
  const selectIds = new Set([...html.matchAll(/<select\b[^>]*\bid="([^"]+)"/g)].map(match => match[1]));
  class Element {
    constructor(id) { this.id = id; this.options = []; this.value = ""; this.textContent = ""; this.hidden = false; this.open = false; this.disabled = false; this.dataset = {}; this.handlers = new Map(); this.attributes = new Map(); this.classList = { toggle() {}, add() {}, remove() {} }; this.children = []; }
    // Native single selects discard a value when there is no matching option.
    set value(value) { const text = String(value); this._value = selectIds.has(this.id) && !this.options.includes(text) ? "" : text; }
    get value() { return this._value; }
    set innerHTML(text) { this._html = text; for (const id of this.children) nodes.delete(id); this.children = []; for (const match of text.matchAll(/\bid="([^"<>]+)"/g)) { nodes.set(match[1], new Element(match[1])); this.children.push(match[1]); } if (selectIds.has(this.id)) { this.options = [...text.matchAll(/<option\b[^>]*value="([^"]*)"/g)].map(match => match[1]); this.value = this.options[0] || ""; } }
    get innerHTML() { return this._html || ""; }
    addEventListener(name, handler) { this.handlers.set(name, handler); }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
    querySelectorAll() { return []; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    focus() { context.document.activeElement = this; }
    async trigger(name, value = this.value) { this.value = value; let prevented = false; await this.handlers.get(name)?.({ target: this, preventDefault() { prevented = true; } }); return prevented; }
    input(value) { this.value = value; this.handlers.get("input")?.({ target: this }); }
  }
  for (const match of html.matchAll(/\bid="([^"<>]+)"/g)) nodes.set(match[1], new Element(match[1]));
  for (const match of html.matchAll(/<select\b[^>]*\bid="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) nodes.get(match[1]).innerHTML = match[2];
  const context = { console, Map, Set, Intl, Date, URLSearchParams, AbortController,
    document: { getElementById: id => nodes.get(id) || null, querySelectorAll: () => [], querySelector: () => new Element("grid") },
    location: { port: "offline" }, localStorage: { getItem: () => null, setItem() {} },
    addEventListener(name, handler) { events.set(name, handler); },
    setTimeout: () => 1, clearTimeout() {}, requestAnimationFrame(fn) { fn(); },
    fetch: async (url, options = {}) => { const call = { url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null }; calls.push(call); const handler = routes.get(call.method + " " + url); if (!handler) throw new Error("unexpected offline request"); const result = await handler(call); return { ok: !result.httpError, status: result.httpError || 200, json: async () => result }; },
  };
  vm.createContext(context); vm.runInContext(script.replace(entrypoint, exposed), context);
  context.ui.bindEvents(); context.ui.state.bootstrap = { csrf_token: "offline", company: { id: "demo", mode: "simulation" } };
  routes.set("GET /api/knowledge", async () => JSON.parse(JSON.stringify(data)));
  routes.set("POST /api/knowledge", async () => ({ changed: true, release_id: "release-two" }));
  routes.set("GET /api/conversations", async () => ({ items: [] }));
  nodes.get("status-filter").value = "all";
  return { ui: context.ui, calls, routes, events, element(id) { const node = nodes.get(id); assert(node, "expected visible control: " + id); return node; }, config() { return JSON.parse(nodes.get("knowledge-json").value); }, posts() { return calls.filter(call => call.method === "POST"); } };
}
const tests = []; function test(name, run) { tests.push([name, run]); }
test("empty company can add its first document without JSON and send untouched company fields", async () => {
  const env = harness(snapshot([])); await env.ui.openKnowledge();
  assert.equal(env.element("knowledge-empty").hidden, false);
  await env.element("knowledge-add").trigger("click");
  assert.equal(env.element("knowledge-fields").hidden, false);
  assert.equal(env.element("knowledge-version").value, "1");
  assert(env.config().knowledge[0].id);
  env.element("knowledge-title").input("包装要求"); env.element("knowledge-content-editor").input("木箱尺寸须确认");
  env.element("knowledge-from").input("2026-09-01"); env.element("knowledge-until").input("2026-12-31");
  await env.element("knowledge-form").trigger("submit");
  assert.equal(env.posts().length, 1); const sent = env.posts()[0].body;
  assert.equal(sent.base_release, "release-one"); assert.equal(sent.config.reply_strategy, "简短回复");
  assert.deepEqual(sent.config.rules, ["不得猜测"]); assert.equal(sent.config.knowledge[0].title, "包装要求");
  assert.equal(env.element("knowledge-dialog").open, false);
});
test("switching or removing a new document preserves changes to the other document", async () => {
  const env = harness(); await env.ui.openKnowledge(); env.element("knowledge-content-editor").input("已修改的交付要求");
  await env.element("knowledge-add").trigger("click"); const newId = env.config().knowledge[1].id;
  assert.notEqual(newId, "delivery"); env.element("knowledge-title").input("未发布资料");
  await env.element("knowledge-document").trigger("change", "0"); assert.equal(env.element("knowledge-content-editor").value, "已修改的交付要求");
  await env.element("knowledge-document").trigger("change", "1"); assert.equal(env.element("knowledge-title").value, "未发布资料");
  await env.element("knowledge-remove").trigger("click"); assert.equal(env.config().knowledge.length, 2);
  await env.element("knowledge-confirm-accept").trigger("click");
  assert.equal(env.config().knowledge.length, 1); assert.equal(env.config().knowledge[0].content, "已修改的交付要求"); assert.equal(env.posts().length, 0);
});
test("removing last published document needs confirmation and only changes the publication payload", async () => {
  const env = harness(); await env.ui.openKnowledge(); await env.element("knowledge-remove").trigger("click");
  await env.element("knowledge-confirm-keep").trigger("click"); assert.equal(env.config().knowledge.length, 1);
  await env.element("knowledge-remove").trigger("click"); await env.element("knowledge-confirm-accept").trigger("click");
  assert.equal(env.element("knowledge-empty").hidden, false); assert.equal(env.posts().length, 0);
  await env.element("knowledge-form").trigger("submit"); assert.deepEqual(env.posts()[0].body.config.knowledge, []);
});
for (const [field, value] of [["knowledge-title", "  "], ["knowledge-content-editor", ""], ["knowledge-from", "2027-02-30"], ["knowledge-until", "2025-12-31"], ["knowledge-version", ""]]) {
  test("invalid " + field + " blocks publication and keeps entered text", async () => {
    const env = harness(); await env.ui.openKnowledge(); env.element(field).input(value); await env.element("knowledge-form").trigger("submit");
    assert.equal(env.posts().length, 0); assert.equal(env.element("knowledge-dialog").open, true);
    assert.equal(env.element(field).value, value); assert.equal(env.element("knowledge-error").hidden, false);
  });
}
test("close buttons and Escape preserve edits until discard is confirmed", async () => {
  const env = harness(); await env.ui.openKnowledge(); env.element("knowledge-title").input("未保存");
  for (const id of ["knowledge-close", "knowledge-cancel"]) { await env.element(id).trigger("click"); assert.equal(env.element("knowledge-dialog").open, true); assert.equal(env.element("knowledge-confirm").hidden, false); await env.element("knowledge-confirm-keep").trigger("click"); }
  assert.equal(await env.element("knowledge-dialog").trigger("cancel"), true); assert.equal(env.element("knowledge-dialog").open, true);
  await env.element("knowledge-confirm-accept").trigger("click"); assert.equal(env.element("knowledge-dialog").open, false); assert.equal(env.posts().length, 0);
});
test("an empty new document is dirty and page navigation warns only while edits exist", async () => {
  const env = harness(snapshot([])); await env.ui.openKnowledge(); assert(env.events.has("beforeunload"));
  const event = { prevented: false, preventDefault() { this.prevented = true; } };
  env.events.get("beforeunload")(event); assert.equal(event.prevented, false);
  await env.element("knowledge-add").trigger("click"); env.events.get("beforeunload")(event); assert.equal(event.prevented, true);
  await env.element("knowledge-close").trigger("click"); await env.element("knowledge-confirm-accept").trigger("click");
  event.prevented = false; env.events.get("beforeunload")(event); assert.equal(event.prevented, false);
});
test("failed save preserves content for retry and blocks duplicate actions while pending", async () => {
  const env = harness(); await env.ui.openKnowledge(); env.element("knowledge-title").input("保留这份内容");
  const pending = deferred(); env.routes.set("POST /api/knowledge", async () => pending.promise);
  const request = env.element("knowledge-form").trigger("submit"); await settle();
  assert.equal(env.element("knowledge-add").disabled, true); assert.equal(env.element("knowledge-title").disabled, true);
  await env.element("knowledge-form").trigger("submit"); await env.element("knowledge-add").trigger("click"); await env.element("knowledge-close").trigger("click");
  assert.equal(env.posts().length, 1); assert.equal(env.config().knowledge.length, 1); assert.equal(env.element("knowledge-dialog").open, true);
  pending.resolve({ httpError: 503, error: "暂时不能保存" }); await request;
  assert.equal(env.element("knowledge-title").value, "保留这份内容"); assert.equal(env.element("knowledge-title").disabled, false); assert.equal(env.element("knowledge-submit").disabled, false);
});
test("conflict keeps the original base release and only reloads after discard confirmation", async () => {
  const env = harness(); await env.ui.openKnowledge(); env.element("knowledge-title").input("本地修改");
  env.routes.set("POST /api/knowledge", async () => ({ httpError: 400, error: "企业资料已在其他页面更新，请重新载入后保存" }));
  await env.element("knowledge-form").trigger("submit"); assert.equal(env.element("knowledge-title").value, "本地修改");
  assert.equal(env.element("knowledge-reload").hidden, false); assert.equal(env.element("knowledge-submit").disabled, true);
  env.routes.set("GET /api/knowledge", async () => snapshot([{ ...doc(), title: "另一页面的更新" }], "release-two"));
  const calls = env.calls.length; await env.element("knowledge-reload").trigger("click"); assert.equal(env.calls.length, calls);
  await env.element("knowledge-confirm-keep").trigger("click"); assert.equal(env.element("knowledge-title").value, "本地修改");
  await env.element("knowledge-reload").trigger("click"); await env.element("knowledge-confirm-accept").trigger("click"); await settle();
  assert.equal(env.element("knowledge-title").value, "另一页面的更新"); env.element("knowledge-title").input("合并后内容");
  env.routes.set("POST /api/knowledge", async () => ({ changed: true })); await env.element("knowledge-form").trigger("submit");
  assert.equal(env.posts()[1].body.base_release, "release-two");
});
test("refreshing the knowledge page cannot silently replace an open editor's base release", async () => {
  const env = harness(); await env.ui.openKnowledge(); env.element("knowledge-title").input("旧页面修改");
  env.routes.set("GET /api/knowledge", async () => snapshot([doc()], "release-two")); await env.ui.loadKnowledge();
  await env.element("knowledge-form").trigger("submit"); assert.equal(env.posts()[0].body.base_release, "release-one");
});
test("advanced JSON invalid input is preserved and cannot be replaced by ordinary edits", async () => {
  const env = harness(); await env.ui.openKnowledge(); env.element("knowledge-json").input("{broken");
  await env.element("knowledge-add").trigger("click"); await env.element("knowledge-form").trigger("submit");
  assert.equal(env.element("knowledge-json").value, "{broken"); assert.equal(env.posts().length, 0); assert.equal(env.element("knowledge-fields").hidden, true);
});
test("document titles and bodies are escaped in the page and selector", async () => {
  const malicious = { ...doc(), title: '<img src=x onerror="bad()">', content: "<script>bad()</script>" };
  const env = harness(snapshot([malicious])); env.ui.state.page = "knowledge"; await env.ui.loadKnowledge(); await env.ui.openKnowledge();
  assert(!env.element("knowledge-content").innerHTML.includes("<script>")); assert(!env.element("knowledge-document").innerHTML.includes("<img"));
  assert.equal(env.element("knowledge-title").value, malicious.title);
});
test("empty page provides a direct add action", async () => {
  const env = harness(snapshot([])); env.ui.state.page = "knowledge"; await env.ui.loadKnowledge();
  await env.element("knowledge-empty-add").trigger("click");
  assert.equal(env.element("knowledge-dialog").open, true); assert.equal(env.config().knowledge.length, 1);
});
test("card edit still selects the intended document when another page reordered documents", async () => {
  const env = harness(snapshot([doc("first"), { ...doc("second"), title: "第二份" }])); env.ui.state.page = "knowledge"; await env.ui.loadKnowledge();
  env.routes.set("GET /api/knowledge", async () => snapshot([{ ...doc("second"), title: "第二份新版" }, doc("first")], "release-two"));
  await env.element("knowledge-edit-1").trigger("click");
  assert.equal(env.element("knowledge-title").value, "第二份新版");
});
test("first opening the second card edits that document rather than the first one", async () => {
  const env = harness(snapshot([doc("first"), { ...doc("second"), title: "第二份" }])); env.ui.state.page = "knowledge"; await env.ui.loadKnowledge();
  await env.element("knowledge-edit-1").trigger("click");
  assert.equal(env.element("knowledge-document").value, "1"); assert.equal(env.element("knowledge-title").value, "第二份");
  env.element("knowledge-title").input("更新第二份");
  assert.equal(env.config().knowledge[0].title, "交付资料"); assert.equal(env.config().knowledge[1].title, "更新第二份");
});
test("adding after an existing document selects the new blank entry and cannot overwrite the old one", async () => {
  const env = harness(); await env.ui.openKnowledge(); await env.element("knowledge-add").trigger("click");
  assert.equal(env.element("knowledge-document").value, "1"); assert.equal(env.element("knowledge-title").value, "");
  env.element("knowledge-title").input("新的产品要求");
  assert.equal(env.config().knowledge[0].title, "交付资料"); assert.equal(env.config().knowledge[1].title, "新的产品要求");
  await env.element("knowledge-add").trigger("click");
  assert.equal(env.element("knowledge-document").value, "2"); assert.equal(env.element("knowledge-title").value, "");
  assert.equal(env.config().knowledge[1].title, "新的产品要求");
});
test("double open waits for one request and a failed open can be retried", async () => {
  const env = harness(); const pending = deferred(); env.routes.set("GET /api/knowledge", async () => pending.promise);
  const request = env.ui.openKnowledge(); await env.ui.openKnowledge(); assert.equal(env.calls.length, 1);
  assert.equal(env.element("knowledge-add-page").disabled, true); pending.resolve({ httpError: 503, error: "未连接" }); await request;
  assert.equal(env.element("knowledge-dialog").open, false); assert.equal(env.element("knowledge-add-page").disabled, false);
  env.routes.set("GET /api/knowledge", async () => snapshot()); await env.ui.openKnowledge(); assert.equal(env.element("knowledge-dialog").open, true);
});
test("failed reload preserves edits and keeps the conflicting save blocked", async () => {
  const env = harness(); await env.ui.openKnowledge(); env.element("knowledge-title").input("保留的草稿");
  env.routes.set("POST /api/knowledge", async () => ({httpError:409,error:"版本冲突"})); await env.element("knowledge-form").trigger("submit");
  env.routes.set("GET /api/knowledge", async () => ({httpError:503,error:"未连接"}));
  await env.element("knowledge-reload").trigger("click"); await env.element("knowledge-confirm-accept").trigger("click");
  assert.equal(env.element("knowledge-title").value, "保留的草稿"); assert.equal(env.element("knowledge-submit").disabled, true);
});
(async () => { let failed = 0; for (const [name, run] of tests) { try { await run(); console.log("PASS " + name); } catch (error) { failed++; console.error("FAIL " + name + "\n" + error.stack); } } console.log(`${tests.length - failed}/${tests.length} knowledge UI checks passed`); if (failed) process.exitCode = 1; })();
