"""Tela inicial do RINO: pesquisa pelo navegador, progresso ao vivo e resultados.

Página estática (HTML + JS sem dependências externas) que usa a própria RINO API:
``POST /cases`` → ``POST /cases/{id}/investigate`` → acompanha ``GET /jobs/{id}`` → mostra
``GET /cases/{id}/entities`` e gera relatórios via ``POST /cases/{id}/exports``.
Dados vindos da API são sempre inseridos como texto (``textContent``), nunca como HTML.
"""

from __future__ import annotations

from html import escape

from osintizada.branding import COLORS, FULL_TITLE, PRODUCT_NAME, TAGLINE

_TEMPLATE = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" type="image/png" href="/branding/icon.png">
<style>
:root{--ink:__INK__;--graphite:__GRAPHITE__;--steel:__STEEL__;--accent:__ACCENT__;--glow:__GLOW__;
--paper:__PAPER__;--line:#2b3440;--muted:#aeb7c2;--bad:#ff6b6b;--warn:#f0b429;--card:#161c24}
*{box-sizing:border-box}
html{background:var(--ink)}
body{margin:0;min-height:100vh;background:radial-gradient(circle at 50% 0,#18202b 0,var(--ink) 55%);
color:var(--paper);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--glow)}
header{display:flex;align-items:center;gap:14px;padding:12px 20px;border-bottom:2px solid var(--accent);
background:rgba(13,17,23,.85);position:sticky;top:0;z-index:5;flex-wrap:wrap}
header img{width:44px;height:44px;border-radius:10px;background:#fff}
.brand b{display:block;font-size:20px;letter-spacing:.18em}
.brand span{color:var(--muted);font-size:12px}
.pills{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}
.pill{border:1px solid var(--line);background:var(--graphite);border-radius:999px;padding:3px 10px;font-size:12px}
.pill i{font-style:normal;color:var(--muted);margin-right:4px}
.ok{color:var(--glow)} .bad{color:var(--bad)} .warn{color:var(--warn)}
main{max-width:1200px;margin:0 auto;padding:20px 16px;display:grid;grid-template-columns:280px 1fr;gap:16px}
@media (max-width:900px){main{grid-template-columns:1fr}main>aside{order:2}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.card h2{margin:0 0 12px;font-size:15px;letter-spacing:.04em}
label{display:block;color:var(--muted);font-size:12px;margin:10px 0 4px}
textarea,select,input{width:100%;background:var(--ink);color:var(--paper);border:1px solid var(--line);
border-radius:8px;padding:9px 10px;font:inherit}
textarea{min-height:84px;resize:vertical}
textarea:focus,select:focus,input:focus{outline:2px solid var(--accent);outline-offset:0}
.row{display:flex;gap:10px;align-items:end;flex-wrap:wrap}
.row>div{flex:1;min-width:150px}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:10px 16px;font-weight:700;
cursor:pointer;font:inherit;font-weight:700}
button:hover{background:#1478e0} button:disabled{opacity:.5;cursor:default}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--paper)}
button.ghost:hover{border-color:var(--accent)}
.hint{color:var(--muted);font-size:12px;margin-top:6px}
.bar{height:8px;background:var(--ink);border-radius:99px;overflow:hidden;border:1px solid var(--line)}
.bar>div{height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--glow));transition:width .4s}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}
@media (max-width:600px){.stats{grid-template-columns:repeat(2,1fr)}}
.stat{background:var(--graphite);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.stat b{display:block;font-size:20px} .stat span{color:var(--muted);font-size:12px}
.stage{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.stage h2{margin:0}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:var(--graphite);color:var(--muted);font-weight:600;position:sticky;top:0}
td.val{word-break:break-all}
tr:hover td{background:#1b232d}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;background:#10345a;color:#9fd0ff}
.cases{list-style:none;margin:0;padding:0;max-height:420px;overflow:auto}
.cases li{padding:8px;border-radius:8px;cursor:pointer;border:1px solid transparent}
.cases li:hover,.cases li.active{border-color:var(--accent);background:#141b24}
.cases small{display:block;color:var(--muted)}
.hidden{display:none}
.msg{padding:10px;border-radius:8px;background:#3a1a1a;color:#ffd0d0;margin-top:10px}
.actions{display:flex;gap:8px;flex-wrap:wrap}
footer{text-align:center;color:var(--steel);font-size:12px;padding:10px 0 24px}
.ai-panel{margin-top:16px;border-top:1px dashed var(--line);padding-top:12px}
.ai-panel h3{margin:0 0 8px;font-size:14px}
.ai-panel h3 small{color:var(--muted);font-weight:400}
.ai-panel button{padding:6px 10px;font-size:13px}
.ai-card{background:var(--graphite);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-top:10px}
.ai-card .meta{color:var(--muted);font-size:12px;margin-bottom:6px}
.ai-card ul{margin:6px 0 0;padding-left:18px}
.ai-card li{margin:3px 0}
.ai-card .mini{padding:2px 8px;font-size:12px;margin-left:6px}
.ai-card code{background:var(--ink);padding:1px 5px;border-radius:4px}
footer a{margin:0 8px}
</style></head>
<body>
<header>
  <img src="/branding/logo.png" alt="Logo __NAME__">
  <div class="brand"><b>__NAME__</b><span>__TAGLINE__</span></div>
  <div class="pills">
    <span class="pill"><i>API</i><span id="s-api">…</span></span>
    <span class="pill"><i>Banco</i><span id="s-database">…</span></span>
    <span class="pill"><i>Executor</i><span id="s-worker">…</span></span>
    <span class="pill" title="IA auxiliar: interpreta evidências, não as produz"><i>AI</i><span id="s-ai">…</span></span>
  </div>
</header>

<main>
  <aside>
    <section class="card">
      <h2>Investigações recentes</h2>
      <ul class="cases" id="cases"><li><small>carregando…</small></li></ul>
    </section>
    <section class="card hidden" id="token-card">
      <h2>Token de acesso</h2>
      <input id="token" type="password" placeholder="RINO_API_TOKEN" autocomplete="off">
      <div class="hint">Esta API exige token. Ele fica só nesta aba do navegador.</div>
    </section>
  </aside>

  <section>
    <form class="card" id="form">
      <h2>Nova investigação</h2>
      <label for="inputs">Alvos (um por linha): domínio, IP, e-mail, usuário, telefone…</label>
      <textarea id="inputs" placeholder="exemplo.com.br&#10;contato@exemplo.com.br&#10;8.8.8.8" required></textarea>
      <div class="row">
        <div><label for="mode">Modo</label>
          <select id="mode">
            <option value="quick">Rápida (profundidade 1)</option>
            <option value="deep" selected>Profunda (profundidade 2)</option>
            <option value="investigation">Investigação (profundidade 2, mais fontes)</option>
            <option value="deep_sweep">Varredura completa (profundidade 3, demorada)</option>
          </select></div>
        <div><label for="name">Nome do caso (opcional)</label><input id="name" maxlength="200"></div>
        <div style="flex:0"><button id="go" type="submit">Investigar</button></div>
      </div>
      <div class="hint">A pesquisa roda no servidor. Você pode fechar esta página: o resultado fica salvo.</div>
      <div class="msg hidden" id="form-msg"></div>
    </form>

    <section class="card hidden" id="progress-card">
      <div class="stage"><h2 id="stage">Na fila…</h2><span id="job-status" class="tag">QUEUED</span></div>
      <div class="bar"><div id="bar"></div></div>
      <div class="stats">
        <div class="stat"><b id="p-prov">0</b><span>fontes consultadas</span></div>
        <div class="stat"><b id="p-ent">0</b><span>entidades</span></div>
        <div class="stat"><b id="p-ev">0</b><span>evidências</span></div>
        <div class="stat"><b id="p-piv">0</b><span>pivôs</span></div>
      </div>
      <div class="actions" style="margin-top:12px"><button class="ghost" id="cancel">Cancelar</button></div>
      <div class="msg hidden" id="job-msg"></div>
    </section>

    <section class="card hidden" id="results-card">
      <div class="stage"><h2 id="res-title">Resultados</h2>
        <div class="actions">
          <button class="ghost" data-export="html">Relatório HTML</button>
          <button class="ghost" data-export="json">JSON</button>
          <button class="ghost" data-export="csv">CSV</button>
        </div></div>
      <div class="row" style="margin-bottom:10px">
        <div><label for="f-type">Tipo</label><select id="f-type"><option value="">Todos</option></select></div>
        <div><label for="f-text">Filtrar</label><input id="f-text" placeholder="texto…"></div>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Tipo</th><th>Valor</th><th>Origem</th><th>Prof.</th><th>Confiança</th><th>Evid.</th></tr></thead>
        <tbody id="rows"></tbody></table></div>
      <div class="hint" id="res-hint"></div>
      <div class="msg hidden" id="export-msg"></div>
      <div class="ai-panel">
        <h3>Análise por IA <small>— interpretação (DERIVED), não evidência; nada é executado sem você</small></h3>
        <div class="actions">
          <button class="ghost" data-ai="summary">Resumo</button>
          <button class="ghost" data-ai="relevance">Triagem</button>
          <button class="ghost" data-ai="extract">Extrair entidades</button>
          <button class="ghost" data-ai="pivots">Sugerir consultas</button>
          <button class="ghost" data-ai="translate">Traduzir</button>
          <button class="ghost" data-ai="classify">Classificar</button>
        </div>
        <div class="hint" id="ai-msg"></div>
        <div id="ai-results"></div>
      </div>
    </section>
  </section>
</main>
<footer><a href="/docs">Swagger</a><a href="/redoc">ReDoc</a><a href="/health">Health</a>
<div>__NAME__ versão __VERSION__</div></footer>

<script>
"use strict";
const $ = id => document.getElementById(id);
const STATUS = {ok:"ok", ONLINE:"online", STALE:"atrasado", OFFLINE:"offline", NOT_CONFIGURED:"não configurado",
  WORKER_UNAVAILABLE:"indisponível", UNAVAILABLE:"indisponível", MIGRATIONS_PENDING:"migrações pendentes"};
const JOB = {PENDING:"pendente", QUEUED:"na fila", RUNNING:"executando", RETRYING:"nova tentativa",
  COMPLETED:"concluída", FAILED:"falhou", CANCELLED:"cancelada", INTERRUPTED:"interrompida",
  OPEN:"aberta", ARCHIVED:"arquivada"};
const ORIGIN = {SEED:"alvo informado", DISCOVERED:"descoberta", DERIVED:"derivada", PIVOT:"pivô"};
function originLabel(e){
  const ai = (e.metadata || {}).ai;
  if (ai && ai.suggested) return "sugerida por IA" + (ai.review === "ACCEPTED" ? " (aceita)" : ai.review === "REJECTED" ? " (rejeitada)" : " (pendente)");
  return ORIGIN[e.origin] || e.origin;
}
let state = {caseId:null, jobId:null, timer:null, entities:[]};

function token(){ try { return sessionStorage.getItem("rino_token") || ""; } catch(e){ return ""; } }
$("token").addEventListener("change", e => {
  try { sessionStorage.setItem("rino_token", e.target.value.trim()); } catch(_){}
  loadCases();
});
async function api(path, opts={}){
  const headers = Object.assign({"content-type":"application/json"}, opts.headers || {});
  if (token()) headers["authorization"] = "Bearer " + token();
  const r = await fetch(path, Object.assign({}, opts, {headers}));
  if (r.status === 401){ $("token-card").classList.remove("hidden"); throw new Error("Informe o token de acesso."); }
  if (!r.ok){ let d = ""; try { d = (await r.json()).detail; } catch(_){} throw new Error(d ? JSON.stringify(d) : ("HTTP " + r.status)); }
  return r;
}
function show(el, text){ el.textContent = text; el.classList.remove("hidden"); }
function hide(el){ el.classList.add("hidden"); }
function stageLabel(s){
  if (!s) return "Na fila…";
  let m = /^PIVOT_DEPTH_(\d+)/.exec(s);
  if (m) return "Seguindo pistas (profundidade " + m[1] + ")";
  return ({INITIAL_PROVIDERS:"Consultando fontes iniciais", INITIAL_PROVIDERS_COMPLETED:"Fontes iniciais concluídas",
    CORRELATION:"Correlacionando", CORRELATION_COMPLETED:"Correlação concluída", COMPLETED:"Concluída",
    EXPORTING:"Gerando relatório", EXPORTED:"Relatório pronto"})[s] || s;
}

async function health(){
  try {
    const h = await (await fetch("/health")).json();
    const set = (id, v, extra) => { const e = $(id); e.textContent = (STATUS[v] || v) + (extra || "");
      e.title = v; e.className = (v === "ok" || v === "ONLINE") ? "ok" : (v === "STALE" ? "warn" : "bad"); };
    set("s-api", h.api.status); set("s-database", h.database.status);
    set("s-worker", h.worker.status, h.worker.mode === "EMBEDDED" ? " (no servidor)" : "");
    const ai = h.ai || {}; const el = $("s-ai");
    const modeLabel = {LOCAL_ONLY:"LOCAL", HYBRID:"HYBRID", CLOUD:"CLOUD"}[ai.mode] || "—";
    el.textContent = modeLabel + (ai.active_provider ? " · " + ai.active_provider.replace("ai.", "") : " · indisponível");
    el.className = ai.active_provider ? "ok" : "warn";
    el.title = ai.active_provider ? "provider ativo: " + ai.active_provider : "nenhum provider de IA disponível (o RINO funciona sem IA)";
  } catch(e){ $("s-api").textContent = "indisponível"; $("s-api").className = "bad"; }
}

async function loadCases(){
  const ul = $("cases");
  try {
    const cases = await (await api("/cases?limit=30")).json();
    ul.replaceChildren();
    if (!cases.length){ const li = document.createElement("li"); li.innerHTML = "<small>nenhuma ainda</small>"; ul.append(li); return; }
    for (const c of cases){
      const li = document.createElement("li");
      const t = document.createElement("div"); t.textContent = c.name;
      const s = document.createElement("small");
      s.textContent = (JOB[c.status] || c.status.toLowerCase()) + " · " + new Date(c.created_at + (c.created_at.endsWith("Z") ? "" : "Z")).toLocaleString("pt-BR");
      li.append(t, s); li.dataset.id = c.id;
      if (c.id === state.caseId) li.classList.add("active");
      li.addEventListener("click", () => openCase(c.id, c.name));
      ul.append(li);
    }
  } catch(e){ ul.replaceChildren(); const li = document.createElement("li"); const s = document.createElement("small"); s.textContent = e.message; li.append(s); ul.append(li); }
}

$("form").addEventListener("submit", async ev => {
  ev.preventDefault(); hide($("form-msg"));
  const inputs = $("inputs").value.split(/\n|,/).map(s => s.trim()).filter(Boolean);
  if (!inputs.length) return;
  $("go").disabled = true;
  try {
    const name = $("name").value.trim() || (inputs[0] + (inputs.length > 1 ? " +" + (inputs.length - 1) : ""));
    const c = await (await api("/cases", {method:"POST", body:JSON.stringify({name})})).json();
    const j = await (await api("/cases/" + c.id + "/investigate", {method:"POST",
      body:JSON.stringify({inputs, mode:$("mode").value})})).json();
    state.caseId = c.id; hide($("results-card"));
    if (j.warnings && j.warnings.length) show($("form-msg"), "Atenção: " + j.warnings.join(", ") + " — o job ficou salvo e será executado quando houver executor.");
    watch(j.job_id, c.name); loadCases();
  } catch(e){ show($("form-msg"), e.message); }
  finally { $("go").disabled = false; }
});

function watch(jobId, title){
  state.jobId = jobId; clearTimeout(state.timer);
  $("progress-card").classList.remove("hidden"); hide($("job-msg"));
  $("cancel").disabled = false;
  const tick = async () => {
    try {
      const j = await (await api("/jobs/" + jobId)).json();
      $("stage").textContent = stageLabel(j.stage);
      $("job-status").textContent = JOB[j.status] || j.status;
      $("bar").style.width = (j.status === "COMPLETED" ? 100 : (j.progress_percent || 2)) + "%";
      $("p-prov").textContent = j.providers_completed ?? 0;
      $("p-ent").textContent = j.entities_found ?? 0;
      $("p-ev").textContent = j.evidence_found ?? 0;
      $("p-piv").textContent = j.pivots_processed ?? 0;
      if (["COMPLETED","FAILED","CANCELLED"].includes(j.status)){
        $("cancel").disabled = true;
        if (j.status === "FAILED") show($("job-msg"), "Falhou: " + (j.error ? j.error.message : "erro"));
        if (j.status === "CANCELLED") show($("job-msg"), "Investigação cancelada. O que já foi coletado continua salvo.");
        openCase(j.case_id, title); loadCases(); return;
      }
    } catch(e){ show($("job-msg"), e.message); }
    state.timer = setTimeout(tick, 1200);
  };
  tick();
}
$("cancel").addEventListener("click", async () => {
  if (!state.jobId) return;
  try { await api("/jobs/" + state.jobId + "/cancel", {method:"POST"}); $("cancel").disabled = true; }
  catch(e){ show($("job-msg"), e.message); }
});

async function openCase(caseId, name){
  state.caseId = caseId;
  document.querySelectorAll("#cases li").forEach(li => li.classList.toggle("active", li.dataset.id === caseId));
  try {
    state.entities = await (await api("/cases/" + caseId + "/entities")).json();
    const jobs = await (await api("/cases/" + caseId + "/jobs")).json();
    const running = jobs.find(j => j.job_type === "INVESTIGATION" && ["PENDING","QUEUED","RUNNING","RETRYING"].includes(j.status));
    if (running && running.id !== state.jobId) watch(running.id, name);
    $("res-title").textContent = "Resultados — " + (name || "");
    const types = [...new Set(state.entities.map(e => e.type))].sort();
    const sel = $("f-type"); sel.replaceChildren(new Option("Todos (" + state.entities.length + ")", ""));
    for (const t of types) sel.append(new Option(t + " (" + state.entities.filter(e => e.type === t).length + ")", t));
    render(); $("results-card").classList.remove("hidden"); hide($("export-msg")); loadAI();
  } catch(e){ show($("form-msg"), e.message); }
}
function render(){
  const type = $("f-type").value, q = $("f-text").value.trim().toLowerCase();
  const rows = state.entities.filter(e => (!type || e.type === type) && (!q || e.canonical_value.toLowerCase().includes(q)))
    .sort((a, b) => a.depth - b.depth || b.confidence - a.confidence);
  const tb = $("rows"); tb.replaceChildren();
  for (const e of rows.slice(0, 1000)){
    const tr = document.createElement("tr");
    const cells = [e.type, e.display_value || e.canonical_value, originLabel(e), e.depth,
                   Math.round((e.confidence || 0) * 100) + "%", e.evidence_count ?? ""];
    cells.forEach((v, i) => { const td = document.createElement("td"); td.textContent = v; if (i === 1) td.className = "val"; tr.append(td); });
    tb.append(tr);
  }
  $("res-hint").textContent = rows.length ? (rows.length > 1000 ? "Mostrando 1000 de " + rows.length + ". Use o filtro." : rows.length + " entidade(s).")
    : "Nenhuma entidade com este filtro.";
}
$("f-type").addEventListener("change", render);
$("f-text").addEventListener("input", render);

document.querySelectorAll("[data-export]").forEach(b => b.addEventListener("click", async () => {
  const fmt = b.dataset.export; const msg = $("export-msg"); show(msg, "Gerando " + fmt.toUpperCase() + "…");
  // A aba do relatório é aberta já no clique (depois de esperas assíncronas o navegador bloqueia pop-ups).
  const tab = fmt === "html" ? window.open("", "_blank") : null;
  if (tab) tab.document.write("<p style='font-family:sans-serif'>Gerando relatório RINO…</p>");
  try {
    const j = await (await api("/cases/" + state.caseId + "/exports", {method:"POST", body:JSON.stringify({format:fmt})})).json();
    for (let i = 0; i < 300; i++){
      const s = await (await api("/jobs/" + j.job_id)).json();
      if (s.status === "COMPLETED") break;
      if (["FAILED","CANCELLED"].includes(s.status)) throw new Error("Falha ao gerar o relatório.");
      await new Promise(r => setTimeout(r, 700));
    }
    const r = await api("/jobs/" + j.job_id + "/download");
    const blob = await r.blob(); const url = URL.createObjectURL(blob);
    const cd = r.headers.get("content-disposition") || ""; const m = /filename="?([^";]+)/.exec(cd);
    if (tab) tab.location.href = url;
    else { const a = document.createElement("a"); a.href = url; a.download = m ? m[1] : ("rino." + fmt); a.click(); }
    hide(msg);
  } catch(e){ if (tab) tab.close(); show(msg, e.message); }
}));


// --- IA: análises (Jobs) e revisão humana ------------------------------------------------------------
const AI_OPS = {summary:"Resumo", relevance:"Triagem", extract:"Extração", pivots:"Consultas sugeridas",
  translate:"Tradução", classify:"Classificação"};
function el(tag, text, cls){ const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
async function aiRun(op){
  const msg = $("ai-msg"); msg.textContent = AI_OPS[op] + ": na fila…";
  try {
    const j = await (await api("/cases/" + state.caseId + "/ai/analyze", {method:"POST", body:JSON.stringify({operation:op})})).json();
    for (let i = 0; i < 900; i++){
      const s = await (await api("/jobs/" + j.job_id)).json();
      if (["COMPLETED","FAILED","CANCELLED"].includes(s.status)){ msg.textContent = ""; break; }
      msg.textContent = AI_OPS[op] + ": " + (JOB[s.status] || s.status) + "…";
      await new Promise(r => setTimeout(r, 1000));
    }
    await loadAI(); openCase(state.caseId, $("res-title").textContent.replace("Resultados — ", ""));
  } catch(e){ msg.textContent = e.message; }
}
document.querySelectorAll("[data-ai]").forEach(b => b.addEventListener("click", () => aiRun(b.dataset.ai)));
async function review(entityId, accepted){
  try { await api("/cases/" + state.caseId + "/entities/" + entityId + "/ai-review", {method:"POST", body:JSON.stringify({accepted})}); await loadAI(); }
  catch(e){ $("ai-msg").textContent = e.message; }
}
async function investigateQuery(q){
  try {
    const j = await (await api("/cases/" + state.caseId + "/investigate", {method:"POST", body:JSON.stringify({inputs:[q], mode:"quick"})})).json();
    watch(j.job_id, $("res-title").textContent.replace("Resultados — ", ""));
  } catch(e){ $("ai-msg").textContent = e.message; }
}
async function loadAI(){
  const box = $("ai-results"); box.replaceChildren();
  let anns = [];
  try { anns = await (await api("/cases/" + state.caseId + "/ai")).json(); } catch(e){ return; }
  const latest = {};
  for (const a of anns) if (!latest[a.operation]) latest[a.operation] = a;
  for (const a of Object.values(latest)){
    const card = el("div", undefined, "ai-card");
    const when = new Date(a.created_at + (a.created_at.endsWith("Z") ? "" : "Z")).toLocaleString("pt-BR");
    card.append(el("b", AI_OPS[a.operation] || a.operation));
    card.append(el("div", [a.status, a.provider ? (a.provider.replace("ai.", "") + " / " + a.model) : "", a.mode, when]
      .filter(Boolean).join(" · "), "meta"));
    const o = a.output || {};
    if (!["OK","PARTIAL"].includes(a.status)){ card.append(el("div", a.reason || a.status)); box.append(card); continue; }
    if (a.operation === "summary"){
      card.append(el("div", o.summary)); const ul = el("ul");
      (o.key_points || []).forEach(p => ul.append(el("li", p))); card.append(ul);
      card.append(el("div", "Evidências citadas: " + (o.cited_ids || []).length, "meta"));
    } else if (a.operation === "relevance"){
      const st = o.stages || {};
      card.append(el("div", "Entrada " + st.input + " → filtros " + st.after_deterministic_filters + " → avaliadas " + st.assessed + " → alto interesse " + st.high_interest, "meta"));
      const ul = el("ul"); (o.items || []).slice(0, 10).forEach(i => ul.append(el("li", i.score.toFixed(2) + " — " + i.reason))); card.append(ul);
    } else if (a.operation === "extract"){
      const ul = el("ul");
      for (const c of (o.candidates || [])){
        const li = el("li", c.type + ": " + c.value + " (IA " + (c.ai_confidence ?? "?") + ")" + (c.already_known ? " — já conhecida" : ""));
        if (c.entity_id && !c.already_known){
          const ok = el("button", "Aceitar", "ghost mini"); ok.onclick = () => review(c.entity_id, true);
          const no = el("button", "Rejeitar", "ghost mini"); no.onclick = () => review(c.entity_id, false);
          li.append(ok, no);
        }
        ul.append(li);
      }
      card.append(ul); card.append(el("div", (o.discarded || []).length + " descartada(s) pela validação", "meta"));
    } else if (a.operation === "pivots"){
      const ul = el("ul");
      for (const q of (o.queries || [])){
        const li = el("li"); li.append(el("code", q.query), document.createTextNode(" — " + q.reason + (q.already_executed ? " (já consultada)" : "")));
        if (!q.already_executed){ const b = el("button", "Investigar", "ghost mini"); b.onclick = () => investigateQuery(q.query); li.append(b); }
        ul.append(li);
      }
      card.append(ul);
    } else if (a.operation === "translate"){
      const ul = el("ul"); (o.items || []).slice(0, 8).forEach(i => ul.append(el("li", "[" + i.source_language + "] " + i.translation))); card.append(ul);
    } else if (a.operation === "classify"){
      card.append(el("div", Object.entries(o.counts || {}).map(([k, v]) => k + ": " + v).join(" · ")));
    }
    box.append(card);
  }
}
try { $("token").value = token(); } catch(_){}
health(); setInterval(health, 10000); loadCases();
</script>
</body></html>"""


def home_html(version: str) -> str:
    c = COLORS
    page = _TEMPLATE
    for key, value in (("__TITLE__", escape(FULL_TITLE)), ("__NAME__", escape(PRODUCT_NAME)),
                       ("__TAGLINE__", escape(TAGLINE)), ("__VERSION__", escape(version)),
                       ("__INK__", c["ink"]), ("__GRAPHITE__", c["graphite"]), ("__STEEL__", c["steel"]),
                       ("__ACCENT__", c["accent"]), ("__GLOW__", c["accent_glow"]), ("__PAPER__", c["paper"])):
        page = page.replace(key, value)
    return page
