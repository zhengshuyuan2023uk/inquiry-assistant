"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { bootstrap: null, conversations: [], conversationsVersion: 0, selected: null, detail: null, detailVersion: 0, page: "inbox", pageVersion: 0, knowledge: null, activeJob: null, pollTimer: null, toastTimer: null, busyAnalyze: false, busyAdopt: false, busyMessage: false, busySync: false, sync: null, updatedScopes: new Set(), newScopes: new Set(), replyEdits: new Map(), originalEdits: new Map(), instructionEdits: new Map(), messageEdits: new Map(), draftChoices: new Map(), languages: new Map(), pendingAnalysis: null, messageTarget: null, strategyRelease: null, strategyDirty: false, strategyVersion: 0, models: [], modelId: "", modelsLoaded: false, modelsLoading: false, modelsVersion: 0, modelError: "", modelNotice: "", jobStream: null, jobWatchVersion: 0, jobConnection: "idle", jobPollInFlight: false, jobPollController: null, jobFinalizedId: null, jobCancelPending: null, jobCancelNotice: "", jobResultLoadingId: null, jobResultError: false };
  const statusLabels = { needs_sync: "待更新聊天", needs_analysis: "待生成回复", pending: "有可用回复", approved: "已采用", rejected: "历史回复", stale: "需重新生成" };
  state.customers = { snapshot: null, selected: new Set(), baseline: new Set(), scanning: false, saving: false, valid: false, error: "", saved: false, discardAction: null };
  state.knowledgeEditor = { baseline: "", release: null, loading: false, saving: false, conflict: false, confirmAction: null };
  state.replySettings = { baseline: "", release: null, fields: [], customFields: [], rules: [], loaded: false, loading: false, saving: false, conflict: false, version: 0, confirmAction: null };
  const fieldLabels = { origin:"起运地", destination:"目的地", goods:"货物", quantity:"数量", weight_kg:"毛重", volume_cbm:"体积", transport_mode:"运输方式", deadline:"期望交付", product:"产品", specification:"规格" };
  const languages = { auto:"跟随客户", en:"英语", es:"西班牙语", fr:"法语", de:"德语", pt:"葡萄牙语", ar:"阿拉伯语", zh:"中文", ja:"日语", ko:"韩语", it:"意大利语", ru:"俄语" };
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
  const key = (item) => item ? JSON.stringify([item.account_id,item.conversation_id]) : null;
  const same = (a,b) => key(a) === key(b);
  const label = (field) => Object.hasOwn(fieldLabels, field) ? fieldLabels[field] : field;
  const isRunning = (job) => !!job && ["queued","running","cancelling"].includes(job.status);
  const currentDraft = () => state.detail?.drafts?.find((draft) => draft.id === state.draftChoices.get(key(state.selected))) || state.detail?.drafts?.[0] || null;
  const stale = (draft) => !!draft && (draft.requires_recheck || draft.status === "stale");
  const badge = (status) => `<span class="badge ${Object.hasOwn(statusLabels,status) ? status : "needs_analysis"}">${escape(statusLabels[status] || "待生成回复")}</span>`;
  const loading = (text) => `<div class="loading-state"><span class="spinner"></span>${escape(text)}</div>`;
  const empty = (title,text,extra="") => `<div class="empty-state"><h3>${escape(title)}</h3><p>${escape(text)}</p>${extra}</div>`;
  function time(value,full=false) { if(!value)return "—"; const date=new Date(value); return Number.isNaN(date.valueOf()) ? String(value) : new Intl.DateTimeFormat("zh-CN",full?{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}:{hour:"2-digit",minute:"2-digit",hour12:false}).format(date); }
  function day(value) { const date=new Date(value); return Number.isNaN(date.valueOf()) ? "聊天记录" : new Intl.DateTimeFormat("zh-CN",{month:"long",day:"numeric",weekday:"short"}).format(date); }
  function initials(title) { const name=String(title||"客户").trim(); return /^[a-zA-Z]/.test(name)?name.split(/\s+/).slice(0,2).map(word=>word[0]).join("").toUpperCase():[...name].slice(0,2).join(""); }
  function toast(message,error=false) { clearTimeout(state.toastTimer); $("toast").textContent=message; $("toast").classList.toggle("error",error); $("toast").hidden=false; state.toastTimer=setTimeout(()=>{$("toast").hidden=true;},error?8000:4800); }
  function formError(id,message="") { $(id).textContent=message; $(id).hidden=!message; }
  async function api(path,data,signal) { const options={credentials:"same-origin",headers:{Accept:"application/json"},cache:"no-store"}; if(signal)options.signal=signal; if(data!==undefined){options.method="POST"; options.headers["Content-Type"]="application/json";options.headers["X-CSRF-Token"]=state.bootstrap?.csrf_token||"";options.body=JSON.stringify(data);} let response,result;try{response=await fetch(path,options);}catch{throw new Error("无法连接工作台，请重新打开询盘助手；仍不可用时请联系负责人。");} try{result=await response.json();}catch{throw new Error("工作台返回了无法读取的结果，请刷新后重试。");}if(!response.ok){const error=new Error(result.error||`操作失败（${response.status}），请重试。`);error.status=response.status;throw error;}return result; }
  function modelStorageKey(){const company=state.bootstrap?.company;return company?"inquiry-assistant:model:"+JSON.stringify([company.id,company.mode,location.port]):null;}
  function selectedModelAvailable(){return state.modelsLoaded&&!state.modelsLoading&&!state.modelError&&state.models.some(model=>model.id===state.modelId);}
  function renderModels(){
    const locked=isRunning(state.activeJob)||state.busyAnalyze||state.busyAdopt,ready=selectedModelAvailable();
    $("reply-model").disabled=locked||state.modelsLoading||!state.modelsLoaded||!!state.modelError;
    $("model-retry").disabled=locked||state.modelsLoading;
    $("model-status").classList.toggle("model-status-error",!!state.modelError||(!state.modelsLoading&&!state.modelId));
    const selected=state.models.find(model=>model.id===state.modelId);
    const message=state.modelsLoading?"正在读取可用模型…":state.modelError||state.modelNotice||(!state.modelId?"请选择后续回复使用的模型。":`已配置：${selected?.name||state.modelId}。生成时使用该型号，不会自动切换。`);
    $("model-status-text").textContent=message;
    $("model-status").hidden=false;
    const engine=state.bootstrap?.capabilities?.model;
    $("manager-service-engine").textContent=engine==="codex_app_server"?"Codex App Server · 本机后台连接":engine==="codex"||engine==="codex_exec"?"Codex exec · 兼容运行方式":engine?"已配置 AI 连接":"等待工作台连接";
    $("ai-service-status").hidden=ready;
    $("ai-service-status-text").textContent=state.modelsLoading?"正在连接 AI 服务…":state.modelError?"AI 服务暂不可用，请重新检查；仍不可用时请联系负责人。":"AI 服务需要完成设置，请联系负责人。";
    $("ai-service-retry").disabled=locked||state.modelsLoading;
  }
  function openManagerSettings(event){
    state.managerSettingsReturnId=event?.target?.id==="ai-settings-open"?"ai-settings-open":"more-open";
    $("more-dialog").close();
    renderModels();
    $("manager-settings-dialog").showModal();
  }
  async function loadModels(){
    if(state.modelsLoading)return;
    const version=++state.modelsVersion;state.modelsLoading=true;state.modelsLoaded=false;state.modelError="";state.modelNotice="";
    $("reply-model").innerHTML='<option value="">正在读取…</option>';renderJob();
    try{
      const result=await api("/api/models");if(version!==state.modelsVersion)return;
      if(result.error)throw new Error(String(result.error));
      const models=Array.isArray(result.models)?result.models.filter(model=>model&&typeof model.id==="string"&&model.id.trim()&&typeof model.name==="string"&&model.name.trim()):[];
      if(!models.length||new Set(models.map(model=>model.id)).size!==models.length)throw new Error("未取得可用模型，请重新读取。");
      let saved="";try{const storageKey=modelStorageKey();if(storageKey)saved=localStorage.getItem(storageKey)||"";}catch{}
      const remembered=models.find(model=>model.id===saved),workspaceDefault=models.find(model=>model.id===result.default_model),catalogDefault=models.find(model=>model.default===true),preferred=result.default_model?workspaceDefault:catalogDefault;
      state.models=models;state.modelsLoaded=true;state.modelId=(saved?remembered?.id:preferred?.id)||"";
      if(saved&&!remembered)state.modelNotice="上次使用的模型不在当前列表，请重新选择。系统未自动切换型号。";
      if(result.default_model&&!workspaceDefault&&!saved)state.modelNotice="工作台默认模型暂不可用，请选择列表中的可用型号。系统未自动切换型号。";
      $("reply-model").innerHTML=(state.modelId?"":'<option value="">请选择模型</option>')+models.map(model=>`<option value="${escape(model.id)}">${escape(model.name)}${model.id===preferred?.id?" · 默认":""}</option>`).join("");
      $("reply-model").value=state.modelId;
    }catch(error){if(version!==state.modelsVersion)return;state.models=[];state.modelId="";state.modelsLoaded=false;state.modelError=`模型列表读取失败：${error.message}`;$("reply-model").innerHTML='<option value="">未能读取模型</option>';}
    finally{if(version===state.modelsVersion){state.modelsLoading=false;renderJob();}}
  }
  function setPane(pane){ document.querySelector(".inbox-grid").dataset.mobilePane=pane; document.querySelectorAll("[data-pane]").forEach(button=>{button.classList.toggle("active",button.dataset.pane===pane);button.setAttribute("aria-pressed",String(button.dataset.pane===pane));}); }
  async function setPage(page) {
    if (state.replySettings.saving || knowledgeBusy()) { toast("正在保存或载入，请稍候再切换。"); return; }
    if (page !== state.page && replySettingsDirty()) {
      confirmReplyDiscard("切换页面将放弃尚未保存的回复设置。", async () => { restoreReplyBaseline(); await showPage(page); }); return;
    }
    if (knowledgeDirty()) {
      confirmKnowledge("切换页面将放弃尚未保存的企业资料。", "放弃修改并切换", async () => { $("knowledge-dialog").close(); await showPage(page); }); return;
    }
    if ($("knowledge-dialog").open) $("knowledge-dialog").close();
    await showPage(page);
  }
  async function showPage(page) {
    if (state.page === "strategy" && page !== "strategy" && state.replySettings.loading) {
      state.replySettings.version++; state.replySettings.loading = false;
    }
    state.page=page;const version=++state.pageVersion;
    document.querySelectorAll(".main-nav [data-page]").forEach(button=>{const active=button.dataset.page===page;button.classList.toggle("active",active);if(active)button.setAttribute("aria-current","page");else button.removeAttribute("aria-current");});
    for(const name of ["inbox","knowledge","strategy","activity"])$("page-"+name).hidden=name!==page;
    if(page==="knowledge")await loadKnowledge(version);if(page==="activity")await loadActivity(version);if(page==="strategy")await loadStrategy();
  }
  function filteredConversations(){const query=$("conversation-search").value.trim().toLocaleLowerCase();const filter=$("status-filter").value;return state.conversations.filter(item=>(filter==="all"||item.status===filter)&&(!query||[item.title,item.subtitle,item.preview].join(" ").toLocaleLowerCase().includes(query))).sort((a,b)=>Number(state.newScopes.has(key(b)))-Number(state.newScopes.has(key(a))));}
  function renderConversations(){const items=filteredConversations();$("conversation-count").textContent=state.conversations.length;$("visible-count").textContent=items.length===state.conversations.length?"最近会话":`${items.length} 条结果`;if(!items.length){$("conversation-list").innerHTML=empty(state.conversations.length?"没有匹配的客户":"还没有聊天记录",state.conversations.length?"换个关键词，或切换筛选条件。":"先更新聊天，或从更多操作导入记录。",state.conversations.length?'<button class="button secondary small" id="clear-filters">清除筛选</button>':"");$("clear-filters")?.addEventListener("click",()=>{$("conversation-search").value="";$("status-filter").value="all";renderConversations();});return;}
    $("conversation-list").innerHTML=items.map((item,index)=>`<button class="conversation-item ${same(item,state.selected)?"active":""}" data-index="${index}" aria-pressed="${same(item,state.selected)}"><span class="contact-avatar tone-${state.conversations.indexOf(item)%4}">${escape(initials(item.title))}</span><span class="conversation-details"><span class="conversation-title-row"><strong>${escape(item.title)}</strong><time>${escape(time(item.last_at))}</time></span><span class="conversation-subtitle">${escape(item.subtitle||"客户会话")}</span><span class="conversation-preview">${escape(item.preview||"暂无消息正文")}</span><span class="conversation-status-row">${state.updatedScopes.has(key(item))?'<span class="badge updated">新消息</span>':badge(item.status)}<span>${Number(item.message_count)||0} 条</span></span></span></button>`).join("");$("conversation-list").querySelectorAll("[data-index]").forEach(button=>button.addEventListener("click",()=>{selectConversation(items[Number(button.dataset.index)]);setPane("work");}));}
  async function refreshConversations(selectFirst=false) {
    const version = ++state.conversationsVersion, data = await api("/api/conversations");
    if (version !== state.conversationsVersion) return;
    state.conversations = data.items || [];
    if (state.selected) {
      const item = state.conversations.find(item => same(item, state.selected));
      if (item) {
        state.selected = item;
        $("chat-title").textContent = item.title || "客户会话";
        $("chat-subtitle").textContent = item.subtitle || "客户会话";
        $("chat-avatar").textContent = initials(item.title);
        if (state.detail && same(state.detail.conversation, item) &&
            (state.detail.conversation.title !== item.title || state.detail.conversation.subtitle !== item.subtitle)) {
          state.detail.conversation = { ...state.detail.conversation, title: item.title, subtitle: item.subtitle };
          renderChat(true);
        }
      }
    }
    renderConversations();
    if (selectFirst && !state.selected && state.conversations.length) await selectConversation(state.conversations[0]);
  }
  async function selectConversation(item,preserveScroll=false){const target={...item},listedConversations=state.conversations;state.selected=target;state.detail=null;const scope=key(target),version=++state.detailVersion;state.updatedScopes.delete(scope);state.newScopes.delete(scope);renderConversations();$("workspace-empty").hidden=true;$("workspace-content").hidden=false;$("chat-title").textContent=item.title||"客户会话";$("chat-subtitle").textContent=item.subtitle||"导入会话";$("chat-avatar").textContent=initials(item.title);$("chat-status").className=`badge ${Object.hasOwn(statusLabels,item.status)?item.status:"needs_analysis"}`;$("chat-status").textContent=statusLabels[item.status]||"待生成回复";$("chat-message-count").textContent="";$("reply-language").value=state.languages.get(scope)||"auto";$("message-list").innerHTML=loading("读取聊天记录…");$("reply-body").innerHTML=loading("读取回复…");renderJob();try{const data=await api(`/api/conversation?${new URLSearchParams({account:item.account_id,conversation:item.conversation_id})}`);if(version!==state.detailVersion||key(state.selected)!==scope)return;if(state.conversations!==listedConversations){const refreshed=state.conversations.find(entry=>same(entry,target));if(refreshed)data.conversation={...data.conversation,title:refreshed.title,subtitle:refreshed.subtitle};}state.detail=data;state.selected={...data.conversation};renderChat(preserveScroll);renderReply();renderJob();renderConversations();}catch(error){if(version!==state.detailVersion||key(state.selected)!==scope)return;$("message-list").innerHTML=empty("暂时无法读取会话",error.message,'<button class="button secondary small" id="retry-conversation">重试</button>');$("retry-conversation").addEventListener("click",()=>selectConversation(target));$("reply-body").innerHTML=empty("回复尚未载入","重新读取会话后继续。");renderJob();}}
  function renderChat(preserveScroll=false){if(!state.detail)return;const item=state.detail.conversation,messages=state.detail.messages||[],scroll=$("message-list").scrollTop;$("chat-title").textContent=item.title||"客户";$("chat-subtitle").textContent=item.subtitle||"客户会话";$("chat-avatar").textContent=initials(item.title);$("chat-status").className=`badge ${Object.hasOwn(statusLabels,item.status)?item.status:"needs_analysis"}`;$("chat-status").textContent=statusLabels[item.status]||"待生成回复";$("chat-message-count").textContent=`${messages.length} 条记录`;$("chat-context-label").textContent=state.bootstrap.company.mode==="simulation"?"虚构演练记录 · 不代表真实客户或业务":"聊天记录仅供当前企业处理";let previous="";$("message-list").innerHTML=messages.map((message,index)=>{const date=day(message.sent_at),divider=date!==previous?`<div class="date-divider">${escape(date)}</div>`:"";previous=date;return `${divider}<article class="message ${message.direction==="outbound"?"outbound":"inbound"}" id="chat-message-${index}"><div class="message-meta"><span>${escape(message.direction==="outbound"?"销售":item.title||"客户")}</span><time>${escape(time(message.sent_at))}</time></div><div class="message-bubble" dir="auto">${escape(message.body)}</div></article>`;}).join("")||empty("暂未读取到这位客户的记录","先点击更新聊天；源端暂未提供此联系人的记录。");requestAnimationFrame(()=>{$("message-list").scrollTop=preserveScroll?scroll:$("message-list").scrollHeight;});}
  function cachedReply(draft){if(!draft)return state.originalEdits.get(key(state.selected))||"";const cached=state.replyEdits.get(draft.id);if(cached&&draft.status==="approved"&&(cached.status!=="approved"||cached.finalText!==draft.final_text)){state.replyEdits.delete(draft.id);return draft.final_text||draft.result?.reply||"";}return cached?.text??draft.final_text??draft.result?.reply??"";}
  function renderReply(){const detail=state.detail;if(!detail)return;const draft=currentDraft(),result=draft?.result||{},scope=key(state.selected),isStale=stale(draft),mode=draft?.request?.mode||"generate",facts=result.facts||[],required=detail.required_fields||[],citations=result.citations||[],rationale=Array.isArray(result.rationale)?result.rationale:[],warnings=Array.isArray(result.warnings)?result.warnings:(Array.isArray(result.uncertainties)?result.uncertainties:[]);
    let html=`<section class="stream-preview" id="job-preview" aria-label="生成中的回复预览" hidden><div class="stream-preview-heading"><strong>生成中，完成后可复制</strong><span>原稿保留在下方</span></div><div class="stream-preview-text" id="job-preview-text" dir="auto" tabindex="0"></div><p>逐步预览尚未检查，请等待生成完成。</p></section><div class="reply-meta"><span class="reply-label">${draft?(mode==="polish"?"润色后的正文":"对客回复正文"):"在这里准备要发给客户的话"}</span>${draft?`<span class="badge ${draft.runner==="demo_fixture"?"fixture":"generated"}">${draft.runner==="demo_fixture"?"示例草稿":mode==="polish"?"AI 润色":mode==="refine"?"已按要求调整":"AI 生成"}</span>`:""}${result.reply_language?`<span class="language-result">${escape(languages[result.reply_language]||result.reply_language)}</span>`:""}<span class="reply-edit-state" id="reply-edit-state"></span></div>`;
    if(isStale)html+='<div class="warning-note stale-note">聊天、资料或策略已更新。这份回复不能直接复制；请重新生成，也可写下新内容后点击润色。</div>';
    html+=`<label class="sr-only" for="reply-editor">对客回复正文</label><textarea id="reply-editor" class="reply-editor" rows="4" dir="auto" placeholder="可以先用中文写下你想表达的意思，再选择对客语言进行润色。" >${escape(cachedReply(draft))}</textarea>`;
    if(rationale.length)html+=`<div class="reply-rationale"><span>这样写的理由</span><p>${escape(rationale.join(" "))}</p></div>`;
    if(!detail.messages?.length)html+='<p class="compose-hint">先点击更新聊天；源端暂未提供此联系人的记录。</p>';
    else if(!draft)html+='<p class="compose-hint">点击“AI 帮我回复”，或先写下想表达的内容，再点“润色我写的话”。这里的文字不会加入客户聊天。</p>';
    if(warnings.length)html+=`<div class="visible-warnings"><span>请注意</span><p>${escape([...new Set(warnings)].join("；"))}</p></div>`;

    if(draft&&!isStale)html+=`<div class="refine-row"><label class="sr-only" for="refine-instruction">用中文说明如何调整回复</label><input id="refine-instruction" class="text-input" maxlength="4000" placeholder="用中文说要求：更简短一点，先问体积…" value="${escape(state.instructionEdits.get(scope)||"")}"><button class="button secondary small" id="refine-button">按我的要求调整</button></div>`;
    html+='<div class="reply-details">';
    if(draft&&mode!=="polish")html+=`<details class="reply-detail"><summary>需求与报价信息 <span>${facts.length}/${required.length}</span></summary>${result.summary?`<p class="detail-summary">${escape(result.summary)}</p>`:""}<div class="facts-grid">${required.map(field=>{const fact=facts.find(item=>item.field===field);return `<div class="fact-row ${fact?"known":"missing"}"><span class="fact-label">${escape(label(field))}</span><span class="fact-value">${escape(fact?.value||"待向客户确认")}${fact?.message_ids?.length?`<span class="fact-sources">${fact.message_ids.map((id,index)=>`<button class="fact-source" data-source="${escape(id)}">依据${fact.message_ids.length>1?" "+(index+1):""} ↗</button>`).join("")}</span>`:""}</span></div>`;}).join("")}</div></details>`;
    if(draft)html+=`<details class="reply-detail"><summary>资料依据 <span>${citations.length}</span></summary>${citations.map(citation=>{const document=(draft.citation_sources||[]).find(item=>item.id===citation.id&&String(item.version)===String(citation.version));return `<article class="citation"><h3>${escape(document?.title||"历史资料引用")}</h3><span class="citation-tag">${escape(citation.id)} · 版本 ${escape(citation.version)}</span><p>${escape(document?.content||"未保存可还原的引用原文，当前资料不能替代生成时依据。")}</p>${document?`<span class="citation-tag">有效期 ${escape(document.valid_from)} — ${escape(document.valid_until)}</span>`:""}</article>`;}).join("")||'<p class="reply-note">本次没有引用企业资料。</p>'}</details>`;
    if(detail.drafts?.length)html+=`<details class="reply-detail"><summary>历史回复 <span>${detail.drafts.length}</span></summary><label class="form-label" for="draft-history">查看回复版本</label><select class="text-input" id="draft-history">${detail.drafts.map((item,index)=>`<option value="${escape(item.id)}" ${item.id===draft?.id?"selected":""}>${index===0?"最新":"历史"} · ${escape(time(item.created_at,true))} · ${escape(statusLabels[item.status]||"回复")}${state.replyEdits.has(item.id)?" · 有本地修改":""}</option>`).join("")}</select></details>`;
    html+='</div>';$("reply-body").innerHTML=html;
    $("reply-editor").addEventListener("input",event=>{if(state.busyAdopt){event.target.value=cachedReply(draft);return;}if(draft)state.replyEdits.set(draft.id,{text:event.target.value,status:draft.status,finalText:draft.final_text});else state.originalEdits.set(scope,event.target.value);updateEditState();renderJob();});
    $("refine-instruction")?.addEventListener("input",event=>{if(state.busyAdopt){event.target.value=state.instructionEdits.get(scope)||"";return;}state.instructionEdits.set(scope,event.target.value);});$("refine-instruction")?.addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.isComposing){event.preventDefault();requestAnalysis("refine");}});$("refine-button")?.addEventListener("click",()=>requestAnalysis("refine"));
    $("draft-history")?.addEventListener("change",event=>{state.draftChoices.set(scope,event.target.value);renderReply();renderJob();});$("reply-body").querySelectorAll("[data-source]").forEach(button=>button.addEventListener("click",()=>focusMessage(button.dataset.source)));updateEditState();}
  function updateEditState(){const draft=currentDraft();if(!$("reply-edit-state"))return;const dirty=draft&&$("reply-editor")?.value!==(draft.final_text??draft.result?.reply??"");$("reply-edit-state").textContent=dirty?"有未采用的修改":draft?.status==="approved"?"已采用":"可直接编辑";}
  function focusMessage(id){const index=state.detail?.messages?.findIndex(message=>message.message_id===id);if(index===undefined||index<0)return;const node=$("chat-message-"+index);node?.scrollIntoView({behavior:"smooth",block:"center"});node?.classList.add("message-highlight");setTimeout(()=>node?.classList.remove("message-highlight"),2200);}
  function renderJob(){
    const job=state.activeJob,busy=isRunning(job),sameCustomer=!!job&&same(job,state.selected),here=busy&&sameCustomer;
    const ready=!!state.detail&&!!state.detail.messages?.length,draft=currentDraft(),blocked=stale(draft),hasText=!!$("reply-editor")?.value.trim(),modelReady=selectedModelAvailable();
    const waitingForResult=sameCustomer&&job?.status==="succeeded"&&(state.jobResultLoadingId===job.id||state.jobResultError);
    $("generate-button").disabled=!ready||!modelReady||busy||state.busyAnalyze||state.busyAdopt;
    $("generate-button").classList.toggle("primary",!draft);$("generate-button").classList.toggle("secondary",!!draft);
    $("polish-button").disabled=!ready||!modelReady||busy||state.busyAnalyze||state.busyAdopt;
    $("reply-language").disabled=!ready||here||state.busyAdopt;
    $("adopt-button").disabled=!ready||!draft||blocked||!hasText||here||waitingForResult||state.busyAdopt||draft.status==="rejected";
    $("adopt-button").textContent=state.busyAdopt?"保存并复制中…":"复制回复";
    $("adopt-status").textContent=here?"生成中，完成后可复制":waitingForResult?"等待载入已完成的回复":blocked?"已过期，请重新生成":"仅保存采用结果，不会向客户发送";
    const successWarning=job?.status==="succeeded"&&typeof job.warning==="string"?job.warning.trim():"";
    const terminalNotice=sameCustomer&&(["failed","cancelled","interrupted"].includes(job?.status)||!!successWarning);
    $("job-banner").hidden=!busy&&!terminalNotice&&!waitingForResult;
    const disconnected=busy&&["reconnecting","disconnected"].includes(state.jobConnection);
    $("job-banner").classList.toggle("job-disconnected",disconnected||!!terminalNotice||!!state.jobResultError);
    $("job-status-icon").hidden=!busy&&!state.jobResultLoadingId;
    $("job-cancel").hidden=!busy;
    const stopping=busy&&(job.status==="cancelling"||state.jobCancelPending===job.id);
    $("job-cancel").disabled=!busy||stopping||job?.can_cancel===false;
    $("job-cancel").textContent=stopping?(state.jobCancelNotice?"等待确认…":"正在停止…"):job?.can_cancel===false?"暂不可停止":"停止生成";
    $("job-reconnect").hidden=!disconnected&&!state.jobResultError;
    $("job-reconnect").disabled=!!state.jobResultLoadingId;
    $("job-reconnect").textContent=state.jobResultError?"重新读取结果":"重新连接";
    let message="";
    if(busy){
      if(disconnected)message="连接暂时中断，正在查询原任务；原稿已保留。";
      else if(job.status==="cancelling")message="正在停止生成，请等待任务确认结束。";
      else if(state.jobCancelPending===job.id)message=state.jobCancelNotice||"已请求停止，正在等待任务确认。";
      else message=job.progress_message||"正在准备回复，请保持工作台运行。";
      if(!sameCustomer)message="另一位客户的回复："+message;
    }else if(waitingForResult){message=state.jobResultError?"回复已完成，但尚未载入。请重新读取结果后再复制。":"回复已完成，正在载入检查后的结果。";if(successWarning)message+=" "+successWarning;}
    else if(terminalNotice)message=jobTerminalMessage(job);
    if($("job-status-text").textContent!==message)$("job-status-text").textContent=message;
    if($("reply-editor"))$("reply-editor").readOnly=here||state.busyAdopt;
    if($("refine-button"))$("refine-button").disabled=!modelReady||busy||blocked||state.busyAnalyze||state.busyAdopt;
    if($("refine-instruction"))$("refine-instruction").disabled=here||state.busyAdopt;
    renderJobPreview();renderModels();
  }
  function renderJobPreview(){
    const container=$("job-preview"),body=$("job-preview-text");if(!container||!body)return;
    const job=state.activeJob,show=isRunning(job)&&same(job,state.selected);
    container.hidden=!show;
    const text=show&&typeof job.preview_reply==="string"?job.preview_reply:"";
    const visibleText=show?(text||"正在整理内容，完成后会显示可用回复。") : "";
    if(body.textContent!==visibleText)body.textContent=visibleText;
    container.classList.toggle("preview-waiting",!text);
  }
  function jobTerminalMessage(job){
    const status=job?.status,error=typeof job?.error==="string"?job.error.trim():"",warning=typeof job?.warning==="string"?job.warning.trim():"";
    if(status==="failed"&&error)return `本次生成未完成，原稿已保留。${error}`;
    const message={succeeded:"回复已准备好，请核对后复制。",failed:"本次生成未完成，原稿已保留。可以重新生成。",cancelled:"已停止生成，本次预览未作为回复保存，原稿已保留。",interrupted:"生成已中断，原稿已保留。确认工作台运行后可重新生成。"}[status]||"任务状态已更新。";
    return status==="succeeded"&&warning?`${message} ${warning}`:message;
  }
  function requestAnalysis(mode){if(!selectedModelAvailable()){toast("AI 服务尚未就绪，请重新检查或联系负责人完成设置。",true);return;}if(!state.selected||!state.detail||!state.detail.messages?.length||isRunning(state.activeJob)||state.busyAnalyze||state.busyAdopt)return;const draft=currentDraft(),original=$("reply-editor")?.value.trim()||"",instruction=$("refine-instruction")?.value.trim()||"";if(mode==="polish"&&!original){toast("先在回复框写下想表达的内容，再点润色。",true);$("reply-editor").focus();return;}if(mode==="refine"&&(!draft||stale(draft))){toast("这份回复已过期，请先重新生成。",true);return;}if(mode==="refine"&&!original){toast("回复正文不能为空，请先写下原文或重新生成。",true);$("reply-editor").focus();return;}if(mode==="refine"&&!instruction){toast("请用中文写下希望怎样调整。",true);$("refine-instruction").focus();return;}const target={...state.selected},request={mode,instruction:mode==="refine"?instruction:"",original_text:mode==="generate"?"":original,base_draft_id:mode==="refine"?draft.id:null,language:state.languages.get(key(target))||"auto",model:state.modelId};if(state.bootstrap.company.mode==="customer"&&!state.bootstrap.capabilities?.customer_model_allowed){state.pendingAnalysis={target,request};$("customer-model-dialog").showModal();}else startAnalysis(target,request,state.bootstrap.company.mode==="customer"&&!!state.bootstrap.capabilities?.customer_model_allowed);}
  async function startAnalysis(target,request,allow){if(state.busyAnalyze||state.busyAdopt)return;if(!selectedModelAvailable()||!state.models.some(model=>model.id===request.model)){toast("AI 服务设置已变化，请联系负责人检查后再试。",true);return;}state.busyAnalyze=true;renderJob();try{const result=await api("/api/analyze",{account_id:target.account_id,conversation_id:target.conversation_id,allow_customer_model:allow,request});if(allow&&state.bootstrap.capabilities)state.bootstrap.capabilities.customer_model_allowed=true;watchJob(result.job);toast(request.mode==="polish"?"正在润色你的原文。":"正在准备回复，可继续查看其他客户。");}catch(error){toast(error.message,true);}finally{state.busyAnalyze=false;renderJob();}}
  function closeJobTransport(){
    if(state.jobStream){state.jobStream.close();state.jobStream=null;}
    clearTimeout(state.pollTimer);state.pollTimer=null;
    if(state.jobPollController){state.jobPollController.abort();state.jobPollController=null;}
    state.jobPollInFlight=false;
  }
  function watchJob(job,reconnect=false){
    if(!job||typeof job.id!=="string")return;
    if(!reconnect&&state.activeJob?.id===job.id&&(state.jobStream||state.pollTimer||state.jobPollInFlight)){acceptJobSnapshot(job);return;}
    const previousId=state.activeJob?.id;
    closeJobTransport();const version=++state.jobWatchVersion;
    state.activeJob=job;state.jobConnection="connecting";state.jobResultLoadingId=null;state.jobResultError=false;
    if(previousId!==job.id){state.jobCancelPending=null;state.jobCancelNotice="";state.jobFinalizedId=null;}
    renderJob();
    if(!isRunning(job)){finishJob(job,version);return;}
    if(typeof EventSource!=="function"){state.jobConnection="polling";scheduleJobPoll(0,version);renderJob();return;}
    let stream;
    try{stream=new EventSource(`/api/jobs/events?id=${encodeURIComponent(job.id)}`);state.jobStream=stream;}
    catch{state.jobConnection="polling";scheduleJobPoll(0,version);renderJob();return;}
    const current=()=>version===state.jobWatchVersion&&state.activeJob?.id===job.id&&state.jobStream===stream;
    stream.onopen=()=>{if(!current())return;state.jobConnection="live";clearTimeout(state.pollTimer);state.pollTimer=null;renderJob();};
    stream.addEventListener("job",event=>{
      if(!current())return;
      let snapshot;try{snapshot=JSON.parse(event.data);}catch{state.jobConnection="reconnecting";scheduleJobPoll(0,version);renderJob();return;}
      state.jobConnection="live";acceptJobSnapshot(snapshot,version);renderJob();
    });
    stream.onerror=()=>{if(!current()||!isRunning(state.activeJob))return;state.jobConnection="reconnecting";renderJob();scheduleJobPoll(0,version);};
  }
  function acceptJobSnapshot(job,version=state.jobWatchVersion){
    const current=state.activeJob;
    if(version!==state.jobWatchVersion||!job||job.id!==current?.id)return false;
    if(!["queued","running","cancelling","succeeded","failed","cancelled","interrupted"].includes(job.status))return false;
    if(job.account_id!==current.account_id||job.conversation_id!==current.conversation_id)return false;
    if(state.jobFinalizedId===job.id||(!isRunning(current)&&isRunning(job)))return false;
    const incomingRevision=Number.isSafeInteger(job.revision)?job.revision:null;
    const currentRevision=Number.isSafeInteger(current.revision)?current.revision:null;
    if(currentRevision!==null&&(incomingRevision===null||incomingRevision<currentRevision))return false;
    if(currentRevision!==null&&incomingRevision===currentRevision)return false;
    state.activeJob=job;
    if(isRunning(job))renderJob();else finishJob(job,version);
    return true;
  }
  function scheduleJobPoll(delay=2500,version=state.jobWatchVersion){
    clearTimeout(state.pollTimer);state.pollTimer=null;
    if(version!==state.jobWatchVersion||!isRunning(state.activeJob))return;
    state.pollTimer=setTimeout(()=>{state.pollTimer=null;pollJob(version);},delay);
  }
  async function pollJob(version=state.jobWatchVersion){
    const job=state.activeJob;
    if(version!==state.jobWatchVersion||!isRunning(job)||state.jobPollInFlight)return;
    state.jobPollInFlight=true;const controller=new AbortController();state.jobPollController=controller;
    const timeout=setTimeout(()=>controller.abort(),10000);
    try{
      const result=await api(`/api/jobs?id=${encodeURIComponent(job.id)}`,undefined,controller.signal);
      if(version!==state.jobWatchVersion||state.activeJob?.id!==job.id)return;
      acceptJobSnapshot(result.job,version);
      if(isRunning(state.activeJob)&&state.jobConnection!=="live")state.jobConnection=state.jobStream?"reconnecting":"polling";
    }catch{
      if(version!==state.jobWatchVersion||state.activeJob?.id!==job.id||!isRunning(state.activeJob))return;
      if(state.jobConnection!=="live")state.jobConnection="disconnected";
    }finally{
      clearTimeout(timeout);
      if(version===state.jobWatchVersion&&state.activeJob?.id===job.id){
        state.jobPollInFlight=false;if(state.jobPollController===controller)state.jobPollController=null;
        renderJob();if(isRunning(state.activeJob)&&state.jobConnection!=="live")scheduleJobPoll(3000,version);
      }
    }
  }
  async function finishJob(job,version){
    if(version!==state.jobWatchVersion||state.activeJob?.id!==job.id||state.jobFinalizedId===job.id)return;
    state.jobFinalizedId=job.id;closeJobTransport();state.jobConnection="terminal";state.jobCancelPending=null;state.jobCancelNotice="";
    if(job.status==="succeeded"){
      state.jobResultLoadingId=job.id;state.jobResultError=false;renderJob();
      await loadCompletedJob(job,version);
    }else{state.jobResultLoadingId=null;state.jobResultError=false;renderJob();toast(jobTerminalMessage(job),job.status!=="cancelled");}
    if(version===state.jobWatchVersion&&state.activeJob?.id===job.id&&state.page==="activity")await loadActivity(state.pageVersion);
  }
  async function loadCompletedJob(job,version=state.jobWatchVersion){
    if(version!==state.jobWatchVersion||state.activeJob?.id!==job.id||job.status!=="succeeded")return;
    state.jobResultLoadingId=job.id;state.jobResultError=false;renderJob();
    try{
      await refreshConversations();
      if(version!==state.jobWatchVersion||state.activeJob?.id!==job.id)return;
      state.draftChoices.delete(key(job));
      if(same(state.selected,job)){
        const target={...state.selected};await selectConversation(target,true);
        if(version!==state.jobWatchVersion||state.activeJob?.id!==job.id)return;
        if(same(state.selected,job)&&(!state.detail||!(state.detail.drafts||[]).some(draft=>draft.id===job.draft_id)))throw new Error("result not loaded");
      }
      toast(jobTerminalMessage(job));
    }catch{
      if(version===state.jobWatchVersion&&state.activeJob?.id===job.id){state.jobResultError=true;toast("回复已完成，但结果暂未载入。请重新读取结果。",true);}
    }finally{
      if(version===state.jobWatchVersion&&state.activeJob?.id===job.id){state.jobResultLoadingId=null;renderJob();}
    }
  }
  async function cancelGeneration(){
    const job=state.activeJob,version=state.jobWatchVersion;
    if(!isRunning(job)||job.status==="cancelling"||job.can_cancel===false||state.jobCancelPending===job.id)return;
    state.jobCancelPending=job.id;state.jobCancelNotice="";renderJob();
    try{
      const result=await api("/api/jobs/cancel",{id:job.id});
      if(version!==state.jobWatchVersion||state.activeJob?.id!==job.id)return;
      if(result.job)acceptJobSnapshot(result.job,version);
      if(isRunning(state.activeJob))pollJob(version);
    }catch(error){
      if(version!==state.jobWatchVersion||state.activeJob?.id!==job.id||!isRunning(state.activeJob))return;
      if(error.status===409){state.jobCancelNotice="停止请求未被接受，正在确认任务结果。";toast(state.jobCancelNotice);}
      else state.jobCancelNotice="停止状态尚未确认，正在重新查询任务。";
      renderJob();pollJob(version);
    }
  }
  function reconnectJob(){
    const job=state.activeJob;
    if(isRunning(job))watchJob(job,true);
    else if(job?.status==="succeeded"&&state.jobResultError&&!state.jobResultLoadingId)loadCompletedJob(job);
  }

  async function adoptReply(){const draft=currentDraft(),target=state.selected?{...state.selected}:null;if(!draft||!target||state.busyAdopt||(isRunning(state.activeJob)&&same(state.activeJob,target))||(state.activeJob?.status==="succeeded"&&same(state.activeJob,target)&&(state.jobResultLoadingId||state.jobResultError)))return;if(stale(draft)){toast("回复已过期，请重新生成后再复制采用。",true);return;}const finalText=$("reply-editor").value.trim();if(!finalText){toast("回复正文不能为空。",true);return;}state.busyAdopt=true;renderJob();let saved=false;try{const result=await api("/api/adopt",{draft_id:draft.id,final_text:finalText});saved=true;state.replyEdits.delete(draft.id);if(result.draft?.id)state.draftChoices.set(key(target),result.draft.id);let copied=false;try{if(!navigator.clipboard?.writeText)throw new Error("unavailable");await navigator.clipboard.writeText(finalText);copied=true;}catch{}toast(copied?"正文已复制，采用结果已保存。":"已采用，未能复制，请手动复制。",!copied);await refreshConversations();if(same(state.selected,target)){await selectConversation(state.selected,true);if(!copied){$("reply-editor")?.focus();$("reply-editor")?.select();}}}catch(error){toast(saved?`已保存采用结果，但刷新未完成：${error.message}`:error.message,true);}finally{state.busyAdopt=false;renderJob();}}
  function renderSync(){const sync=state.sync;$("sync-button").disabled=state.busySync||!sync?.configured;$("sync-button-label").textContent=state.busySync?"更新中…":"更新聊天";$("sync-source").textContent=sync?.source_type==="demo"?"演练聊天来源":sync?.source_type==="whatsapp_bridge"?(sync.source_label||"WhatsApp 聊天来源"):"尚未配置聊天来源";let message="请由管理员配置聊天来源";const result=sync?.last_result;if(sync?.configured){message=result?`${time(result.finished_at,true)} · ${result.status==="partial"?"部分更新":"更新完成"} · 新增 ${Number(result.inserted)||0} 条`:"尚未更新聊天";if(sync.source_type==="whatsapp_bridge"&&sync.connection_state!=="connected")message+=" · 在线状态未确认";}if(state.busySync)message="正在读取聊天记录…";$("sync-summary").textContent=message;$("sync-summary").title=result?.note||message;$("sync-summary").classList.toggle("sync-partial",result?.status==="partial");}
  async function loadSync(){try{state.sync=await api("/api/sync");renderSync();}catch(error){$("sync-source").textContent="聊天更新暂不可用";$("sync-summary").textContent=error.message;$("sync-button").disabled=true;}}
  async function updateChats(){if(state.busySync||!state.sync?.configured)return;state.busySync=true;renderSync();const previous=new Set(state.conversations.map(key));try{const result=await api("/api/sync",{});state.sync={...state.sync,last_result:result};for(const item of result.affected_conversations||[]){state.updatedScopes.add(key(item));if(!previous.has(key(item)))state.newScopes.add(key(item));}await refreshConversations(true);if(state.selected&&(result.affected_conversations||[]).some(item=>same(item,state.selected)))await selectConversation(state.selected,true);const conflicts=Array.isArray(result.conflicts)?result.conflicts.length:Number(result.conflicts)||0;toast(`${result.status==="partial"?"部分聊天已更新":"聊天已更新"}：新增 ${Number(result.inserted)||0} 条${conflicts?`，${conflicts} 条需检查`:""}。${result.note||""}`,result.status==="partial");}catch(error){toast(error.message,true);}finally{state.busySync=false;renderSync();}}
  function customersDirty() {
    const manager = state.customers;
    return manager.selected.size !== manager.baseline.size || [...manager.selected].some(id => !manager.baseline.has(id));
  }
  function filteredCustomers() {
    const search = $("customer-search").value.trim().toLocaleLowerCase(), filter = $("customer-filter").value;
    return (state.customers.snapshot?.items || []).filter(item =>
      (filter === "all" || item.status === filter) &&
      (!search || `${item.display_name || ""} ${item.identifier || ""}`.toLocaleLowerCase().includes(search)));
  }
  function customerIdentifier(item) {
    if (state.customers.snapshot?.source_type === "demo") return item.kind === "group" ? "虚构群聊" : "虚构客户";
    const identifier = String(item.identifier || "");
    if (item.kind === "group" || identifier.endsWith("@g.us")) return "群聊";
    if (/^\d+@s\.whatsapp\.net$/.test(identifier)) return "+" + identifier.split("@")[0];
    if (identifier.endsWith("@lid")) return "WhatsApp 联系人";
    return identifier || "客户会话";
  }
  function renderCustomers() {
    const manager = state.customers, snapshot = manager.snapshot, busy = manager.scanning || manager.saving;
    const focusedCustomer = document.activeElement?.dataset?.customerId, listScroll = $("customer-list").scrollTop;
    const dirty = customersDirty(), max = Number(snapshot?.max_selected) || 100;
    const ready = !!snapshot?.configured && manager.valid && !busy;
    $("customer-source").textContent = snapshot?.source_type === "demo" ? "演练来源 · 全部为虚构客户" :
      snapshot?.source_type === "whatsapp_bridge" ? `${snapshot.source_label || "WhatsApp 聊天来源"} · 在线状态未确认` :
      manager.scanning ? "正在读取聊天来源…" : "尚未连接聊天来源";
    $("customer-scanned-at").textContent = manager.scanning ? "正在重新扫描会话名单…" :
      snapshot?.scanned_at ? `${manager.valid ? "本次扫描" : "上次成功扫描"}：${time(snapshot.scanned_at, true)}` : "尚未完成扫描";
    $("customer-source-note").textContent = snapshot?.source_type === "demo" ? "可用虚构客户练习添加、暂停和恢复，不会读取真实聊天。" :
      snapshot?.configured ? "名称跟随 WhatsApp，修改后重新扫描或更新聊天。只列出来源已收到的会话，扫描不会导入消息。" :
      "首次使用需由管理员连接获准的聊天账号。连接完成后，点击重新扫描即可添加客户。";
    $("customer-rescan").disabled = busy;
    $("customer-rescan").textContent = manager.scanning ? "扫描中…" : "重新扫描";
    $("customer-search").disabled = !ready; $("customer-filter").disabled = !ready;
    $("customer-save").disabled = !ready || !dirty || manager.selected.size > max || !!manager.discardAction;
    $("customer-save").textContent = manager.saving ? "保存中…" : "保存选择";
    $("customer-close").disabled = busy; $("customer-cancel").disabled = busy;
    $("customer-error").hidden = !manager.error; $("customer-error").textContent = manager.error;
    $("customer-list").setAttribute("aria-busy", String(manager.scanning));
    if (manager.scanning) $("customer-list").innerHTML = loading("正在扫描最新客户会话…");
    else if (!manager.valid && manager.error) $("customer-list").innerHTML = empty("本次扫描尚未确认", "请重新扫描后再选择客户。上次的列表不会作为最新结果保存。");
    else if (!snapshot?.configured) $("customer-list").innerHTML = empty("先连接聊天来源", "请管理员完成首次连接，然后在这里重新扫描。");
    else if (!snapshot.items.length) $("customer-list").innerHTML = empty("暂时没有可添加的会话", "聊天来源接收到客户会话后，再点击重新扫描。");
    else {
      const items = filteredCustomers();
      $("customer-list").innerHTML = items.length ? items.map(item => {
        const selected = manager.selected.has(item.id), unavailable = item.available === false;
        const disabled = !ready || !!manager.discardAction || (!selected && ((unavailable && !manager.baseline.has(item.id)) || manager.selected.size >= max));
        const status = { new: "未加入", active: "已加入", paused: "已暂停" }[item.status] || "未加入";
        const identifier = customerIdentifier(item), name = item.display_name && item.display_name !== item.identifier ? item.display_name : identifier;
        return `<label class="customer-choice ${selected ? "is-selected" : ""} ${unavailable ? "is-unavailable" : ""}"><input type="checkbox" data-customer-id="${escape(item.id)}" ${selected ? "checked" : ""} ${disabled ? "disabled" : ""}><span class="customer-choice-copy"><strong>${escape(name)}</strong><span>${escape(identifier)}</span><small>${unavailable ? "当前来源未找到 · 已有历史保留" : item.last_message_at ? `最近会话 ${escape(time(item.last_message_at, true))}` : "暂无会话时间"}</small></span><span class="customer-choice-tags">${item.kind === "group" ? '<span class="badge customer-group">群聊</span>' : ""}<span class="badge customer-status-${escape(item.status)}">${status}</span></span></label>`;
      }).join("") : empty("没有匹配的客户", "换个名称、号码或筛选条件，已有勾选会保留。");
    }
    $("customer-list").scrollTop = listScroll;
    if (focusedCustomer) {
      for (const checkbox of $("customer-list").querySelectorAll("[data-customer-id]")) {
        if (checkbox.dataset.customerId === focusedCustomer && !checkbox.disabled) { checkbox.focus({ preventScroll: true }); break; }
      }
    }
    let added = 0, paused = 0, restored = 0;
    for (const item of snapshot?.items || []) {
      if (manager.selected.has(item.id) && !manager.baseline.has(item.id)) item.status === "paused" ? restored++ : added++;
      if (!manager.selected.has(item.id) && manager.baseline.has(item.id)) paused++;
    }
    $("customer-selected-count").textContent = `已选 ${manager.selected.size} / ${max} 位`;
    $("customer-change-count").textContent = dirty ? `待新增 ${added} · 暂停 ${paused} · 恢复 ${restored}` :
      manager.saved ? "选择已保存 · 再次修改请重新扫描" : "新客户默认不选，可按需要勾选";
    $("customer-discard").hidden = !manager.discardAction;
    $("customer-discard-message").textContent = manager.discardAction === "scan" ? "重新扫描会载入已保存的选择，尚未保存的修改将被放弃。" : "关闭后将放弃本次尚未保存的勾选修改。";
    $("customer-discard-confirm").textContent = manager.discardAction === "scan" ? "放弃修改并扫描" : "放弃修改并关闭";
    $("customer-result-panel").hidden = !manager.saved || dirty || busy;
    $("customer-result").textContent = manager.selected.size ? "客户选择已保存，尚未更新聊天。点击右侧按钮读取已选择客户的记录。" : "已暂停全部客户的后续同步，已有历史聊天和回复保留。";
    $("customer-update").hidden = !manager.saved || dirty || !manager.selected.size;
    $("customer-update").disabled = busy || state.busySync;
  }
  function toggleCustomer(id, selected) {
    const manager = state.customers, item = manager.snapshot?.items.find(item => item.id === id);
    if (!item || !manager.valid || manager.scanning || manager.saving || manager.discardAction) return;
    if (selected && ((item.available === false && !manager.baseline.has(id)) || manager.selected.size >= (Number(manager.snapshot.max_selected) || 100))) { renderCustomers(); return; }
    if (selected) manager.selected.add(id); else manager.selected.delete(id);
    manager.saved = false; renderCustomers();
  }
  async function openCustomers() {
    if (!state.bootstrap || $("customer-dialog").open) return;
    const manager = state.customers;
    manager.discardAction = null; manager.saved = false;
    $("customer-search").value = ""; $("customer-filter").value = "all";
    $("customer-dialog").showModal(); await scanCustomers();
  }
  async function scanCustomers() {
    const manager = state.customers;
    if (manager.scanning || manager.saving) return;
    manager.scanning = true; manager.valid = false; manager.error = ""; manager.saved = false; manager.discardAction = null;
    renderCustomers();
    try {
      const snapshot = await api("/api/customers/scan", {});
      manager.snapshot = snapshot; manager.selected = new Set((snapshot.items || []).filter(item => item.selected).map(item => item.id));
      manager.baseline = new Set(manager.selected); manager.valid = !!snapshot.configured && !!snapshot.scan_id;
      try { await refreshConversations(); }
      catch { toast("扫描已完成，主列表名称暂未刷新，请点击列表刷新。", true); }
    } catch (error) { manager.error = error.message; }
    finally { manager.scanning = false; renderCustomers(); }
  }
  function requestCustomerRescan() {
    const manager = state.customers;
    if (manager.scanning || manager.saving) return;
    if (customersDirty()) { manager.discardAction = "scan"; renderCustomers(); $("customer-keep").focus(); return; }
    return scanCustomers();
  }
  function closeCustomers() {
    const manager = state.customers;
    if (manager.scanning || manager.saving) return;
    if (customersDirty()) { manager.discardAction = "close"; renderCustomers(); $("customer-keep").focus(); return; }
    $("customer-dialog").close();
  }
  function resolveCustomerDiscard(discard) {
    const manager = state.customers, action = manager.discardAction;
    manager.discardAction = null;
    if (!discard) { renderCustomers(); return; }
    manager.selected = new Set(manager.baseline);
    if (action === "scan") return scanCustomers();
    if (action === "close") $("customer-dialog").close();
    renderCustomers();
  }
  async function saveCustomers() {
    const manager = state.customers;
    if (manager.scanning || manager.saving || !manager.valid || !manager.snapshot?.configured || !customersDirty() || manager.discardAction) return;
    if (manager.selected.size > (Number(manager.snapshot.max_selected) || 100)) return;
    manager.saving = true; manager.error = ""; renderCustomers();
    try {
      const result = await api("/api/customers/selection", { scan_id: manager.snapshot.scan_id, selected_ids: [...manager.selected] });
      manager.snapshot = result; manager.selected = new Set((result.items || []).filter(item => item.selected).map(item => item.id));
      manager.baseline = new Set(manager.selected); manager.valid = !!result.configured && !!result.scan_id; manager.saved = true;
      toast("客户选择已保存；更新聊天后可查看新客户的消息。");
      await loadSync();
      try { await refreshConversations(); } catch { toast("客户选择已保存，客户列表暂未刷新，请点击列表刷新。", true); }
    } catch (error) {
      manager.valid = false; manager.error = `${error.message} 请重新扫描以确认当前已保存的选择。`;
    } finally { manager.saving = false; renderCustomers(); }
  }
  function openMore(){$("simulation-open").hidden=state.bootstrap?.company.mode!=="simulation";$("simulation-open").disabled=!state.selected;$("more-dialog").showModal();}
  function openMessage(){$("more-dialog").close();if(!state.selected||state.bootstrap.company.mode!=="simulation")return;state.messageTarget={...state.selected};$("message-target").textContent=`添加到 ${state.selected.title} 的演练会话，不会向客户发送。`;$("message-body").value=state.messageEdits.get(key(state.messageTarget))||"";formError("message-error");$("message-dialog").showModal();}
  async function addMessage(event){event.preventDefault();const target=state.messageTarget;if(!target||state.busyMessage)return;const body=$("message-body").value.trim();if(!body)return;state.busyMessage=true;$("message-submit").disabled=true;formError("message-error");try{await api("/api/messages",{account_id:target.account_id,conversation_id:target.conversation_id,body});state.messageEdits.delete(key(target));$("message-dialog").close();toast("演练消息已添加，旧回复需要重新生成。");await refreshConversations();if(same(state.selected,target))await selectConversation(state.selected);}catch(error){formError("message-error",error.message);}finally{state.busyMessage=false;$("message-submit").disabled=false;}}
  function replyPayload() {
    return { text: $("strategy-text").value, required_fields: [...state.replySettings.fields], rules: [...state.replySettings.rules] };
  }
  function replySettingsDirty() {
    return state.replySettings.loaded && (JSON.stringify(replyPayload()) !== state.replySettings.baseline || !!$("reply-custom-field").value.trim());
  }
  const replySettingsLocked = () => state.replySettings.loading || state.replySettings.saving || !!state.replySettings.confirmAction || !state.replySettings.loaded;
  const replyFieldKeys = () => [...Object.keys(fieldLabels), ...state.replySettings.customFields];
  function renderReplySettingsControls() {
    const editor = state.replySettings, locked = replySettingsLocked();
    for (const id of ["strategy-text", "reply-custom-field", "reply-field-add", "reply-rule-add"]) $(id).disabled = locked;
    replyFieldKeys().forEach((key, index) => { if ($("reply-field-" + index)) $("reply-field-" + index).disabled = locked; });
    editor.rules.forEach((rule, index) => { $("reply-rule-" + index).disabled = locked; $("reply-rule-remove-" + index).disabled = locked; });
    $("strategy-submit").disabled = locked || editor.conflict;
    $("strategy-submit").textContent = editor.saving ? "正在保存…" : "保存回复设置";
    $("reply-settings-reload").disabled = editor.loading || editor.saving || !!editor.confirmAction;
    $("strategy-state").textContent = editor.loading ? "正在读取设置…" : editor.saving ? "正在保存，请稍候。" : editor.conflict ? "其他页面已更新，请重新载入后编辑。" : !editor.loaded ? "未能读取设置，请重新载入。" : replySettingsDirty() ? "有未保存的修改" : "当前设置已保存";
  }
  function renderReplyFields() {
    const keys = replyFieldKeys();
    $("reply-required-fields").innerHTML = keys.map((key, index) => `<label class="reply-field-choice"><input type="checkbox" id="reply-field-${index}"><span>${escape(label(key))}${Object.hasOwn(fieldLabels, key) ? "" : '<small>自定义</small>'}</span></label>`).join("");
    keys.forEach((key, index) => {
      const input = $("reply-field-" + index); input.dataset.requiredField = key; input.checked = state.replySettings.fields.includes(key);
      input.addEventListener("change", () => {
        if (replySettingsLocked()) { input.checked = state.replySettings.fields.includes(key); return; }
        if (input.checked && !state.replySettings.fields.includes(key)) state.replySettings.fields.push(key);
        else if (!input.checked) state.replySettings.fields = state.replySettings.fields.filter(field => field !== key);
        renderReplySettingsControls();
      });
    });
  }
  function renderReplyRules() {
    $("reply-rules").innerHTML = state.replySettings.rules.map((rule, index) => `<div class="reply-rule-row"><div><label class="form-label" for="reply-rule-${index}">确认事项 ${index + 1}</label><textarea class="text-input" id="reply-rule-${index}" rows="2" placeholder="例如：特殊包装需确认后再报价"></textarea></div><button class="button secondary small" type="button" id="reply-rule-remove-${index}" aria-label="移除确认事项 ${index + 1}">移除</button></div>`).join("") || '<p class="reply-rules-empty">暂未添加额外确认事项，可按实际业务补充。</p>';
    state.replySettings.rules.forEach((rule, index) => {
      $("reply-rule-" + index).value = rule;
      $("reply-rule-" + index).addEventListener("input", event => { if (!replySettingsLocked()) { state.replySettings.rules[index] = event.target.value; renderReplySettingsControls(); } });
      $("reply-rule-remove-" + index).addEventListener("click", () => { if (replySettingsLocked()) return; state.replySettings.rules.splice(index, 1); renderReplyRules(); renderReplySettingsControls(); });
    });
  }
  function applyReplySettings(data) {
    const editor = state.replySettings;
    editor.fields = [...data.required_fields]; editor.customFields = data.required_fields.filter(key => !Object.hasOwn(fieldLabels, key)); editor.rules = [...data.rules];
    $("strategy-text").value = data.text; $("reply-custom-field").value = "";
    editor.release = data.active_release; editor.loaded = true; editor.conflict = false;
    editor.baseline = JSON.stringify(replyPayload()); renderReplyFields(); renderReplyRules();
  }
  function restoreReplyBaseline() {
    if (state.replySettings.baseline) applyReplySettings({ ...JSON.parse(state.replySettings.baseline), active_release: state.replySettings.release });
    renderReplySettingsControls();
  }
  async function loadStrategy({ force = false } = {}) {
    const editor = state.replySettings;
    if (editor.loading || editor.saving || (replySettingsDirty() && !force)) return;
    const version = ++editor.version; editor.loading = true; formError("strategy-error"); renderReplySettingsControls();
    try {
      const data = await api("/api/reply-settings");
      if (version !== editor.version || state.page !== "strategy") return;
      if (typeof data.text !== "string" || !Array.isArray(data.required_fields) || !data.required_fields.every(item => typeof item === "string") || !Array.isArray(data.rules) || !data.rules.every(item => typeof item === "string") || !data.active_release) throw new Error("回复设置格式不完整，请重新载入。");
      applyReplySettings(data);
    } catch (error) { if (version === editor.version) formError("strategy-error", error.message + (editor.loaded ? " 当前输入已保留。" : "")); }
    finally { if (version === editor.version) { editor.loading = false; renderReplySettingsControls(); } }
  }
  function confirmReplyDiscard(message, action) {
    if (state.replySettings.saving || state.replySettings.loading) return;
    state.replySettings.confirmAction = action; $("reply-discard-message").textContent = message;
    renderReplySettingsControls(); if (!$("reply-settings-discard").open) $("reply-settings-discard").showModal(); $("reply-discard-keep").focus();
  }
  async function resolveReplyDiscard(accept) {
    const action = state.replySettings.confirmAction;
    state.replySettings.confirmAction = null; $("reply-settings-discard").close(); renderReplySettingsControls();
    if (accept && action) await action();
  }
  function reloadReplySettings() {
    if (state.replySettings.loading || state.replySettings.saving || state.replySettings.confirmAction) return;
    if (replySettingsDirty()) confirmReplyDiscard("重新载入会放弃尚未保存的回复设置。需要保留的内容请先复制。", () => loadStrategy({ force: true }));
    else return loadStrategy({ force: true });
  }
  function addReplyField() {
    if (replySettingsLocked()) return;
    const value = $("reply-custom-field").value.trim();
    if (!value) { formError("strategy-error", "先填写需要补充的问题名称。"); return; }
    const matched = Object.entries(fieldLabels).find(([key, name]) => value === key || value === name);
    const key = matched ? matched[0] : value;
    if (!Object.hasOwn(fieldLabels, key) && !state.replySettings.customFields.includes(key)) state.replySettings.customFields.push(key);
    if (!state.replySettings.fields.includes(key)) state.replySettings.fields.push(key);
    $("reply-custom-field").value = ""; formError("strategy-error"); renderReplyFields(); renderReplySettingsControls();
  }
  async function saveStrategy(event) {
    event.preventDefault(); const editor = state.replySettings;
    if (replySettingsLocked() || editor.conflict) return;
    formError("strategy-error"); const payload = replyPayload();
    if ($("reply-custom-field").value.trim()) { formError("strategy-error", "还有未添加的自定义问题，请先点击添加或清空输入。"); return; }
    if (!payload.required_fields.length) { formError("strategy-error", "至少勾选一项需要了解的信息。"); return; }
    if (payload.rules.some(rule => !rule.trim())) { formError("strategy-error", "确认事项不能为空，请填写内容或移除空白项。"); return; }
    if (new Set(payload.rules).size !== payload.rules.length) { formError("strategy-error", "确认事项有重复内容，请合并后保存。"); return; }
    editor.saving = true; renderReplySettingsControls(); let result;
    try { result = await api("/api/reply-settings", { ...payload, base_release: editor.release }); }
    catch (error) { editor.conflict = error.status === 409 || /其他页面更新|版本冲突/.test(error.message); formError("strategy-error", error.message + " 当前输入已保留。"); return; }
    finally { editor.saving = false; renderReplySettingsControls(); }
    editor.release = result.release_id || result.active_release || editor.release; editor.baseline = JSON.stringify(payload); renderReplySettingsControls();
    toast(result.changed === false ? "设置没有变化，当前版本已保留。" : "回复设置已保存，已有回复需重新核查。");
    try { await refreshConversations(); if (state.selected) await selectConversation(state.selected, true); }
    catch (error) { toast(`回复设置已保存，但页面刷新未完成。请重新载入工作台。${error.message}`, true); }
  }
  function validity(document){const today=state.bootstrap?.as_of||new Date().toISOString().slice(0,10);if(document.valid_until<today)return["expired","已过期"];if(document.valid_from>today)return["fixture","尚未生效"];return["valid","当前有效"];}
  async function loadKnowledge(version = state.pageVersion) {
    $("knowledge-content").innerHTML = loading("读取企业资料中");
    try {
      const data = await api("/api/knowledge");
      state.knowledge = data;
      if (state.page !== "knowledge" || version !== state.pageVersion) return;
      const config = data.config;
      const docs = config.knowledge || [];
      const history = data.history || [];
      $("knowledge-content").innerHTML = `<div class="knowledge-document-grid">${docs.length ? docs.map((document, index) => {
        const [className, text] = validity(document);
        const content = String(document.content || ""), summary = content.length > 200 ? content.slice(0, 200) + "…" : content;
        return `<article class="content-card"><div class="knowledge-card-head"><h3>${escape(document.title)}</h3><span class="badge ${className}">${text}</span></div><p class="knowledge-copy knowledge-summary">${escape(summary)}</p><div class="knowledge-validity"><span>${escape(document.valid_from)} — ${escape(document.valid_until)}</span></div><div class="knowledge-card-actions"><button class="button secondary small" id="knowledge-edit-${index}">编辑资料</button></div></article>`;
      }).join("") : empty("还没有企业资料", "点击右上方“新增资料”，从一份已确认的产品或服务资料开始。")}</div><details class="knowledge-history"><summary>资料更新记录 · ${history.length} 次</summary><ul class="release-list">${history.slice().reverse().slice(0, 10).map(item => `<li><strong>${escape(item.action === "rollback" ? "恢复历史资料" : "更新资料")}</strong> · ${escape(time(item.published_at || item.created_at || item.at || item.timestamp, true))}<br>版本 ${escape(String(item.release_id || item.release || "").slice(0, 10) || "当前版本")}</li>`).join("") || '<li>当前资料已加载</li>'}</ul></details>`;
      docs.forEach((document, index) => $("knowledge-edit-" + index).addEventListener("click", () => openKnowledge({ documentId: document.id })));
    } catch (error) {
      if (state.page === "knowledge" && version === state.pageVersion) {
        $("knowledge-content").innerHTML = empty("无法读取企业知识", error.message, '<button class="button secondary small" id="retry-knowledge">重新载入</button>');
        $("retry-knowledge").addEventListener("click", () => loadKnowledge());
      }
    }
  }
  async function loadActivity(version = state.pageVersion) {
    $("activity-content").innerHTML = loading("读取运行记录中");
    try {
      const [data, sync] = await Promise.all([api("/api/activity"), api("/api/sync").catch(() => state.sync)]);
      if (sync && !state.busySync) { state.sync = sync; renderSync(); }
      if (state.page !== "activity" || version !== state.pageVersion) return;
      if (isRunning(data.active_job) && !isRunning(state.activeJob)) watchJob(data.active_job);
      const health = data.health || {};
      const runs = data.runs || [];
      const imports = data.imports || [];
      const outbox = data.outbox || [];
      const source = state.sync;
      const coverage = source?.last_result?.coverage || {};
      const coverageText = coverage.source_first_at && coverage.source_last_at ? `${String(coverage.source_first_at).slice(0,10)} 至 ${String(coverage.source_last_at).slice(0,10)}` : "尚无可确认的聊天日期范围";
      const syncRecord = `<section class="content-card"><div class="card-heading"><h3>聊天更新</h3><span>${escape(source?.source_label || (source?.source_type === "demo" ? "固定演练来源" : "尚未配置"))}</span></div><p class="knowledge-copy">已覆盖聊天日期：${escape(coverageText)}</p><p class="reply-note">${source?.source_type === "demo" ? "这是固定的虚构演练记录。" : "范围仅表示当前来源可读到的记录，不代表客户完整聊天历史。"}</p><p class="reply-note">最近更新：${escape(time(source?.last_result?.finished_at,true))}${source?.last_result?.note ? " · " + escape(source.last_result.note) : ""}</p></section>`;
      const row = (cells) => `<tr>${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`;
      $("activity-content").innerHTML = `<div class="health-grid"><section class="health-card"><span class="health-card-label">工作区状态</span><div class="health-card-value"><span class="small-dot ${health.ok ? "teal" : ""}"></span>${health.ok ? "正常" : "需要检查"}</div><p>${health.ok ? "企业资料与聊天数据检查通过" : "请检查企业配置与数据库状态"}</p></section><section class="health-card"><span class="health-card-label">消息渠道</span><div class="health-card-value">${source?.source_type === "demo" ? "演练聊天" : source?.configured ? "已配置读取" : "尚未配置"}</div><p>${source?.source_type === "whatsapp_bridge" ? "当前仅读取指定联系人的来源记录" : source?.source_type === "demo" ? "虚构记录，可演练更新流程" : "可由管理员配置来源，或导入文件"}</p></section><section class="health-card"><span class="health-card-label">自动发送</span><div class="health-card-value">未启用</div><p>采用仅保存结果，不执行客户发送</p></section></div>${syncRecord}<section class="content-card"><div class="card-heading"><h3>生成与润色记录</h3><span>最近 ${runs.length} 次 · 供负责人排查</span></div><div class="table-scroll"><table class="activity-table"><thead><tr><th>开始时间</th><th>分析方式</th><th>请求模型</th><th>输入消息</th><th>耗时</th><th>结果</th></tr></thead><tbody>${runs.map((run) => row([escape(time(run.started_at, true)), escape(run.runner === "demo_fixture" ? "示例数据" : run.runner || "未记录"), escape(run.requested_model || "型号未记录"), `${Number(run.input_messages) || 0} 条`, run.duration_ms == null ? "进行中" : `${Math.round(Number(run.duration_ms) / 1000)} 秒`, `<span class="badge ${run.status === "succeeded" ? "valid" : ["failed", "interrupted"].includes(run.status) ? "expired" : run.status === "cancelled" ? "fixture" : "pending"}">${escape(({succeeded:"已完成", failed:"未完成", cancelled:"已停止", interrupted:"已中断", cancelling:"停止中", queued:"等待处理", running:"处理中"})[run.status] || "处理中")}</span>${run.error_category ? `<br>${escape(({cancelled:"本次已停止",interrupted:"本次生成中断",timeout:"处理超时"})[run.error_category] || "未完成，请检查工作台后重试")}` : ""}`])).join("") || '<tr><td colspan="6" class="no-records">还没有真实分析记录。预置示例不会计为模型运行。</td></tr>'}</tbody></table></div></section><section class="content-card"><div class="card-heading"><h3>聊天导入</h3><span>最近 ${imports.length} 批</span></div><div class="table-scroll"><table class="activity-table"><thead><tr><th>导入时间</th><th>来源</th><th>记录数</th><th>新增</th><th>重复跳过</th></tr></thead><tbody>${imports.map((item) => row([escape(time(item.imported_at, true)), escape(item.source_label || item.source || "文件导入"), escape(item.record_count ?? "—"), escape(item.inserted ?? "—"), escape(item.duplicates ?? 0)])).join("") || '<tr><td colspan="5" class="no-records">尚未导入聊天记录</td></tr>'}</tbody></table></div></section><section class="content-card"><div class="card-heading"><h3>已采用回复</h3><span>${outbox.length} 条 · 工作台未执行发送</span></div>${outbox.map((item) => `<div class="outbox-card"><p>${escape(item.final_text || "已保存审核记录")}</p><span class="badge ${item.requires_recheck ? "stale" : "approved"}">${item.requires_recheck ? "需重新核查" : "已保存采用结果"}</span></div>`).join("") || '<p class="reply-note">复制并采用回复后，结果会保存在这里。</p>'}</section>`;
    } catch (error) {
      if (state.page === "activity" && version === state.pageVersion) {
        $("activity-content").innerHTML = empty("无法读取运行记录", error.message, '<button class="button secondary small" id="retry-activity">重新载入</button>');
        $("retry-activity").addEventListener("click", () => loadActivity());
      }
    }
  }
  function openImport() {
    if (!state.bootstrap) { toast("工作区尚未连接，请先刷新页面。", true); return; }
    formError("import-error");
    $("import-dialog").showModal();
  }
  async function readJSONFile(file) {
    if (!file) throw new Error("请先选择一个 JSON 文件。");
    if (file.size > 5 * 1024 * 1024) throw new Error("文件超过 5 MB，请分批导入。");
    try { return JSON.parse(await file.text()); } catch { throw new Error("文件不是有效的 JSON，请检查内容后重新选择。"); }
  }
  async function importRecords(event) {
    event.preventDefault();
    formError("import-error");
    $("import-submit").disabled = true;
    $("import-submit").textContent = "正在导入…";
    try {
      const data = await readJSONFile($("import-file").files[0]);
      const records = Array.isArray(data) ? data : data.records;
      if (!Array.isArray(records) || !records.length) throw new Error("文件需要是非空消息数组，或包含 records 消息数组的对象。");
      if (records.length > 2000) throw new Error("单次最多导入 2,000 条消息，请拆分文件。");
      const source = $("import-source").value.trim();
      if (!source) throw new Error("请填写便于识别的来源名称。");
      const result = await api("/api/import", { records, source });
      $("import-dialog").close();
      $("import-form").reset();
      toast(`导入完成：新增 ${result.inserted ?? 0} 条，重复跳过 ${result.duplicates ?? 0} 条。`);
      await refreshConversations(true);
      if (state.selected) await selectConversation(state.selected, true);
      if (state.page === "activity") await loadActivity(state.pageVersion);
    } catch (error) { formError("import-error", error.message); }
    finally { $("import-submit").disabled = false; $("import-submit").textContent = "导入记录"; }
  }
  const knowledgeBusy = () => state.knowledgeEditor.loading || state.knowledgeEditor.saving;
  function knowledgeDirty() {
    if (!$("knowledge-dialog").open) return false;
    try { return JSON.stringify(JSON.parse($("knowledge-json").value)) !== state.knowledgeEditor.baseline; }
    catch { return true; }
  }
  function renderKnowledgeControls() {
    const editor = state.knowledgeEditor, locked = knowledgeBusy() || !!editor.confirmAction;
    for (const id of ["knowledge-document", "knowledge-add", "knowledge-remove", "knowledge-title", "knowledge-content-editor", "knowledge-version", "knowledge-from", "knowledge-until", "knowledge-json", "knowledge-file", "knowledge-validate", "knowledge-reload"]) $(id).disabled = locked;
    for (const id of ["knowledge-advanced-open", "knowledge-add-page", "knowledge-close", "knowledge-cancel"]) $(id).disabled = knowledgeBusy();
    $("knowledge-submit").disabled = locked || editor.conflict;
    $("knowledge-submit").textContent = editor.saving ? "正在保存…" : "保存并发布";
    $("knowledge-confirm").hidden = !editor.confirmAction;
    $("knowledge-reload").hidden = !editor.conflict;
    $("knowledge-dirty").textContent = editor.loading ? "正在读取资料…" : editor.saving ? "正在校验并保存，请稍候。" : knowledgeDirty() ? "有未保存的修改 · 保存后，已有回复需重新核查。" : "当前资料已保存。";
  }
  function editableKnowledge() {
    let config;
    try { config = JSON.parse($("knowledge-json").value); }
    catch { throw new Error("高级配置中的 JSON 格式有误，请修正后继续。输入内容已保留。"); }
    if (!config || typeof config !== "object" || Array.isArray(config) || !Array.isArray(config.knowledge)) throw new Error("知识配置需要包含 knowledge 资料列表，请检查高级配置。");
    return config;
  }
  async function readKnowledgeEditor({ add = false, documentId = null, advanced = state.knowledgeEditor.advanced } = {}) {
    if (knowledgeBusy()) return;
    state.knowledgeEditor.loading = true; renderKnowledgeControls();
    try {
      const data = await api("/api/knowledge");
      // This baseline belongs to the editor, independently of background page refreshes.
      state.knowledgeEditor.baseline = JSON.stringify(data.config);
      state.knowledgeEditor.release = data.active_release;
      state.knowledgeEditor.conflict = false;
      state.knowledgeEditor.advanced = !!advanced;
      $("knowledge-advanced").hidden = !advanced; $("knowledge-advanced").open = !!advanced;
      $("knowledge-dialog-title").textContent = advanced ? "高级资料维护" : "维护企业资料";
      $("knowledge-json").value = JSON.stringify(data.config, null, 2);
      const index = documentId ? data.config.knowledge.findIndex(item => item.id === documentId) : 0;
      populateKnowledgeFields(Math.max(0, index));
      if (documentId && index < 0) toast("这份资料已被其他页面移除，已载入当前最新资料。", true);
      $("knowledge-file").value = "";
      $("knowledge-validation").textContent = "";
      formError("knowledge-error");
      if (!$("knowledge-dialog").open) $("knowledge-dialog").showModal();
    } catch (error) {
      if ($("knowledge-dialog").open) formError("knowledge-error", `未能重新载入，当前输入已保留。${error.message}`);
      else toast(error.message, true);
      return;
    } finally { state.knowledgeEditor.loading = false; renderKnowledgeControls(); }
    if (add) addKnowledgeDocument();
  }
  async function openKnowledge(options = {}) {
    if ($("knowledge-dialog").open || knowledgeBusy()) return;
    if (state.replySettings.loading || state.replySettings.saving) { toast("正在读取或保存回复设置，请稍候。"); return; }
    if (replySettingsDirty()) { confirmReplyDiscard("打开资料维护将放弃尚未保存的回复设置。", () => { restoreReplyBaseline(); return openKnowledge(options); }); return; }
    await readKnowledgeEditor({ advanced: false, ...options });
  }
  function populateKnowledgeFields(selectedIndex = null) {
    let config;
    try { config = editableKnowledge(); } catch { $("knowledge-fields").hidden = true; $("knowledge-empty").hidden = true; $("knowledge-document").innerHTML = '<option value="">请先修正高级配置</option>'; renderKnowledgeControls(); return; }
    const documents = config.knowledge;
    // A native select drops values that have no option yet. Capture the desired
    // index separately, then assign it only after rebuilding the option list.
    const previous = selectedIndex === null ? $("knowledge-document").value : String(selectedIndex);
    $("knowledge-document").innerHTML = documents.map((document, index) => `<option value="${index}">${escape(document?.title || `未命名资料 ${index + 1}`)}</option>`).join("") || '<option value="">暂无资料，请点击新增</option>';
    $("knowledge-document").value = previous !== "" && Number(previous) < documents.length ? previous : documents.length ? "0" : "";
    $("knowledge-fields").hidden = !documents.length;
    $("knowledge-empty").hidden = !!documents.length;
    loadKnowledgeFields();
    renderKnowledgeControls();
  }
  function loadKnowledgeFields() {
    let config;
    try { config = editableKnowledge(); } catch { return; }
    const item = config.knowledge?.[Number($("knowledge-document").value)];
    if (!item || typeof item !== "object" || Array.isArray(item)) { $("knowledge-fields").hidden = true; return; }
    $("knowledge-fields").hidden = false;
    $("knowledge-title").value = item.title || "";
    $("knowledge-content-editor").value = item.content || "";
    $("knowledge-version").value = item.version || "";
    $("knowledge-from").value = item.valid_from || "";
    $("knowledge-until").value = item.valid_until || "";
  }
  function updateKnowledgeFields() {
    if (knowledgeBusy() || state.knowledgeEditor.confirmAction) return;
    let config;
    try { config = editableKnowledge(); } catch (error) { formError("knowledge-error", error.message); return; }
    const item = config.knowledge?.[Number($("knowledge-document").value)];
    if (!item) return;
    item.title = $("knowledge-title").value;
    item.content = $("knowledge-content-editor").value;
    item.version = $("knowledge-version").value;
    item.valid_from = $("knowledge-from").value;
    item.valid_until = $("knowledge-until").value;
    $("knowledge-json").value = JSON.stringify(config, null, 2);
    $("knowledge-validation").textContent = "";
    const selected = $("knowledge-document").value;
    $("knowledge-document").innerHTML = config.knowledge.map((document, index) => `<option value="${index}">${escape(document?.title || `未命名资料 ${index + 1}`)}</option>`).join("");
    $("knowledge-document").value = selected;
    renderKnowledgeControls();
  }
  function addKnowledgeDocument() {
    if (knowledgeBusy() || state.knowledgeEditor.confirmAction) return;
    try {
      const config = editableKnowledge();
      let id;
      do { id = "doc-" + (globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`); } while (config.knowledge.some(item => item?.id === id));
      config.knowledge.push({ id, version: "1", title: "", content: "", valid_from: "", valid_until: "" });
      $("knowledge-json").value = JSON.stringify(config, null, 2);
      populateKnowledgeFields(config.knowledge.length - 1); formError("knowledge-error"); $("knowledge-validation").textContent = "";
      $("knowledge-title").focus();
    } catch (error) { formError("knowledge-error", error.message); }
  }
  function confirmKnowledge(message, label, action) {
    state.knowledgeEditor.confirmAction = action;
    $("knowledge-confirm-message").textContent = message;
    $("knowledge-confirm-accept").textContent = label;
    renderKnowledgeControls(); $("knowledge-confirm-keep").focus();
  }
  async function resolveKnowledgeConfirm(accept) {
    if (knowledgeBusy()) return;
    const action = state.knowledgeEditor.confirmAction;
    state.knowledgeEditor.confirmAction = null; renderKnowledgeControls();
    if (accept && action) await action();
  }
  function removeKnowledgeDocument() {
    if (knowledgeBusy() || state.knowledgeEditor.confirmAction) return;
    try {
      const config = editableKnowledge(), index = Number($("knowledge-document").value), item = config.knowledge[index];
      if (!item) return;
      confirmKnowledge(`移除“${item.title || "未命名资料"}”？保存发布后将不再用于新回复，历史版本和旧回复依据会保留。其他资料的未保存修改不受影响。`, "确认移除", () => {
        config.knowledge.splice(index, 1);
        $("knowledge-json").value = JSON.stringify(config, null, 2);
        populateKnowledgeFields(Math.max(0, index - 1)); $("knowledge-validation").textContent = ""; formError("knowledge-error");
      });
    } catch (error) { formError("knowledge-error", error.message); }
  }
  function closeKnowledge() {
    if (knowledgeBusy()) return;
    const close = () => { state.knowledgeEditor.confirmAction = null; $("knowledge-dialog").close(); renderKnowledgeControls(); };
    if (knowledgeDirty()) confirmKnowledge("当前资料有未保存的修改。关闭将放弃这些修改，已发布的资料不受影响。", "放弃修改并关闭", close);
    else close();
  }
  function reloadKnowledgeEditor() {
    if (knowledgeBusy() || state.knowledgeEditor.confirmAction) return;
    if (knowledgeDirty()) confirmKnowledge("重新载入会放弃当前未保存的修改。需要保留的内容请先复制，再读取其他页面发布的最新资料。", "放弃修改并载入", () => readKnowledgeEditor());
    else return readKnowledgeEditor();
  }
  function validateKnowledge() {
    let config;
    try { config = JSON.parse($("knowledge-json").value); } catch { throw new Error("JSON 格式有误，请检查引号、逗号与括号。"); }
    if (!config || typeof config !== "object" || Array.isArray(config)) throw new Error("知识配置必须是 JSON 对象。");
    if (config.id !== state.bootstrap.company.id) throw new Error("配置的企业 ID 与当前工作区不一致。");
    if (config.mode !== state.bootstrap.company.mode) throw new Error("配置的数据模式与当前工作区不一致。");
    if (!Array.isArray(config.rules) || !Array.isArray(config.knowledge) || !Array.isArray(config.required_fields)) throw new Error("配置必须包含业务规则、知识资料与必备字段列表。");
    const ids = new Set();
    const validDate = value => typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) && value.slice(0, 4) !== "0000" && !Number.isNaN(Date.parse(value + "T00:00:00Z")) && new Date(value + "T00:00:00Z").toISOString().slice(0, 10) === value;
    config.knowledge.forEach((item, index) => {
      let issue = "";
      if (!item || typeof item !== "object" || Array.isArray(item)) issue = "资料必须是对象，请检查高级配置。";
      else if (typeof item.title !== "string" || !item.title.trim()) issue = "请填写资料标题。";
      else if (typeof item.content !== "string" || !item.content.trim()) issue = "请填写资料内容。";
      else if (typeof item.version !== "string" || !item.version.trim()) issue = "请填写资料版本。";
      else if (typeof item.id !== "string" || !item.id.trim() || ids.has(item.id)) issue = "资料编号必须填写且不能重复，请检查高级配置。";
      else if (!validDate(item.valid_from) || !validDate(item.valid_until)) issue = "请填写有效的生效日期和截止日期（年-月-日）。";
      else if (item.valid_from > item.valid_until) issue = "截止日期不能早于生效日期。";
      if (issue) {
        $("knowledge-document").value = String(index); loadKnowledgeFields();
        throw new Error(`第 ${index + 1} 份资料：${issue}`);
      }
      ids.add(item.id);
    });
    return config;
  }
  async function publishKnowledge(event) {
    event.preventDefault();
    if (knowledgeBusy() || state.knowledgeEditor.confirmAction || state.knowledgeEditor.conflict) return;
    formError("knowledge-error");
    let config, result;
    try {
      config = validateKnowledge();
      state.knowledgeEditor.saving = true; renderKnowledgeControls();
      result = await api("/api/knowledge", { config, base_release: state.knowledgeEditor.release });
    } catch (error) {
      state.knowledgeEditor.conflict = error.status === 409 || /其他页面更新|版本冲突/.test(error.message);
      formError("knowledge-error", error.message + (state.knowledgeEditor.conflict ? " 当前输入已保留，请重新载入后再编辑发布。" : ""));
      return;
    } finally { state.knowledgeEditor.saving = false; renderKnowledgeControls(); }
    state.knowledgeEditor.baseline = JSON.stringify(config);
    $("knowledge-dialog").close();
    toast(result.changed === false ? "资料内容没有变化，当前版本已保留。" : "企业资料已发布，已有建议需重新核查。");
    state.bootstrap.company.name = config.name;
    $("company-name").textContent = config.name;
    try {
      if (state.page === "knowledge") await loadKnowledge(state.pageVersion);
      await refreshConversations();
      if (state.selected) await selectConversation(state.selected, true);
    } catch (error) { toast(`资料已保存，但页面刷新未完成。请重新载入工作台。${error.message}`, true); }
  }
  function bindEvents(){
    $("customers-open").addEventListener("click", openCustomers);
    $("customer-rescan").addEventListener("click", requestCustomerRescan);
    $("customer-search").addEventListener("input", renderCustomers); $("customer-filter").addEventListener("change", renderCustomers);
    $("customer-list").addEventListener("change", event => { const id = event.target?.dataset?.customerId; if (id) toggleCustomer(id, event.target.checked); });
    $("customer-save").addEventListener("click", saveCustomers);
    $("customer-close").addEventListener("click", closeCustomers); $("customer-cancel").addEventListener("click", closeCustomers);
    $("customer-dialog").addEventListener("cancel", event => { event.preventDefault(); closeCustomers(); });
    $("customer-keep").addEventListener("click", () => resolveCustomerDiscard(false));
    $("customer-discard-confirm").addEventListener("click", () => resolveCustomerDiscard(true));
    $("customer-update").addEventListener("click", () => { if (state.customers.saving || state.customers.scanning || customersDirty()) return; $("customer-dialog").close(); updateChats(); });
    document.querySelectorAll(".main-nav [data-page]").forEach(button=>button.addEventListener("click",()=>setPage(button.dataset.page)));
    document.querySelectorAll("[data-pane]").forEach(button=>button.addEventListener("click",()=>setPane(button.dataset.pane)));
    document.querySelectorAll("[data-close]").forEach(button=>button.addEventListener("click",()=>$(button.dataset.close).close()));
    $("conversation-search").addEventListener("input",renderConversations);$("status-filter").addEventListener("change",renderConversations);
    $("refresh-list").addEventListener("click",async()=>{$("refresh-list").disabled=true;try{await refreshConversations(true);if(state.selected)await selectConversation(state.selected,true);}catch(error){toast(error.message,true);}finally{$("refresh-list").disabled=false;}});
    $("job-cancel").addEventListener("click",cancelGeneration);$("job-reconnect").addEventListener("click",reconnectJob);
    $("generate-button").addEventListener("click",()=>requestAnalysis("generate"));$("polish-button").addEventListener("click",()=>requestAnalysis("polish"));$("adopt-button").addEventListener("click",adoptReply);
    $("model-retry").addEventListener("click",loadModels);
    $("ai-service-retry").addEventListener("click",loadModels);
    $("manager-settings-open").addEventListener("click",openManagerSettings);
    $("ai-settings-open").addEventListener("click",openManagerSettings);
    $("manager-settings-dialog").addEventListener("close",()=>{const target=$(state.managerSettingsReturnId||"more-open");(state.managerSettingsReturnId==="ai-settings-open"&&$("ai-service-status").hidden?$("more-open"):target)?.focus();});
    $("reply-model").addEventListener("change",event=>{
      const id=event.target.value;
      if(isRunning(state.activeJob)||state.busyAnalyze||state.busyAdopt||!state.modelsLoaded||!state.models.some(model=>model.id===id)){event.target.value=state.modelId;return;}
      state.modelId=id;state.modelNotice="";
      try{const storageKey=modelStorageKey();if(storageKey)localStorage.setItem(storageKey,id);}catch{}
      renderJob();
    });
    $("reply-language").addEventListener("change",event=>{if(state.busyAdopt){event.target.value=state.languages.get(key(state.selected))||"auto";return;}if(state.selected)state.languages.set(key(state.selected),event.target.value);});
    $("customer-model-confirm").addEventListener("click",()=>{const pending=state.pendingAnalysis;state.pendingAnalysis=null;$("customer-model-dialog").close();if(pending)startAnalysis(pending.target,pending.request,true);});$("customer-model-dialog").addEventListener("close",()=>{state.pendingAnalysis=null;});
    $("sync-button").addEventListener("click",updateChats);$("more-open").addEventListener("click",openMore);$("conversation-more").addEventListener("click",openMore);
    $("simulation-open").addEventListener("click",openMessage);$("activity-open").addEventListener("click",()=>{$("more-dialog").close();setPage("activity");});
    $("message-form").addEventListener("submit",addMessage);$("message-body").addEventListener("input",event=>{if(state.messageTarget)state.messageEdits.set(key(state.messageTarget),event.target.value);});
    $("import-open").addEventListener("click",()=>{$("more-dialog").close();openImport();});$("import-form").addEventListener("submit",importRecords);$("import-file").addEventListener("change",()=>{if(!$("import-source").value&&$("import-file").files[0])$("import-source").value=$("import-file").files[0].name.replace(/\.json$/i,"").slice(0,120);});
    $("knowledge-advanced-open").addEventListener("click",()=>{$("manager-settings-dialog").close();return openKnowledge({advanced:true});});$("knowledge-form").addEventListener("submit",publishKnowledge);
    $("knowledge-add-page").addEventListener("click",()=>openKnowledge({add:true}));
    $("knowledge-add").addEventListener("click",addKnowledgeDocument); $("knowledge-remove").addEventListener("click",removeKnowledgeDocument);
    $("knowledge-close").addEventListener("click",closeKnowledge); $("knowledge-cancel").addEventListener("click",closeKnowledge);
    $("knowledge-dialog").addEventListener("cancel",event=>{event.preventDefault();closeKnowledge();});
    $("knowledge-confirm-keep").addEventListener("click",()=>resolveKnowledgeConfirm(false)); $("knowledge-confirm-accept").addEventListener("click",()=>resolveKnowledgeConfirm(true));
    $("knowledge-reload").addEventListener("click",reloadKnowledgeEditor);
    globalThis.addEventListener?.("beforeunload",event=>{if(knowledgeDirty()||state.knowledgeEditor.saving||replySettingsDirty()||state.replySettings.saving){event.preventDefault();event.returnValue="";}});
    $("knowledge-validate").addEventListener("click",()=>{formError("knowledge-error");try{const config=validateKnowledge();$("knowledge-validation").textContent=`格式与企业归属通过，包含 ${config.knowledge.length} 份资料。发布时进一步校验。`;}catch(error){$("knowledge-validation").textContent="";formError("knowledge-error",error.message);}});
    $("knowledge-json").addEventListener("input",()=>{if(knowledgeBusy()||state.knowledgeEditor.confirmAction)return;$("knowledge-validation").textContent="";populateKnowledgeFields();});$("knowledge-document").addEventListener("change",()=>{if(!knowledgeBusy()&&!state.knowledgeEditor.confirmAction)loadKnowledgeFields();});
    for(const id of ["knowledge-title","knowledge-content-editor","knowledge-version","knowledge-from","knowledge-until"])$(id).addEventListener("input",updateKnowledgeFields);
    $("knowledge-file").addEventListener("change",async()=>{
      if(knowledgeBusy()||state.knowledgeEditor.confirmAction)return;
      const file=$("knowledge-file").files[0];if(!file)return;
      formError("knowledge-error");state.knowledgeEditor.loading=true;renderKnowledgeControls();
      let config;
      try{config=await readJSONFile(file);}catch(error){formError("knowledge-error",error.message);return;}
      finally{state.knowledgeEditor.loading=false;$("knowledge-file").value="";renderKnowledgeControls();}
      const apply=()=>{$("knowledge-json").value=JSON.stringify(config,null,2);populateKnowledgeFields(0);$("knowledge-validation").textContent="文件已载入，尚未发布。";};
      if(knowledgeDirty())confirmKnowledge("载入文件将替换当前全部未保存修改。需要保留的内容请先复制。", "放弃修改并载入", apply);else apply();
    });
    $("strategy-form").addEventListener("submit",saveStrategy);
    $("strategy-text").addEventListener("input",()=>{if(!replySettingsLocked())renderReplySettingsControls();});
    $("reply-custom-field").addEventListener("input",()=>{if(!replySettingsLocked())renderReplySettingsControls();});
    $("reply-custom-field").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();addReplyField();}});
    $("reply-field-add").addEventListener("click",addReplyField);
    $("reply-rule-add").addEventListener("click",()=>{if(replySettingsLocked())return;state.replySettings.rules.push("");renderReplyRules();renderReplySettingsControls();$("reply-rule-"+(state.replySettings.rules.length-1)).focus();});
    $("reply-settings-reload").addEventListener("click",reloadReplySettings);
    $("reply-discard-keep").addEventListener("click",()=>resolveReplyDiscard(false));$("reply-discard-confirm").addEventListener("click",()=>resolveReplyDiscard(true));
    $("reply-settings-discard").addEventListener("cancel",event=>{event.preventDefault();resolveReplyDiscard(false);});
    $("refresh-activity").addEventListener("click",()=>loadActivity());
  }
  async function initialize(){ $("global-error").hidden=true;try{state.bootstrap=await api("/api/bootstrap");$("company-name").textContent=state.bootstrap.company.name;$("mode-badge").textContent=state.bootstrap.company.mode==="simulation"?"演练模式":"客户工作区";$("workspace-note").textContent=state.bootstrap.company.mode==="simulation"?"虚构数据 · 可放心演练":"独立企业工作区";state.sync=state.bootstrap.sync||null;if(state.sync)renderSync();else await loadSync();state.activeJob=state.bootstrap.active_job;await Promise.all([loadModels(),refreshConversations(true)]);renderJob();if(isRunning(state.activeJob))watchJob(state.activeJob);else if(state.bootstrap.recent_jobs?.[0]?.status==="interrupted")toast("上次生成已中断，原稿仍保留。选择客户后可以重新生成。",true);}catch(error){$("company-name").textContent="工作区未连接";$("mode-badge").textContent="未连接";$("global-error").innerHTML=`${escape(error.message)} <button id="retry-bootstrap">重新连接</button>`;$("global-error").hidden=false;$("retry-bootstrap").addEventListener("click",initialize);$("conversation-list").innerHTML=empty("工作区未连接","确认工作台已启动，再点击重新连接。");}}
  bindEvents();setPane("list");initialize();
})();
