/* Reply settings behavior through the real UI controller; synthetic offline APIs only. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "inquiry_product/web/index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "inquiry_product/web/app.js"), "utf8");
const entrypoint = 'bindEvents();setPane("list");initialize();';
const exposed = "globalThis.ui={state,bindEvents,openKnowledge,loadKnowledge,publishKnowledge,setPage,loadStrategy,saveStrategy};";
const doc = (id = "delivery") => ({ id, title: "交付资料", content: "交付时间需确认。", version: "1", valid_from: "2026-01-01", valid_until: "2027-12-31" });
const snapshot = (documents = [doc()], release = "release-one") => ({ active_release: release, history: [], config: { id: "demo", name: "演练企业", mode: "simulation", industry: "trade", rules: ["不得猜测"], required_fields: ["product"], reply_strategy: "简短回复", knowledge: documents } });
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
async function settle() { for (let i = 0; i < 30; i++) await Promise.resolve(); }
function harness(settings = {text:"简短友好",required_fields:["product","custom_port"],rules:["报价须由负责人确认"],protected_rule_count:4,active_release:"settings-one"}) {
  const data = snapshot();
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
  routes.set("GET /api/reply-settings", async () => JSON.parse(JSON.stringify(settings)));
  routes.set("POST /api/reply-settings", async () => ({changed:true,release_id:"settings-two"}));
  routes.set("POST /api/knowledge", async () => ({ changed: true, release_id: "release-two" }));
  routes.set("GET /api/conversations", async () => ({ items: [] }));
  nodes.get("status-filter").value = "all";
  return { ui: context.ui, calls, routes, events, nodes, element(id) { const node = nodes.get(id); assert(node, "expected visible control: " + id); return node; }, config() { return JSON.parse(nodes.get("knowledge-json").value); }, posts() { return calls.filter(call => call.method === "POST"); } };
}
const tests = []; function test(name, run) { tests.push([name, run]); }
const open = async env => { await env.ui.setPage("strategy"); };
const field = (env, key) => { const node = [...env.nodes.values()].find(item => item.dataset.requiredField === key); assert(node, "field checkbox must exist: " + key); return node; };
test("reply settings reads one API and preserves unknown keys when changing a Chinese-labelled checkbox", async () => {
  const env = harness(); await open(env);
  assert.equal(env.element("strategy-text").value, "简短友好");
  assert.equal(field(env,"product").checked, true); assert.equal(field(env,"custom_port").checked, true);
  field(env,"destination").checked = true; await field(env,"destination").trigger("change");
  await env.element("strategy-form").trigger("submit");
  assert.equal(env.posts().length, 1); assert.equal(env.posts()[0].url,"/api/reply-settings");
  assert.deepEqual(env.posts()[0].body,{text:"简短友好",required_fields:["product","custom_port","destination"],rules:["报价须由负责人确认"],base_release:"settings-one"});
  assert(!env.calls.some(call => call.url === "/api/strategy"));
});
test("unchecking every required field is rejected without losing the current form", async () => {
  const env = harness(); await open(env);
  for (const key of ["product","custom_port"]) {field(env,key).checked=false;await field(env,key).trigger("change");}
  await env.element("strategy-form").trigger("submit"); assert.equal(env.posts().length,0); assert.equal(env.element("strategy-error").hidden,false);
  assert.equal(field(env,"product").checked,false); assert.equal(env.element("strategy-text").value,"简短友好");
});
test("custom question can be added and duplicate labels retain the existing key", async () => {
  const env = harness(); await open(env);
  env.element("reply-custom-field").input("采购用途"); await env.element("reply-field-add").trigger("click");
  assert.equal(field(env,"采购用途").checked,true);
  env.element("reply-custom-field").input("产品"); await env.element("reply-field-add").trigger("click");
  await env.element("strategy-form").trigger("submit");
  assert.deepEqual(env.posts()[0].body.required_fields,["product","custom_port","采购用途"]);
});
test("business rules can be edited added and removed while a zero-rule form is valid", async () => {
  const env = harness(); await open(env); env.element("reply-rule-0").input("特殊包装需确认");
  await env.element("reply-rule-add").trigger("click"); env.element("reply-rule-1").input("超大件需确认");
  await env.element("strategy-form").trigger("submit"); assert.deepEqual(env.posts()[0].body.rules,["特殊包装需确认","超大件需确认"]);
  await env.element("reply-rule-remove-1").trigger("click"); await env.element("reply-rule-remove-0").trigger("click");
  await env.element("strategy-form").trigger("submit"); assert.deepEqual(env.posts()[1].body.rules,[]);
});
test("blank rule is rejected before HTTP and other edited rules remain intact", async () => {
  const env = harness(); await open(env); env.element("reply-rule-0").input("保留的确认规则");
  await env.element("reply-rule-add").trigger("click"); await env.element("strategy-form").trigger("submit");
  assert.equal(env.posts().length,0); assert.equal(env.element("reply-rule-0").value,"保留的确认规则");
});
test("leaving dirty settings requires explicit discard and keeping edits stays on the page", async () => {
  const env = harness(); await open(env); env.element("strategy-text").input("修改语气");
  await env.ui.setPage("knowledge"); assert.equal(env.ui.state.page,"strategy"); assert.equal(env.element("reply-settings-discard").open,true);
  await env.element("reply-discard-keep").trigger("click"); assert.equal(env.element("strategy-text").value,"修改语气");
  await env.ui.setPage("knowledge"); await env.element("reply-discard-confirm").trigger("click");
  assert.equal(env.ui.state.page,"knowledge"); await open(env); assert.equal(env.element("strategy-text").value,"简短友好");
});
test("browser refresh warns for preference edits and pending custom question text", async () => {
  const env = harness(); await open(env); const event={prevented:false,preventDefault(){this.prevented=true;}};
  env.events.get("beforeunload")(event); assert.equal(event.prevented,false);
  env.element("reply-custom-field").input("尚未添加"); env.events.get("beforeunload")(event); assert.equal(event.prevented,true);
});
test("save locks fields duplicate submission and navigation, then failure retains the input", async () => {
  const env = harness(); await open(env); env.element("strategy-text").input("我的偏好");
  const pending=deferred(); env.routes.set("POST /api/reply-settings",async()=>pending.promise);
  const request=env.element("strategy-form").trigger("submit"); await settle();
  assert.equal(env.element("strategy-text").disabled,true); assert.equal(field(env,"product").disabled,true);
  await env.element("strategy-form").trigger("submit"); await env.ui.setPage("knowledge");
  assert.equal(env.posts().length,1); assert.equal(env.ui.state.page,"strategy");
  pending.resolve({httpError:503,error:"暂不可用"}); await request;
  assert.equal(env.element("strategy-text").value,"我的偏好"); assert.equal(env.element("strategy-submit").disabled,false);
});
test("conflict keeps edits and requires confirmed reload before another save", async () => {
  const env = harness(); await open(env); env.element("strategy-text").input("本地草稿");
  env.routes.set("POST /api/reply-settings",async()=>({httpError:400,error:"企业资料已在其他页面更新，请重新载入后保存"}));
  await env.element("strategy-form").trigger("submit"); assert.equal(env.element("strategy-submit").disabled,true); assert.equal(env.element("strategy-text").value,"本地草稿");
  env.routes.set("GET /api/reply-settings",async()=>({text:"另一页面修改",required_fields:["weight_kg"],rules:[],active_release:"new-release"}));
  const count=env.calls.length; await env.element("reply-settings-reload").trigger("click"); assert.equal(env.calls.length,count);
  await env.element("reply-discard-keep").trigger("click"); assert.equal(env.element("strategy-text").value,"本地草稿");
  await env.element("reply-settings-reload").trigger("click"); await env.element("reply-discard-confirm").trigger("click");
  assert.equal(env.element("strategy-text").value,"另一页面修改");
  env.routes.set("POST /api/reply-settings",async()=>({changed:true,release_id:"saved"})); await env.element("strategy-form").trigger("submit");
  assert.equal(env.posts()[1].body.base_release,"new-release");
});
test("failed reload preserves edits and the stale base remains blocked", async () => {
  const env = harness(); await open(env); env.element("strategy-text").input("不能丢失");
  env.routes.set("POST /api/reply-settings",async()=>({httpError:409,error:"版本冲突"})); await env.element("strategy-form").trigger("submit");
  env.routes.set("GET /api/reply-settings",async()=>({httpError:503,error:"断开"}));
  await env.element("reply-settings-reload").trigger("click"); await env.element("reply-discard-confirm").trigger("click");
  assert.equal(env.element("strategy-text").value,"不能丢失"); assert.equal(env.element("strategy-submit").disabled,true);
});
test("late reads cannot replace a newer page load or its edits", async () => {
  const env=harness(); const pending=deferred(); env.routes.set("GET /api/reply-settings",async()=>pending.promise);
  const first=env.ui.setPage("strategy"); await settle(); await env.ui.setPage("inbox");
  env.routes.set("GET /api/reply-settings",async()=>({text:"新数据",required_fields:["product"],rules:[],active_release:"new-release"}));
  await open(env); env.element("strategy-text").input("新页面的修改");
  pending.resolve({text:"旧数据",required_fields:["goods"],rules:[],active_release:"old-release"}); await first;
  assert.equal(env.element("strategy-text").value,"新页面的修改"); await env.element("strategy-form").trigger("submit"); assert.equal(env.posts().length,1); assert.equal(env.posts()[0].body.base_release,"new-release");
});
test("knowledge background refresh never advances the settings editor baseline", async () => {
  const env=harness(); await open(env); env.element("strategy-text").input("使用旧基线"); await env.ui.loadKnowledge();
  await env.element("strategy-form").trigger("submit"); assert.equal(env.posts().length,1); assert.equal(env.posts()[0].body.base_release,"settings-one");
});
test("successful save followed by failed list refresh is reported as saved", async () => {
  const env=harness(); await open(env); env.element("strategy-text").input("新偏好"); env.routes.set("GET /api/conversations",async()=>({httpError:503,error:"刷新失败"}));
  await env.element("strategy-form").trigger("submit"); assert.equal(env.element("strategy-error").hidden,true);
  assert.match(env.element("toast").textContent,/已保存/); const event={prevented:false,preventDefault(){this.prevented=true;}}; env.events.get("beforeunload")(event); assert.equal(event.prevented,false);
});
test("ordinary knowledge views hide rules and advanced JSON while managers can open advanced mode", async () => {
  const env=harness(); env.ui.state.page="knowledge"; await env.ui.loadKnowledge();
  assert(!env.element("knowledge-content").innerHTML.includes("不得猜测")); assert(!env.element("knowledge-content").innerHTML.includes("询盘必备信息"));
  await env.ui.openKnowledge(); assert.equal(env.element("knowledge-advanced").hidden,true);
  await env.element("knowledge-close").trigger("click"); await env.element("knowledge-advanced-open").trigger("click");
  assert.equal(env.element("knowledge-advanced").hidden,false); assert.equal(env.element("knowledge-advanced").open,true);
});
test("untrusted custom question and rule values stay text in rendered controls", async () => {
  const env=harness({text:"<img>",required_fields:["product","<script>x</script>"],rules:["</textarea><img src=x>"],active_release:"one"}); await open(env);
  assert(!env.element("reply-required-fields").innerHTML.includes("<script>")); assert(!env.element("reply-rules").innerHTML.includes("<img src=x>"));
  assert.equal(env.element("reply-rule-0").value,"</textarea><img src=x>");
});
test("editing only preferences preserves legacy whitespace values and required field order exactly", async () => {
  const env=harness({text:"原偏好",required_fields:["SKU"," SKU ","destination","origin"],rules:["先核实"," 先核实 "],active_release:"legacy"});
  await open(env); env.element("strategy-text").input("只改语气"); await env.element("strategy-form").trigger("submit");
  assert.equal(env.posts().length,1); assert.deepEqual(env.posts()[0].body.required_fields,["SKU"," SKU ","destination","origin"]); assert.deepEqual(env.posts()[0].body.rules,["先核实"," 先核实 "]);
});
test("custom fields named like object prototype properties keep their literal labels and values", async () => {
  const env=harness({text:"",required_fields:["constructor","toString","product"],rules:[],active_release:"one"}); await open(env);
  assert(env.element("reply-required-fields").innerHTML.includes("constructor")); assert(!env.element("reply-required-fields").innerHTML.includes("function Object"));
  assert.equal(field(env,"constructor").checked,true); await env.element("strategy-form").trigger("submit");
  assert.deepEqual(env.posts()[0].body.required_fields,["constructor","toString","product"]);
});
test("leaving an edited knowledge dialog for reply settings requires confirmation", async () => {
  const env=harness(); await env.ui.setPage("knowledge"); await env.ui.openKnowledge(); env.element("knowledge-title").input("资料未保存");
  await env.ui.setPage("strategy"); assert.equal(env.ui.state.page,"knowledge"); assert.equal(env.element("knowledge-dialog").open,true);
  await env.element("knowledge-confirm-keep").trigger("click"); assert.equal(env.element("knowledge-title").value,"资料未保存");
  await env.ui.setPage("strategy"); await env.element("knowledge-confirm-accept").trigger("click");
  assert.equal(env.ui.state.page,"strategy"); assert.equal(env.element("knowledge-dialog").open,false); assert.equal(env.posts().length,0);
});
test("opening advanced maintenance cannot silently discard pending reply settings", async () => {
  const env=harness(); await open(env); env.element("strategy-text").input("尚未保存");
  await env.element("knowledge-advanced-open").trigger("click"); assert.equal(env.element("reply-settings-discard").open,true); assert.equal(env.element("knowledge-dialog").open,false);
  await env.element("reply-discard-keep").trigger("click"); assert.equal(env.element("strategy-text").value,"尚未保存");
});
(async()=>{let failed=0;for(const[name,run]of tests){try{await run();console.log("PASS "+name);}catch(error){failed++;console.error("FAIL "+name+"\n"+error.stack);}}console.log(`${tests.length-failed}/${tests.length} reply settings UI checks passed`);if(failed)process.exitCode=1;})();
