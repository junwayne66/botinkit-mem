"use strict";

const pages = {
  statements: "当前声明", receipts: "装入回执", handoff: "任务接手",
  sources: "来源与用途", governance: "撤回与恢复", evaluation: "评测与发布"
};
const clients = [
  {id: "spruce", name: "云杉", route: "本机模型", session: "松果"},
  {id: "birch", name: "白桦", route: "云端模型", session: "白露"}
];
const first = "所有助手的报告用中文，先列行动项";
const change = "以后报告先写结论";
const state = {
  version: 0, sequence: 0, revision: 0, snapshots: {}, language: null, opening: null, history: [], pending: [],
  clients: {}, events: [], sourcePaused: false, withdrawn: false, cleaned: false,
  handoffClient: "spruce", hasToolReceipt: true, restore: "old", release: "compared", trainingAllowed: true, releaseLog: []
};
const view = document.querySelector("#view");
const dialog = document.querySelector("#dialog");
const dialogBody = document.querySelector("#dialog-body");
let trigger, statusTimer;

function escapeText(value) {
  return String(value).replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
}
function clearStatements() {
  Object.assign(state, {version:0, sequence:0, revision:0, snapshots:{}, language:null, opening:null, history:[], pending:[], events:[], clients:{}});
  for (const {id} of clients) state.clients[id] = {provided:0, loaded:0, used:null, outcome:"未知", resultVersion:0};
}
function append(text) {
  const id = ++state.sequence;
  const entry = {id, text, updates:[], replaced:[], values:[]};
  const normalized=text.trim().replace(/[。.!！]+$/, "");
  const updates = normalized === first ? [["language","中文"], ["opening","先列行动项"]] : normalized === change ? [["opening","先写结论"]] : [];
  for (const [key, value] of updates) {
    const old = state[key];
    if (old?.value === value) continue;
    const dim = key === "language" ? "报告语言" : "报告开头";
    if (old) entry.replaced.push(`${dim} · ${old.value}（R${old.id}）`);
    const revision = ++state.revision;
    state[key] = {id:revision, sourceId:id, value, text};
    entry.updates.push(`${dim} · ${value}（R${revision}）`);
  }
  if (entry.updates.length) {
    state.version++;
    state.snapshots[state.version] = {language:state.language, opening:state.opening};
  }
  if (!updates.length) state.pending.unshift(entry);
  entry.result = !updates.length ? "待归并" : entry.updates.length ? "已生效" : "相同取值已记录";
  entry.values = [state.language, state.opening].filter(Boolean).map(d => `${d.value}（R${d.id}）`);
  state.history.unshift(entry);
  return entry;
}
const badge = (value, wait = false) => `<span class="pill${wait ? " wait" : ""}">${value}</span>`;
const btn = (action, label, cls = "", id = "") => `<button type="button" data-action="${action}" data-id="${id}" class="${cls}">${label}</button>`;
const link = (page, label) => `<a href="#${page}" class="link">${label}</a>`;
const note = (s) => `<p class="note">${s}</p>`;
const head = (name, desc, action = "") => `<div class="heading"><div><h1 tabindex="-1">${name}</h1><p class="sub">${desc}</p></div>${action}</div>`;
const dl = rows => `<dl>${rows.map(([k,v])=>`<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>`;
const row = (title, result) => `<div class="row"><strong>${title}</strong>${result}</div>`;
const version = n => n ? `版本 ${n}` : "暂无版本";
const keyFor = () => Object.hasOwn(pages,location.hash.slice(1)) ? location.hash.slice(1) : "statements";
function receiptLabel(host) {
  if (!state.version) return "暂无声明";
  if (host.loaded === state.version) return "已装入";
  if (host.provided === state.version) return "已提供 · 待回执";
  return host.loaded ? "等待装入新版" : "待提供";
}
function statements() {
  const count = [state.language,state.opening].filter(Boolean).length;
  const dims = [["报告语言",state.language],["报告开头",state.opening]].filter(([,d])=>d).map(([name,d])=>
    `<div class="item">${row(name,`<span class="mono">R${d.id}</span>`)}<p>${d.value}</p><div class="meta">所有已获准助手 · 本人声明</div></div>`).join("");
  const current = [state.language?.value === "中文" ? "报告用中文" : "", state.opening?.value || ""].filter(Boolean).join("，");
  const recent = state.history[0];
  return head("当前声明","查看此刻对获准助手生效的报告默认值。",btn("compose","追加声明","primary")) +
    `<div class="layout"><div class="stack">
      <section class="hero"><div class="section-head"><span>全局默认值</span>${badge(version(state.version))}</div>
        <h2>${current ? current+"。" : "还没有生效的声明"}</h2>
        <hr class="rule"><div class="row"><span class="note">${count} 项已生效 · 所有已获准助手</span><span class="meta">本人声明</span></div></section>
      <section class="card"><h2>适用明细</h2>${dims || note("追加第一条声明后，按可独立更改的维度列在此处。")}
        ${state.pending.length ? note(`另有 ${state.pending.length} 条待归并。当前确认值仍有独立修订号；待归并内容见下方记录。`) : ""}
      </section>
      <section class="card"><h2>声明记录</h2>${recent ? state.history.map(e=>`<details><summary>D${e.id} · 提交时${e.result}</summary><blockquote>${escapeText(e.text)}</blockquote>
      ${dl([["提出者","本人 · 本机管理台"],["提交时的变更",e.updates.join("，") || "本条未产生新的确定修订"],["取代",e.replaced.join("，") || "无"],["当时确认值",e.values.join("，") || "无"]])}</details>`).join("") : note("暂无记录。")}</section>
      </div><div class="stack"><section class="card"><div class="section-head"><h2>助手装入情况</h2><span class="mono">${clients.filter(c=>state.version && state.clients[c.id].loaded===state.version).length} / 2</span></div>
      ${clients.map(c=>`<div class="item">${row(c.name,badge(receiptLabel(state.clients[c.id]),state.clients[c.id].loaded!==state.version || !state.version))}<p class="meta">${c.route} · 会话「${c.session}」</p></div>`).join("")}
      <div class="actions">${link("receipts","查看装入回执")}</div></section>
      <section class="card"><h2>最近一次声明</h2>${recent ? `<blockquote>${escapeText(recent.text)}</blockquote>${note(`D${recent.id} · 本人 · ${recent.result}`)}` : note("尚未提交。")}
      ${note("共享范围覆盖获准的偏好。每次展开原句时，按实际接收者重新检查来源权限。")}</section></div></div>`;
}
function receipts() {
  return head("装入回执","云杉与白桦为虚构接收者。真实首验使用 Claude Code 与 Codex，按实际会话与模型路由核对。") +
    `<div class="stack">${clients.map(c=>{
      const h = state.clients[c.id];
      let action;
      if (!state.version) action = link("statements","去追加声明");
      else if (h.provided !== state.version) action = btn("provide","模拟提供当前快照","primary",c.id);
      else if (h.loaded !== state.version) action = btn("load","模拟宿主装入回执","primary",c.id);
      else action = btn("outcome","模拟任务反馈","",c.id);
      return `<section class="card"><div class="section-head"><h2>${c.name}</h2>${badge(receiptLabel(h), !state.version || h.loaded!==state.version)}</div>
      <div class="meta">会话「${c.session}」 · ${c.route} · 可替换声明段</div>
      ${dl([["当前应取",version(state.version)],["最近已提供",version(h.provided)],["最近已装入",version(h.loaded)],["采用证据",h.used ? h.used.caption : "尚无"],["最近任务结果",h.resultVersion ? h.outcome+" · "+version(h.resultVersion) : "未知"]])}
      <div class="actions">${action}${btn("receipt-evidence","查看依据","clear",c.id)}</div></section>`;
    }).join("")}<section class="card"><h2>事件顺序</h2>${state.events.length ? `<ol class="timeline">${state.events.map(e=>`<li>${e}</li>`).join("")}</ol>` : note("等待宿主请求。")}</section></div>`;
}
function handoff() {
  const allowed = state.handoffClient==="spruce" && !state.withdrawn;
  const observed = state.hasToolReceipt;
  const summary = observed ? "解析通过，入库仍超时" : "执行结果未知";
  const extraction = allowed ? `<div class="section-head"><h2>自动提炼记录</h2>${badge(observed?"经历已生效":"仅有计划",!observed)}</div>
    <label for="extract-stage">回放到哪条来源</label><select id="extract-stage">
      <option value="plan" ${observed?"":"selected"}>片段 8 · 仅收到普通对话</option>
      <option value="receipt" ${observed?"selected":""}>片段 9 · 收到工具回执</option></select>
    ${dl([["记录类型","系统自动提炼 · 本次任务经历"],["普通对话","片段 8 · 本人描述计划"],["执行凭据",observed?"片段 9 · 工具报告执行结果":"尚未收到工具回执"],["处理结果",observed?"后台提案 P9 → 证据核验通过 → 经历修订 H1":"后台提案 P8 留存计划，执行结果待证"]])}
    ${note("本人明确提出的长期偏好可在「当前声明」查看。任务经历只在适用的接手任务中提供。")}
    <div class="actions">${btn("task-evidence","补读原文窗口","primary")}</div>` :
    `<h2>接手依据</h2>${note("此视图按当前读取范围生成。")}`;
  return head("任务接手","按接收者核对本次经历、提炼结果和原文依据。")+
    `<div class="layout"><div class="stack"><section class="card"><label for="receiver">预览接收者</label>
      <select id="receiver">${clients.map(c=>`<option value="${c.id}" ${c.id===state.handoffClient?"selected":""}>${c.name} · ${c.route}</option>`).join("")}</select></section>
      <section class="card"><h2>${allowed?"溪桥 · 导入超时":"没有可交付的任务材料"}</h2>
        ${allowed ? dl([["目标","定位本次导入停在哪一步"],["计划","准备复验导入流程"],["已执行",observed?"重放解析与写入步骤":"待工具回执"],["最后验证",summary],["待解决",observed?"核对写入调用的超时原因":"取得执行回执"]]) : note("请核对接收者与来源用途，或在获准的本机环境继续。")}
      </section></div><section class="card">${extraction}<div class="actions">${link("sources","查看来源与用途")}</div></section></div>`;
}
function sources() {
  return head("来源与用途","按资料分别阅读采集状态、在线接收者和离线用途。")+
  `<div class="layout"><section class="card"><h2>所有者的声明</h2>${dl([["来源身份","本人 · 本机管理台"],["共享偏好","云杉 / 本机模型，白桦 / 云端模型"],["原句补读","本人和云杉 / 本机模型"],["离线训练","未授权"]])}<div class="actions">${link("statements","查看当前声明")}</div></section>
  <section class="card"><div class="section-head"><h2>本机导入记录</h2>${badge(state.sourcePaused?"已停止采集":"采集中",state.sourcePaused)}</div>
  ${dl([["来源身份","虚构工作区 · 连接器「溪桥」"],["在线用途","任务接手与原文补读"],["接收者","云杉 / 本机模型"],["离线训练","未授权"],["旧对象",state.withdrawn?"来源已撤回":"已采集资料仍可按授权读取"]])}
  <div class="actions">${btn("pause",state.sourcePaused?"恢复采集":"停止采集")}${link("governance","查看撤回与恢复")}</div>
  </section></div>`;
}
function governance() {
  return head("撤回与恢复","检查上游撤回的生效范围，以及备份能证明的撤回水位。")+
  `<div class="layout"><section class="card"><div class="section-head"><h2>来源撤回</h2>${badge(state.withdrawn?"新读取已阻止":"尚未撤回",!state.withdrawn)}</div>
  <p class="note">样本对象为「溪桥」的一份导入记录。回执只保留处理状态和范围。</p>
  ${dl([["后续读取",state.withdrawn?"已阻止搜索、原文补读与派生接手卡":"按现行授权开放"],["受管副本",state.withdrawn?(state.cleaned?"样本索引清理已回报":"索引清理待回报"):"受现行保管规则约束"],["既有投递","历史已投递至云杉的会话，接收端副本无法核实"]])}
  <div class="actions">${!state.withdrawn ? btn("withdraw-preview","预览来源撤回","danger") : !state.cleaned ? btn("clean","模拟接收清理回报") : ""}${link("handoff","复查任务接手")}</div></section>
  <section class="card"><h2>隔离恢复预览</h2><label for="backup-type">可取得的撤回记录</label><select id="backup-type">
    <option value="old" ${state.restore==="old"?"selected":""}>仅有删除前孤本</option><option value="ledger" ${state.restore==="ledger"?"selected":""}>附齐全的后续撤回清单</option></select>
  ${dl(state.restore==="old" ? [["快照截止","水位 18"],["后续水位","未知"],["可开放范围","仅历史预览。普通读取关闭。"]] : [["快照截止","水位 18"],["后续水位","已核对至 21"],["可开放范围","按样本清单应用撤回后，继续校验附件、来源授权和接收域。"]])}
  ${note("恢复预览使用独立的备份样例，页面上的当前资料保持可用。")}</section></div>`;
}

const evalPairs = [
  ["书名精确查询", true, true], ["同义书名查询", false, true],
  ["无馆藏的拒答", true, true], ["不同译名消歧", false, false]
];
function evaluation() {
  const stage = {compared:"待影子对照",shadow:"影子运行",trial:"限定试用",rollback:"已回退",revoked:"训练用途已撤回"}[state.release];
  const active = state.release==="trial" ? "B · 仅限已授权目录试用组" : "A · 词法基线";
  const passing=i=>evalPairs.filter(row=>row[i]).length;
  const gains=evalPairs.filter(([,a,b])=>!a&&b).length, losses=evalPairs.filter(([,a,b])=>a&&!b).length;
  const next={compared:["shadow","模拟影子运行"],shadow:["trial","查看限定试用范围"],trial:["rollback","回退到词法基线"],rollback:["shadow","重新模拟影子运行"]}[state.release];
  const stageData={
    compared:"已经完成隔离回放。线上合成请求仍由 A 处理。",
    shadow:"3 个旁路样本中有 1 个候选顺序不同。B 没有参与实际任务，无任务通过率。",
    trial:"仅两项新授权的目录试用任务采用 B。运行端样本回执为 1 项通过、1 项未知。此轮缺少同任务基线，不能估计实际增益。",
    rollback:"试用范围已收到加载 A 的样本回执。基线使用固定词法规则。",
    revoked:"已停止 B 的训练、发布和试用。运行端已确认回到与训练来源无依赖的规则 A。"
  }[state.release];
  const decision="同义书名得到改善，译名消歧仍失败。4 个冻结任务只支持继续旁路观察，实际试用另限已授权范围。";
  return head("评测与发布","P1 / P2 设计预览 · 独立的合成图书目录。")+
    `<div class="layout"><div class="stack"><section class="card"><div class="section-head"><h2>等预算的配对回放</h2>${badge("P1 · 冻结集 E1")}</div>
      ${note("A 采用词法规则，B 采用候选重排器。同一读取模型、4 个候选、256 token、2 步；每个任务共享相同的可见目录与工具。")}
      ${dl([["任务通过","A "+passing(1)+"/4　·　B "+passing(2)+"/4"],["配对变化",gains+" 项由失败变通过，"+losses+" 项由通过变失败"],["观察单位","4 个独立冻结任务族，各 1 题"]])}
      <details><summary>查看四项配对结果</summary>${evalPairs.map(([name,a,b])=>`<div class="item"><strong>${name}</strong><p class="meta">A ${a?"通过":"失败"}　/　B ${b?"通过":"失败"}</p></div>`).join("")}</details>${note(decision)}</section>
      <section class="card"><div class="section-head"><h2>发布与回退</h2>${badge(stage,true)}</div>
      ${dl([["正在使用",active],["范围之外","A · 词法基线"],["任务效果",stageData]])}<div class="actions">${next?btn(next[0],next[1],"primary"):""}</div>
      ${state.releaseLog.length?`<details><summary>查看操作回执（${state.releaseLog.length}）</summary><ol class="timeline">${state.releaseLog.map(e=>`<li>${e}</li>`).join("")}</ol></details>`:""}</section></div>
      <div class="stack"><section class="card"><h2>离线训练用途</h2>
      ${dl([["来源","虚构目录 C · 训练分区 T1"],["工件关系","T1 → 训练运行 J1 → 重排器 B"],["许可",state.trainingAllowed?"已授权本机训练、冻结评测与限定目录试用":"已撤回 · 禁止新训练与发布"],["冻结分区","E1 留出后续任务族，和 T1 无共同根来源"]])}
      <div class="actions">${state.trainingAllowed?btn("training-preview","预览撤回训练用途","danger"):""}</div></section>
      <section class="card"><h2>当前个人声明</h2><p class="note">${version(state.version)} · ${[state.language,state.opening].filter(Boolean).map(d=>`${d.value}（R${d.id}）`).join("、")||"暂无生效值"}</p>
      ${note("只读核对权威修订。模型与策略开关只作用于对应任务的检索。")}<div class="actions">${link("statements","核对声明")}</div></section></div></div>`;
}

function render() {
  const key = keyFor();
  document.title = `Botinkit Memory · ${pages[key]}`;
  view.innerHTML = {statements,receipts,handoff,sources,governance,evaluation}[key]();
  document.querySelectorAll("nav a").forEach(a=>{
    if(a.hash === "#"+key) a.setAttribute("aria-current","page");
    else a.removeAttribute("aria-current");
  });
}
function say(message) {
  clearTimeout(statusTimer);
  const region = document.querySelector("#status");
  region.textContent = message;
  statusTimer = setTimeout(()=>{ region.textContent=""; }, 3600);
}
function showDialog(title, body) {
  trigger = document.activeElement;
  dialogBody.innerHTML = `<h2 id="dialog-title">${title}</h2>${body}`;
  dialog.showModal();
}
function focusAction(action, id) {
  const key = id ? `[data-id="${id}"]` : "";
  const target = view.querySelector(`[data-action="${action}"]${key}`) ||
    (id && view.querySelector(`button[data-id="${id}"]`)) || view.querySelector("h1");
  target?.focus();
}
function update(message, action, id) {
  render(); focusAction(action,id); say(message);
}
dialog.addEventListener("close",()=>{
  if (!dialog.open) {dialogBody.replaceChildren();if (trigger?.isConnected) trigger.focus();}
});
document.addEventListener("click",e=>{
  const target=e.target.closest("[data-action]");
  if(!target) return;
  const {action,id} = target.dataset;
  const c=clients.find(h=>h.id===id), h=state.clients[id];
  if(action==="close"){dialog.close();return;}
  if(action==="compose") {
    showDialog("追加本人声明",`<p class="sub">写下今后要采用的默认值。此样稿内置下列两句话，其他表达可体验「待归并」。</p>
    <form id="statement-form"><div class="actions">${btn("fill-first","中文＋行动项")}${btn("fill-change","改为结论开头")}</div>
    <label for="original">我的原话</label><textarea id="original" required maxlength="300"></textarea>${note("范围：全局 · 所有获准助手。原句限本人和本机获准助手补读。")}<div class="actions"><button type="submit" class="primary">提交声明</button>${btn("close","取消")}</div></form>`);
  } else if(action.startsWith("fill-")) {
    const input=document.querySelector("#original");input.value=action==="fill-first"?first:change;input.setCustomValidity("");input.focus();
  } else if (action==="provide" || action==="load") {
    const field=action==="provide"?"provided":"loaded";
    h[field]=state.version;
    const event=`${c.name} / ${c.route} · ${version(state.version)} · ${action==="provide"?"已提供":"已装入（宿主回执）"}`;
    state.events.push(event);update(event,action,id);
  } else if(action==="receipt-evidence") {
    const allowed=c.id==="spruce";
    const snapshotView=(n,title)=>{
      const snap=state.snapshots[n];
      if(!snap) return `<h3>${title}</h3>${note("尚无装入回执。")}`;
      const fields=[["报告语言",snap.language],["报告开头",snap.opening]].filter(([,v])=>v);
      const sourceText=[...new Set(fields.map(([,v])=>v.text))];
      return `<h3>${title} · ${version(n)}</h3>${dl(fields.map(([k,v])=>[k,`${v.value} · R${v.id}`]))}
        ${allowed ? sourceText.map(t=>`<blockquote>${escapeText(t)}</blockquote>`).join("") : note("此接收者仅获准共享值，原始会话正文不可补读。")}`;
    };
    showDialog("快照与装入依据",`<p class="sub">按${c.name} / ${c.route}预览 · 来源为本人声明</p><hr class="rule">
      ${snapshotView(h.loaded,"最近已装入")}<hr class="rule">${state.version?snapshotView(state.version,"当前可提供"):"当前暂无生效声明。"}
      <div class="actions">${btn("close","返回")}</div>`);
  } else if(action==="outcome") {
    showDialog("模拟任务反馈",`<p class="sub">${c.name} · ${version(h.loaded)}。两种信号可以独立出现。</p>
    <div class="actions">${btn("used",state.snapshots[h.loaded].opening?"记录报告开头的采用凭据":"记录报告语言的采用凭据","",id)}${btn("passed","记录任务通过","",id)}${btn("failed","记录任务失败","",id)}${btn("close","取消")}</div>`);
  } else if(["used","passed","failed"].includes(action)) {
    if(action==="used") {
      const snapshot=state.snapshots[h.loaded];
      const item=snapshot.opening || snapshot.language;
      const dimension=snapshot.opening ? "报告开头" : "报告语言";
      const proof=snapshot.opening ? (item.value==="先写结论" ? "首段「结论：周报已汇总」" : "首段「行动项：汇总本周进度」") : "样本报告正文为中文";
      h.used={caption:`${dimension} · R${item.id} · ${version(h.loaded)}，样本输出：${proof}`};
    }
    else {h.outcome=action==="passed"?"通过":"失败"; h.resultVersion=h.loaded;}
    const label={used:"已采用",passed:"任务通过",failed:"任务失败"}[action];
    const event=`${c.name} · ${version(h.loaded)} · ${label}${action==="used"?" · "+h.used.caption:""}`;
    state.events.push(event);dialog.close();update(event,"outcome",id);
  } else if(action==="task-evidence" && !state.withdrawn && state.handoffClient==="spruce") {
    showDialog("原文窗口",`<p class="sub">本机导入记录 · 源对象 I7 · 云杉 / 本机模型</p>
      <p class="note">片段 8 · 普通对话 · 消息版本 1</p><blockquote>本人：想定位导入卡在哪一步。我准备复验导入流程。</blockquote>
      ${state.hasToolReceipt ? `<p class="note">片段 9 · 工具回执 · 消息版本 1</p><blockquote>工具：已重放解析与写入步骤。解析通过，入库仍超时。</blockquote>` : note("当前回放时点只包含计划。")}
      <div class="actions">${btn("close","返回接手卡")}</div>`);
  } else if(action==="pause") {
    state.sourcePaused=!state.sourcePaused;update(state.sourcePaused?"采集已停止。已采集资料仍按现有授权读取。":"采集已恢复。","pause");
  } else if(action==="withdraw-preview") {
    showDialog("撤回这份来源资料",`<p class="sub">模拟上游撤回「溪桥」的一份记录。提交后立即阻止新读取，受管索引将等待清理回报。</p>
      ${note("既已发送至云杉的旧会话列为系统外副本。刷新页面可重置演示。")}<div class="actions">${btn("close","取消")}${btn("withdraw","确认模拟撤回","danger")}</div>`);
  } else if(action==="withdraw") {
    state.withdrawn=true;dialog.close();update("撤回已提交。新读取已阻止，索引清理待回报。","clean");
  } else if(action==="clean") {
    state.cleaned=true;update("样本索引已清理。已投递副本仍无法核实。","clean");
  } else if(action==="shadow" && state.trainingAllowed) {
    state.release="shadow";state.releaseLog.push("候选 B 已旁路观察，活动版本为 A。");update("影子对照已完成。","trial");
  } else if(action==="trial" && state.release==="shadow") {
    showDialog("限定目录试用",`<p class="sub">试用许可仅覆盖两项新的合成图书目录任务。B 的目录许可、接收域与预算均已在样本中登记，其他范围继续由 A 处理。</p>
      ${note("本次演示不提供全面发布。运行端回报实际版本后再查看试用结果。")}<div class="actions">${btn("close","取消")}${btn("start-trial","确认模拟限定试用","primary")}</div>`);
  } else if(action==="start-trial" && state.trainingAllowed) {
    state.release="trial";state.releaseLog.push("限定目录试用组已回报加载 B，范围外继续使用 A。");dialog.close();update("限定试用组已确认加载 B。","rollback");
  } else if(action==="rollback") {
    state.release="rollback";state.releaseLog.push("试用范围已回报加载 A；T1 的原有训练许可继续有效。");update("已回退到词法基线 A。","shadow");
  } else if(action==="training-preview") {
    showDialog("撤回目录 C 的训练用途",`<p class="sub">模拟同时要求清除训练影响。T1 将停用于新训练和发布，依赖它的 B 退出试用。训练依赖清单中没有其他后继或已知导出。</p>
      ${note("本次只针对独立目录样本，个人声明仍由权威库提供。")}<div class="actions">${btn("close","取消")}${btn("revoke-training","确认模拟撤回训练用途","danger")}</div>`);
  } else if(action==="revoke-training") {
    state.release="revoked";state.trainingAllowed=false;state.releaseLog.push("训练用途撤回已生效。T1、J1、B 受影响；停用 B，运行端已回报基线 A。");dialog.close();update("已撤回训练用途并切回干净基线 A。","training-preview");
  } else if(action==="reset") {
    clearStatements();appendRoute("statements");say("已清空本页的演示声明。可以开始追加第一条。");
  }
});
function appendRoute(key) {
  if(location.hash!=="#"+key) location.hash=key;
  else {render();view.querySelector("h1").focus();}
}
document.addEventListener("submit",e=>{
  if(e.target.id!=="statement-form") return;
  e.preventDefault();
  const input=document.querySelector("#original"), text=input.value;
  if(!text.trim()){input.setCustomValidity("请填写声明原话。");input.reportValidity();return;}
  const entry=append(text);dialog.close();update(`声明 D${entry.id} ${entry.result}。`,"compose");
});
document.addEventListener("input",e=>{if(e.target.id==="original") e.target.setCustomValidity("");});
document.addEventListener("change",e=>{
  if(e.target.id==="receiver") state.handoffClient=e.target.value;
  else if(e.target.id==="extract-stage") state.hasToolReceipt=e.target.value==="receipt";
  else if(e.target.id==="backup-type") state.restore=e.target.value;
  else return;
  const field=e.target.id;render();document.getElementById(field).focus();
  say({receiver:"已按所选接收者更新材料。", "extract-stage":"接手材料和证据定位已按回放时点更新。", "backup-type":"隔离恢复预览已更新。"}[field]);
});
document.querySelector(".skip").addEventListener("click",e=>{
  e.preventDefault();document.querySelector("#main").focus();
});
const nav=document.querySelector("nav"), menu=document.querySelector("#menu");
function toggleMenu(open) {
  nav.classList.toggle("open",open);menu.setAttribute("aria-expanded",String(open));menu.textContent=open?"收起导航":"展开导航";
}
menu.addEventListener("click",()=>toggleMenu(!nav.classList.contains("open")));
nav.addEventListener("click",e=>{
  if(e.target.closest("a")?.hash===location.hash){toggleMenu(false);view.querySelector("h1").focus();}
});
window.addEventListener("hashchange",()=>{if(dialog.open) dialog.close();toggleMenu(false);render();view.querySelector("h1").focus();});
clearStatements();append(first);render();
