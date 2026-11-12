/* WorkLoop frontend — plain JS, no build step. Talks to the FastAPI backend over REST + /ws. */
"use strict";

// ---------- config ----------
const DEFAULT_SERVER = "http://127.0.0.1:8000";
let SERVER = localStorage.getItem("workloop.server") || DEFAULT_SERVER;

// ---------- labels ----------
const RUN_STATE_FA = {
  idle: "بیکار", running: "در حال اجرا", waiting_permission: "منتظر اجازه",
  needs_user: "منتظر شما", stopping: "در حال توقف",
};
const STATUS_FA = {
  pending: "در صف", running: "در حال اجرا", done: "انجام شد", failed: "ناموفق", manual_fix: "اصلاح دستی",
  approved: "تأیید", rejected: "رد", denied: "رد شد", cancelled: "لغو شد", stopped: "متوقف",
  completed: "کامل", error: "خطا", finished: "تمام", killed: "کشته شد",
};
const KIND_FA = { task: "وظیفه", report: "گزارش", verdict: "رأی", thinking: "فکر", system: "سیستم" };
const AGENT_FA = { boss: "رئیس", worker: "کارگر" };
const ERROR_FA = {
  ROLES_NOT_SET: "اول برای رئیس و کارگر پروفایل انتخاب کنید.",
  PROJECT_NOT_SET: "اول پوشه پروژه را انتخاب کنید.",
  PROJECT_NOT_FOUND: "پوشه پروژه دیگر وجود ندارد.",
  NO_PENDING_PROMPTS: "اول یک پرامپت به صف اضافه کنید.",
  RUN_ACTIVE: "یک اجرا در جریان است.",
  API_TEST_FAILED: "اتصال به پروفایل رئیس یا کارگر برقرار نشد.",
  NAME_TAKEN: "پروفایلی با این نام وجود دارد.",
  PROFILE_IN_USE: "این پروفایل به رئیس یا کارگر اختصاص دارد؛ اول آن را عوض کنید.",
  PROMPT_LOCKED: "فقط پرامپت‌های در صف قابل ویرایش‌اند.",
  NOT_NEEDS_USER: "این کار فقط وقتی اجرا منتظر شماست ممکن است.",
  PERMISSION_RESOLVED: "این درخواست قبلاً پاسخ داده شده.",
};

// ---------- tiny helpers ----------
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const faNum = (n) => (n == null ? "—" : Number(n).toLocaleString("fa-IR"));
const faDate = (s) => (s ? new Date(s).toLocaleString("fa-IR", { dateStyle: "short", timeStyle: "short" }) : "—");
const badge = (status, text) => `<span class="badge ${esc(status)}">${esc(text ?? STATUS_FA[status] ?? status)}</span>`;
const ms = (n) => (n == null ? "" : n < 1000 ? `${faNum(n)} ms` : `${(n / 1000).toLocaleString("fa-IR", { maximumFractionDigits: 1 })} s`);

function toast(text, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), type === "error" ? 6500 : 3500);
}

class ApiErr extends Error {
  constructor(status, body) {
    const e = body?.error || {};
    super(ERROR_FA[e.code] || e.message || `HTTP ${status}`);
    this.code = e.code; this.details = e.details; this.status = status;
  }
}

async function api(method, path, body) {
  let res;
  try {
    res = await fetch(SERVER + "/api" + path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiErr(0, { error: { message: "سرور در دسترس نیست." } });
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiErr(res.status, data);
  return data;
}

function errText(e) {
  let t = e.message;
  const errs = e.details?.errors;
  if (Array.isArray(errs) && errs.length) t += " — " + errs.map((x) => `${x.field}: ${x.message}`).join("؛ ");
  if (e.code === "API_TEST_FAILED" && e.details) {
    const b = e.details.boss, w = e.details.worker;
    t += `\nرئیس: ${b?.ok ? "✓" : b?.message}\nکارگر: ${w?.ok ? "✓" : w?.message}`;
  }
  return t;
}
const fail = (e) => toast(errText(e), "error");

// ---------- app state ----------
const S = {
  view: "run",
  run: { state: "idle", run_id: null, prompt_id: null, round: null, max_rounds: 6 },
  prompts: [],
  profiles: [],
  providers: [],
  roles: null,
  project: null,
  needsUser: null,       // needs_user event data
  permissions: [],       // pending permission requests
  feed: [],              // live feed DOM nodes (kept across view switches)
};
const feedEl = document.createElement("div");
feedEl.className = "feed";
const messageEls = new Map();
const toolEls = new Map();
const cmdEls = new Map();

// ---------- header ----------
function renderHeader() {
  const r = S.run;
  const pill = $("#state-pill");
  pill.className = `pill ${r.state}`;
  pill.textContent = RUN_STATE_FA[r.state] || r.state;
  const parts = [];
  if (r.run_id) parts.push(`اجرای #${faNum(r.run_id)}`);
  if (r.prompt_id) parts.push(`پرامپت #${faNum(r.prompt_id)}`);
  if (r.round) parts.push(`دور ${faNum(r.round)} از ${faNum(r.max_rounds)}`);
  $("#state-detail").textContent = parts.join(" · ");
  const active = r.state !== "idle";
  $("#start-btn").hidden = active;
  $("#stop-btn").hidden = !active;
  $("#stop-btn").disabled = r.state === "stopping";
}

$("#start-btn").onclick = async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true; btn.textContent = "در حال تست اتصال…";
  try {
    const res = await api("POST", "/run/start");
    feedEl.innerHTML = ""; messageEls.clear(); toolEls.clear(); cmdEls.clear();
    S.needsUser = null;
    toast(`اجرای #${faNum(res.run_id)} شروع شد`, "ok");
    location.hash = "#run";
  } catch (e) { fail(e); }
  finally { btn.disabled = false; btn.textContent = "شروع اجرا"; }
};
$("#stop-btn").onclick = async () => {
  if (!confirm("اجرا متوقف شود؟")) return;
  try { await api("POST", "/run/stop"); } catch (e) { fail(e); }
};
$("#server-btn").onclick = () => {
  const v = prompt("آدرس سرور WorkLoop:", SERVER);
  if (!v) return;
  SERVER = v.replace(/\/+$/, "");
  localStorage.setItem("workloop.server", SERVER);
  location.reload();
};

// ---------- modal ----------
function modal(html, onMount) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="backdrop"><div class="modal">${html}</div></div>`;
  const close = () => (root.innerHTML = "");
  root.querySelector(".backdrop").addEventListener("mousedown", (e) => { if (e.target.classList.contains("backdrop")) close(); });
  onMount?.(root.querySelector(".modal"), close);
  return close;
}

// ---------- WebSocket ----------
let ws, wsRetry = 0, lastSeq = null;
function setConn(on, text) {
  $("#conn-dot").className = `dot ${on ? "on" : "off"}`;
  $("#conn-text").textContent = text;
}
function connectWs() {
  const url = SERVER.replace(/^http/, "ws") + "/ws";
  try { ws = new WebSocket(url); } catch { return scheduleWs(); }
  ws.onopen = () => { wsRetry = 0; setConn(true, "متصل"); };
  ws.onclose = () => { setConn(false, "قطع — تلاش مجدد…"); scheduleWs(); };
  ws.onmessage = (m) => {
    let ev; try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.type === "ping") return ws.send(JSON.stringify({ type: "pong" }));
    if (ev.type === "pong") return;
    if (ev.type === "hello") {
      const gap = lastSeq !== null && ev.seq !== lastSeq;
      lastSeq = ev.seq;
      S.run = ev.data.run_state; renderHeader();
      refreshAll(gap);
      return;
    }
    if (typeof ev.seq === "number") lastSeq = ev.seq;
    handleEvent(ev.type, ev.data);
  };
}
function scheduleWs() {
  const delay = Math.min(15000, 800 * 2 ** wsRetry++);
  setTimeout(connectWs, delay);
}

async function refreshAll() {
  await Promise.allSettled([loadPrompts(), loadPermissions(), loadRunState()]);
  rerender();
}
async function loadRunState() { S.run = await api("GET", "/run"); renderHeader(); }
async function loadPrompts() { S.prompts = await api("GET", "/prompts"); }
async function loadPermissions() {
  S.permissions = await api("GET", "/permissions/pending");
  showNextPermission();
}

// ---------- live events ----------
function feedAppend(node) {
  const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
  feedEl.appendChild(node);
  if (S.view === "run" && nearBottom) window.scrollTo(0, document.body.scrollHeight);
}
function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }

function handleEvent(type, d) {
  switch (type) {
    case "run_state":
      S.run = d; renderHeader();
      if (d.state !== "needs_user") S.needsUser = null;
      if (S.view === "run") renderRunSide();
      break;
    case "prompt_status": {
      const p = S.prompts.find((x) => x.id === d.prompt_id);
      if (p) { p.status = d.status; p.rounds_used = d.rounds_used; } else loadPrompts().then(rerender);
      if (S.view === "prompts") renderPrompts();
      if (S.view === "run") renderRunSide();
      break;
    }
    case "round_started":
      feedAppend(el(`<div class="round-sep"><span>پرامپت #${faNum(d.prompt_id)} — دور ${faNum(d.round)} از ${faNum(d.max_rounds)}</span></div>`));
      break;
    case "message_start": {
      const n = el(`<div class="msg ${d.agent} streaming">
        <div class="msg-head">${badge(d.agent, AGENT_FA[d.agent])}<span>${esc(KIND_FA[d.kind] || d.kind)}</span><span>· دور ${faNum(d.round)}</span></div>
        <div class="msg-body"></div></div>`);
      messageEls.set(d.message_id, n); feedAppend(n);
      break;
    }
    case "token": {
      const n = messageEls.get(d.message_id);
      if (n) n.querySelector(".msg-body").textContent += d.text;
      break;
    }
    case "message_end": {
      const n = messageEls.get(d.message_id);
      if (n) { n.classList.remove("streaming"); if (!n.querySelector(".msg-body").textContent.trim()) n.remove(); }
      break;
    }
    case "tool_call": {
      const arg = toolArgPreview(d.name, d.arguments);
      const n = el(`<div class="tool">${badge(d.agent, AGENT_FA[d.agent])}<span>🔧 <code class="mono">${esc(d.name)}</code></span>
        <code class="mono">${esc(arg)}</code><span class="res muted">…</span></div>`);
      toolEls.set(d.call_id, n); feedAppend(n);
      break;
    }
    case "tool_result": {
      const n = toolEls.get(d.call_id);
      if (!n) break;
      n.querySelector(".res").outerHTML = d.ok ? badge("ok", "✓") : badge("bad", "✗");
      if (d.summary && !d.ok) n.appendChild(el(`<div class="sum">${esc(d.summary)}</div>`));
      break;
    }
    case "command_started": {
      const n = el(`<div class="cmd"><div class="cmd-head"><span>$</span><code class="mono">${esc(d.command)}</code><span class="grow"></span><span class="st">در حال اجرا…</span></div>
        ${d.reason ? `<div class="hint" style="padding:4px 12px;background:#efeadf">${esc(d.reason)}</div>` : ""}<pre></pre></div>`);
      cmdEls.set(d.command_id, n); feedAppend(n);
      break;
    }
    case "command_output": {
      const n = cmdEls.get(d.command_id);
      if (!n) break;
      const pre = n.querySelector("pre");
      const span = document.createElement("span");
      if (d.stream === "stderr") span.className = "err";
      span.textContent = d.text; pre.appendChild(span);
      pre.scrollTop = pre.scrollHeight;
      break;
    }
    case "command_finished": {
      const n = cmdEls.get(d.command_id);
      if (!n) break;
      let st = d.timed_out ? badge("bad", "پایان زمان") : d.killed ? badge("stopped", "کشته شد")
        : d.exit_code === 0 ? badge("ok", "کد ۰") : badge("bad", `کد ${d.exit_code}`);
      n.querySelector(".st").outerHTML = `<span>${st} <span class="muted" style="color:#a9b5ae">${ms(d.duration_ms)}</span></span>`;
      const pre = n.querySelector("pre"); if (!pre.textContent) pre.remove();
      break;
    }
    case "permission_request":
      S.permissions.push({ id: d.permission_id, command: d.command, reason: d.reason, danger_reason: d.danger_reason });
      showNextPermission();
      break;
    case "permission_resolved":
      S.permissions = S.permissions.filter((p) => p.id !== d.permission_id);
      if (currentPermissionId === d.permission_id) { closePermission?.(); currentPermissionId = null; showNextPermission(); }
      feedAppend(el(`<div class="tool">🛡️ درخواست اجازه #${faNum(d.permission_id)} ${d.approved ? badge("ok", "تأیید شد") : badge("bad", "رد شد")}</div>`));
      break;
    case "verdict":
      feedAppend(el(verdictHtml(d)));
      break;
    case "prompt_done":
      toast(`پرامپت #${faNum(d.prompt_id)} در ${faNum(d.rounds_used)} دور انجام شد ✓`, "ok");
      break;
    case "needs_user":
      S.needsUser = d;
      toast("پرامپت بعد از همه دورها قبول نشد — منتظر تصمیم شما.", "error");
      if (S.view === "run") renderRunSide(); else location.hash = "#run";
      break;
    case "manual_fix_applied":
      feedAppend(el(`<div class="tool">✍️ اصلاح دستی ثبت شد — افزوده: ${faNum(d.added.length)}، حذف: ${faNum(d.removed.length)}، تغییر: ${faNum(d.modified.length)}</div>`));
      break;
    case "queue_empty":
      toast("صف خالی شد؛ همه پرامپت‌ها اجرا شدند.", "ok");
      feedAppend(el(`<div class="round-sep"><span>پایان صف</span></div>`));
      break;
    case "error":
      toast(`${d.message}`, "error");
      feedAppend(el(`<div class="alert danger"><strong>${esc(d.code)}</strong> — ${esc(d.message)}</div>`));
      break;
  }
}

function toolArgPreview(name, a) {
  if (!a || typeof a !== "object") return "";
  const v = a.path ?? a.command ?? a.pattern ?? a.query ?? Object.values(a).find((x) => typeof x === "string") ?? "";
  const s = String(v);
  return s.length > 90 ? s.slice(0, 87) + "…" : s;
}

function verdictHtml(v) {
  const bugs = (v.bugs || []).map((b) => `<li>${esc(typeof b === "string" ? b : b.description || b.title || JSON.stringify(b))}</li>`).join("");
  const fixes = (v.fix_instructions || []).map((f) => `<li>${esc(typeof f === "string" ? f : JSON.stringify(f))}</li>`).join("");
  return `<div class="verdict ${esc(v.status)}">
    <div class="row">${badge(v.status, v.status === "approved" ? "رئیس تأیید کرد" : "رئیس رد کرد")}<span class="muted">دور ${faNum(v.round)}</span></div>
    <div class="msg-body">${esc(v.summary)}</div>
    ${bugs ? `<strong>باگ‌ها:</strong><ul>${bugs}</ul>` : ""}
    ${fixes ? `<strong>دستور اصلاح:</strong><ul>${fixes}</ul>` : ""}</div>`;
}

// ---------- permission modal ----------
let currentPermissionId = null, closePermission = null;
function showNextPermission() {
  if (currentPermissionId !== null || !S.permissions.length) return;
  const p = S.permissions[0];
  currentPermissionId = p.id;
  closePermission = modal(`
    <h3>⚠️ رئیس می‌خواهد یک دستور خطرناک اجرا کند</h3>
    <div class="cmd"><div class="cmd-head"><span>$</span><code class="mono">${esc(p.command)}</code></div></div>
    <dl class="kv" style="margin-top:12px"><dt>دلیل:</dt><dd>${esc(p.reason)}</dd><dt>خطر:</dt><dd style="color:var(--danger)">${esc(p.danger_reason)}</dd></dl>
    <div class="modal-actions"><button class="btn primary" data-a="1">اجازه بده</button><button class="btn danger" data-a="0">رد کن</button></div>`,
  (m, close) => {
    m.querySelectorAll("[data-a]").forEach((b) => (b.onclick = async () => {
      m.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try { await api("POST", `/permissions/${p.id}`, { approved: b.dataset.a === "1" }); }
      catch (e) { if (e.code !== "PERMISSION_RESOLVED") fail(e); }
      S.permissions = S.permissions.filter((x) => x.id !== p.id);
      close(); currentPermissionId = null; showNextPermission();
    }));
  });
}

// ---------- views ----------
const view = () => $("#view");
function rerender() {
  ({ run: renderRun, prompts: renderPrompts, profiles: renderProfiles, setup: renderSetup, history: renderHistory }[S.view] || renderRun)();
}
function route() {
  const [name, arg] = location.hash.replace("#", "").split("/");
  S.view = ["run", "prompts", "profiles", "setup", "history"].includes(name) ? name : "run";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === S.view));
  if (S.view === "history" && arg) return renderRunDetail(Number(arg));
  rerender();
}
window.addEventListener("hashchange", route);

// ----- run view -----
function renderRun() {
  view().innerHTML = `<div class="run-layout"><div><div id="needs"></div><div id="feed-wrap"></div></div><div class="run-side" id="run-side"></div></div>`;
  $("#feed-wrap").appendChild(feedEl);
  if (!feedEl.children.length) {
    $("#feed-wrap").insertAdjacentHTML("afterbegin", `<div class="empty" id="feed-empty">هنوز رویدادی نیامده. وقتی اجرا شروع شود، گفتگوی رئیس و کارگر اینجا زنده نمایش داده می‌شود.</div>`);
  }
  renderRunSide();
}
function renderRunSide() {
  const side = $("#run-side"); if (!side) return;
  if (feedEl.children.length) $("#feed-empty")?.remove();
  const cur = S.prompts.find((p) => p.id === S.run.prompt_id);
  const pending = S.prompts.filter((p) => p.status === "pending");
  side.innerHTML = `
    <div class="card"><h3>پرامپت فعلی</h3>
      ${cur ? `<div class="q-text" style="max-height:180px;overflow:auto">${esc(cur.content)}</div><div class="row" style="margin-top:6px">${badge(cur.status)}<span class="muted">${faNum(cur.rounds_used)} دور</span></div>` : `<div class="muted">—</div>`}
    </div>
    <div class="card"><h3>صف</h3>
      <div class="muted">${faNum(pending.length)} پرامپت در انتظار</div>
      ${pending.slice(0, 4).map((p) => `<div class="hint" style="border-top:1px solid var(--line);padding-top:4px;margin-top:4px">${faNum(p.position)}. ${esc(p.content.slice(0, 70))}${p.content.length > 70 ? "…" : ""}</div>`).join("")}
      <a href="#prompts" class="hint">مدیریت صف ←</a>
    </div>`;
  const needs = $("#needs"); if (!needs) return;
  if (S.run.state === "needs_user") {
    const n = S.needsUser;
    needs.innerHTML = `<div class="alert">
      <h3>⏸ پرامپت ${n ? `#${faNum(n.prompt_id)} ` : ""}بعد از ${n ? faNum(n.rounds_used) : "همه"} دور تأیید نشد</h3>
      ${n?.summary ? `<div class="msg-body">${esc(n.summary)}</div>` : ""}
      ${n?.bugs?.length ? `<ul>${n.bugs.map((b) => `<li>${esc(typeof b === "string" ? b : b.description || JSON.stringify(b))}</li>`).join("")}</ul>` : ""}
      <div class="field" style="margin-top:10px"><label>پرامپت اصلاحی (بلافاصله بعد از پرامپت ناموفق اجرا می‌شود)</label><textarea id="fix-text" placeholder="مثلاً: خطای import در فایل … را درست کن"></textarea></div>
      <div class="row"><button class="btn primary" id="fix-btn">ارسال پرامپت اصلاحی و ادامه</button>
      <span class="muted">یا</span>
      <button class="btn" id="manual-btn">خودم دستی درستش کردم</button><button class="btn ghost-danger" id="needs-stop">توقف اجرا</button></div>
    </div>`;
    $("#fix-btn").onclick = async () => {
      const text = $("#fix-text").value.trim();
      if (!text) return toast("متن پرامپت اصلاحی را بنویسید.", "error");
      try { await api("POST", "/run/fix-prompt", { text }); S.needsUser = null; await loadPrompts(); renderRunSide(); toast("پرامپت اصلاحی در صف قرار گرفت.", "ok"); } catch (e) { fail(e); }
    };
    $("#manual-btn").onclick = () => modal(`<h3>اصلاح دستی</h3>
      <p class="muted">تغییراتی که در پوشه پروژه داده‌اید خوانده می‌شود و اجرا ادامه پیدا می‌کند.</p>
      <div class="field"><label>یادداشت (اختیاری)</label><textarea id="mf-note"></textarea></div>
      <div class="modal-actions"><button class="btn primary" id="mf-ok">ثبت و ادامه</button><button class="btn" id="mf-cancel">انصراف</button></div>`,
      (m, close) => {
        $("#mf-cancel", m).onclick = close;
        $("#mf-ok", m).onclick = async () => {
          const note = $("#mf-note", m).value.trim();
          try { await api("POST", "/run/manual-fix", note ? { note } : {}); close(); S.needsUser = null; renderRunSide(); } catch (e) { fail(e); }
        };
      });
    $("#needs-stop").onclick = () => $("#stop-btn").click();
  } else needs.innerHTML = "";
}

// ----- prompts view -----
async function renderPrompts() {
  view().innerHTML = `<h2>صف پرامپت‌ها</h2>
    <div class="card">
      <div class="field"><label>پرامپت جدید</label><textarea id="new-prompt" placeholder="کاری که می‌خواهید کارگر انجام دهد و رئیس بررسی کند…"></textarea></div>
      <div class="row"><button class="btn primary" id="add-prompt">افزودن به انتهای صف</button><span class="hint">Ctrl+Enter</span></div>
    </div><div id="q-list"></div>`;
  const add = async () => {
    const content = $("#new-prompt").value.trim();
    if (!content) return;
    try { await api("POST", "/prompts", { content }); $("#new-prompt").value = ""; await loadPrompts(); drawQueue(); } catch (e) { fail(e); }
  };
  $("#add-prompt").onclick = add;
  $("#new-prompt").onkeydown = (e) => { if (e.key === "Enter" && e.ctrlKey) add(); };
  drawQueue();
  try { await loadPrompts(); drawQueue(); } catch (e) { fail(e); }
}
function drawQueue() {
  const list = $("#q-list"); if (!list) return;
  if (!S.prompts.length) { list.innerHTML = `<div class="empty">صف خالی است.</div>`; return; }
  const pending = S.prompts.filter((p) => p.status === "pending");
  list.innerHTML = S.prompts.map((p) => {
    const locked = p.status !== "pending";
    const i = pending.findIndex((x) => x.id === p.id);
    return `<div class="card q-item ${locked ? "locked" : ""}" data-id="${p.id}">
      <div class="q-pos">${faNum(p.position)}</div>
      <div><div class="q-text">${esc(p.content)}</div>
        <div class="row hint" style="margin-top:4px">${badge(p.status)}${p.rounds_used ? `<span>${faNum(p.rounds_used)} دور</span>` : ""}<span>#${faNum(p.id)}</span></div></div>
      <div class="q-actions">${locked ? "" : `
        <button class="btn sm" data-act="up" ${i <= 0 ? "disabled" : ""} title="بالا">▲</button>
        <button class="btn sm" data-act="down" ${i === pending.length - 1 ? "disabled" : ""} title="پایین">▼</button>
        <button class="btn sm" data-act="edit">ویرایش</button>
        <button class="btn sm ghost-danger" data-act="del">حذف</button>`}</div></div>`;
  }).join("");
  list.querySelectorAll("[data-act]").forEach((b) => (b.onclick = () => promptAction(Number(b.closest(".q-item").dataset.id), b.dataset.act)));
}
async function promptAction(id, act) {
  const p = S.prompts.find((x) => x.id === id);
  try {
    if (act === "del") {
      if (!confirm("این پرامپت حذف شود؟")) return;
      await api("DELETE", `/prompts/${id}`);
    } else if (act === "up" || act === "down") {
      const ids = S.prompts.filter((x) => x.status === "pending").map((x) => x.id);
      const i = ids.indexOf(id), j = act === "up" ? i - 1 : i + 1;
      [ids[i], ids[j]] = [ids[j], ids[i]];
      await api("PUT", "/prompts/order", { prompt_ids: ids });
    } else if (act === "edit") {
      return modal(`<h3>ویرایش پرامپت #${faNum(id)}</h3><textarea id="ed" style="min-height:180px">${esc(p.content)}</textarea>
        <div class="modal-actions"><button class="btn primary" id="ed-ok">ذخیره</button><button class="btn" id="ed-x">انصراف</button></div>`,
        (m, close) => {
          $("#ed-x", m).onclick = close;
          $("#ed-ok", m).onclick = async () => {
            try { await api("PUT", `/prompts/${id}`, { content: $("#ed", m).value }); close(); await loadPrompts(); drawQueue(); } catch (e) { fail(e); }
          };
        });
    }
    await loadPrompts(); drawQueue();
  } catch (e) { fail(e); await loadPrompts(); drawQueue(); }
}

// ----- profiles view -----
async function renderProfiles() {
  view().innerHTML = `<h2>پروفایل‌های API</h2><div class="row" style="margin-bottom:12px"><button class="btn primary" id="new-prof">+ پروفایل جدید</button></div><div id="prof-list"><div class="muted">در حال بارگذاری…</div></div>`;
  $("#new-prof").onclick = () => profileForm(null);
  try {
    [S.profiles, S.providers] = await Promise.all([api("GET", "/profiles"), S.providers.length ? S.providers : api("GET", "/providers")]);
  } catch (e) { fail(e); }
  drawProfiles();
}
function drawProfiles() {
  const list = $("#prof-list"); if (!list) return;
  if (!S.profiles.length) { list.innerHTML = `<div class="empty">هنوز پروفایلی نساخته‌اید. برای رئیس و کارگر دست‌کم یک پروفایل لازم است.</div>`; return; }
  const pname = (id) => S.providers.find((p) => p.id === id)?.name || id;
  list.innerHTML = `<div class="card" style="padding:4px 8px"><table><thead><tr><th>نام</th><th>ارائه‌دهنده / مدل</th><th>کلید</th><th>آخرین تست</th><th></th></tr></thead><tbody>
    ${S.profiles.map((p) => `<tr data-id="${p.id}">
      <td><strong>${esc(p.name)}</strong></td>
      <td>${esc(pname(p.provider))}<div class="mono hint">${esc(p.model_name)}</div></td>
      <td class="mono hint">${esc(p.key_preview)}</td>
      <td>${p.last_test_ok == null ? `<span class="muted">—</span>` : badge(p.last_test_ok ? "ok" : "bad", p.last_test_ok ? "موفق" : "ناموفق")}
        ${p.last_test_message && !p.last_test_ok ? `<div class="hint">${esc(p.last_test_message)}</div>` : ""}<div class="hint">${faDate(p.last_tested_at)}</div></td>
      <td><div class="q-actions"><button class="btn sm" data-a="test">تست</button><button class="btn sm" data-a="edit">ویرایش</button><button class="btn sm ghost-danger" data-a="del">حذف</button></div></td></tr>`).join("")}
    </tbody></table></div>`;
  list.querySelectorAll("[data-a]").forEach((b) => (b.onclick = async () => {
    const id = Number(b.closest("tr").dataset.id);
    const p = S.profiles.find((x) => x.id === id);
    if (b.dataset.a === "edit") return profileForm(p);
    if (b.dataset.a === "del") {
      if (!confirm(`پروفایل «${p.name}» حذف شود؟`)) return;
      try { await api("DELETE", `/profiles/${id}`); toast("حذف شد", "ok"); } catch (e) { return fail(e); }
    }
    if (b.dataset.a === "test") {
      b.disabled = true; b.textContent = "…";
      try { const r = await api("POST", `/profiles/${id}/test`); toast(r.ok ? `اتصال موفق (${ms(r.latency_ms)})` : r.message, r.ok ? "ok" : "error"); } catch (e) { fail(e); }
    }
    S.profiles = await api("GET", "/profiles"); drawProfiles();
  }));
}
function profileForm(p) {
  const opts = S.providers.map((x) => `<option value="${esc(x.id)}" ${p?.provider === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("");
  modal(`<h3>${p ? `ویرایش «${esc(p.name)}»` : "پروفایل جدید"}</h3>
    <div class="field"><label>نام</label><input type="text" id="f-name" maxlength="50" value="${esc(p?.name)}" /></div>
    <div class="field"><label>ارائه‌دهنده</label><select id="f-prov">${opts}</select></div>
    <div class="field"><label>مدل</label><input type="text" id="f-model" class="mono" list="f-models" value="${esc(p?.model_name)}" /><datalist id="f-models"></datalist></div>
    <div class="field"><label>کلید API ${p ? `<span class="hint">(خالی بگذارید تا کلید فعلی ${esc(p.key_preview)} بماند)</span>` : ""}</label><input type="password" id="f-key" class="mono" autocomplete="off" /></div>
    <div class="field" id="f-base-wrap"><label>Base URL <span class="hint" id="f-base-hint"></span></label><input type="url" id="f-base" class="mono" value="${esc(p?.base_url)}" /></div>
    <div id="f-test-res" class="hint"></div>
    <div class="modal-actions"><button class="btn primary" id="f-save">ذخیره</button><button class="btn" id="f-test">تست اتصال</button><button class="btn" id="f-x">انصراف</button></div>`,
  (m, close) => {
    const prov = () => S.providers.find((x) => x.id === $("#f-prov", m).value);
    const sync = () => {
      const info = prov(); if (!info) return;
      $("#f-models", m).innerHTML = info.example_models.map((x) => `<option value="${esc(x)}">`).join("");
      $("#f-model", m).placeholder = info.example_models[0] || "";
      $("#f-base", m).placeholder = info.default_base_url || "";
      $("#f-base-hint", m).textContent = info.requires_base_url ? "(الزامی)" : "(اختیاری)";
    };
    $("#f-prov", m).onchange = sync; sync();
    $("#f-x", m).onclick = close;
    const body = () => ({
      name: $("#f-name", m).value, provider: $("#f-prov", m).value, model_name: $("#f-model", m).value,
      api_key: $("#f-key", m).value, base_url: $("#f-base", m).value,
    });
    $("#f-test", m).onclick = async (ev) => {
      const b = body(); const out = $("#f-test-res", m);
      if (!b.api_key && p) { out.textContent = "برای تست قبل از ذخیره، کلید را وارد کنید (یا از دکمه «تست» در جدول استفاده کنید)."; return; }
      ev.target.disabled = true; out.textContent = "در حال تست…";
      try {
        const r = await api("POST", "/profiles/test", { provider: b.provider, api_key: b.api_key, model_name: b.model_name, base_url: b.base_url });
        out.innerHTML = r.ok ? `<span style="color:var(--ok)">✓ اتصال موفق — ${ms(r.latency_ms)}</span>` : `<span style="color:var(--danger)">✗ ${esc(r.message)}</span>`;
      } catch (e) { out.innerHTML = `<span style="color:var(--danger)">${esc(errText(e))}</span>`; }
      ev.target.disabled = false;
    };
    $("#f-save", m).onclick = async () => {
      const b = body();
      try {
        if (p) { if (!b.api_key) delete b.api_key; await api("PUT", `/profiles/${p.id}`, b); }
        else await api("POST", "/profiles", b);
        close(); toast("ذخیره شد", "ok");
        S.profiles = await api("GET", "/profiles"); drawProfiles();
      } catch (e) { fail(e); }
    };
  });
}

// ----- setup view (roles + project) -----
async function renderSetup() {
  view().innerHTML = `<h2>نقش‌ها و پروژه</h2><div class="grid2"><div class="card" id="roles-card">…</div><div class="card" id="proj-card">…</div></div>`;
  try {
    [S.profiles, S.roles, S.project] = await Promise.all([api("GET", "/profiles"), api("GET", "/roles"), api("GET", "/project")]);
  } catch (e) { return fail(e); }
  const locked = S.run.state !== "idle";
  const sel = (id, cur) => `<select id="${id}" ${locked ? "disabled" : ""}><option value="">— انتخاب کنید —</option>${S.profiles.map((p) => `<option value="${p.id}" ${cur?.id === p.id ? "selected" : ""}>${esc(p.name)} · ${esc(p.model_name)}</option>`).join("")}</select>`;
  $("#roles-card").innerHTML = `<h3>نقش‌ها</h3>
    <p class="hint">رئیس برنامه‌ریزی، تست و داوری می‌کند؛ کارگر کد می‌نویسد. می‌توانند یک پروفایل باشند.</p>
    <div class="field"><label>${badge("boss", "رئیس")}</label>${sel("r-boss", S.roles.boss)}</div>
    <div class="field"><label>${badge("worker", "کارگر")}</label>${sel("r-worker", S.roles.worker)}</div>
    <button class="btn primary" id="r-save" ${locked ? "disabled" : ""}>ذخیره نقش‌ها</button>
    ${!S.profiles.length ? `<p class="hint">اول در <a href="#profiles">پروفایل‌ها</a> یکی بسازید.</p>` : ""}
    ${locked ? `<p class="hint">در حین اجرا قابل تغییر نیست.</p>` : ""}`;
  $("#r-save").onclick = async () => {
    const b = Number($("#r-boss").value), w = Number($("#r-worker").value);
    if (!b || !w) return toast("هر دو نقش را انتخاب کنید.", "error");
    try { S.roles = await api("PUT", "/roles", { boss_profile_id: b, worker_profile_id: w }); toast("نقش‌ها ذخیره شد", "ok"); } catch (e) { fail(e); }
  };
  const pj = S.project;
  $("#proj-card").innerHTML = `<h3>پوشه پروژه</h3>
    <p class="hint">پوشه‌ای که عامل‌ها در آن فایل می‌خوانند/می‌نویسند و دستور اجرا می‌کنند.</p>
    ${pj.path ? `<dl class="kv"><dt>نام:</dt><dd>${esc(pj.name)}</dd><dt>مسیر:</dt><dd class="mono">${esc(pj.path)}</dd><dt>وضعیت:</dt><dd>${pj.exists ? `${badge("ok", "موجود")} ${faNum(pj.file_count)} فایل` : badge("bad", "پیدا نشد")}</dd></dl>` : `<div class="muted">هنوز انتخاب نشده.</div>`}
    <div class="field" style="margin-top:12px"><label>مسیر کامل پوشه</label><input type="text" id="p-path" class="mono" placeholder="D:\\Projects\\my-app" value="${esc(pj.path)}" ${locked ? "disabled" : ""}/></div>
    <button class="btn primary" id="p-save" ${locked ? "disabled" : ""}>ذخیره پوشه</button>`;
  $("#p-save").onclick = async () => {
    try { S.project = await api("PUT", "/project", { path: $("#p-path").value }); toast("پوشه پروژه ذخیره شد", "ok"); renderSetup(); } catch (e) { fail(e); }
  };
}

// ----- history -----
let histOffset = 0;
async function renderHistory() {
  view().innerHTML = `<h2>تاریخچه اجراها</h2><div id="h-list" class="muted">در حال بارگذاری…</div>`;
  let data; try { data = await api("GET", `/runs?limit=20&offset=${histOffset}`); } catch (e) { return fail(e); }
  if (!data.items.length) { $("#h-list").innerHTML = `<div class="empty">هنوز اجرایی ثبت نشده.</div>`; return; }
  $("#h-list").innerHTML = `<div class="card" style="padding:4px 8px"><table><thead><tr><th>#</th><th>وضعیت</th><th>شروع</th><th>رئیس / کارگر</th><th>نتیجه</th><th>پروژه</th></tr></thead><tbody>
    ${data.items.map((r) => `<tr class="clickable" data-id="${r.id}"><td>${faNum(r.id)}</td><td>${badge(r.status)}</td><td>${faDate(r.started_at)}</td>
      <td>${esc(r.boss?.name || "—")} / ${esc(r.worker?.name || "—")}</td>
      <td>${badge("ok", `${faNum(r.prompts_done)} ✓`)} ${r.prompts_failed ? badge("bad", `${faNum(r.prompts_failed)} ✗`) : ""}</td>
      <td class="mono hint">${esc(r.project_path)}</td></tr>`).join("")}</tbody></table></div>
    <div class="row"><button class="btn sm" id="h-prev" ${histOffset === 0 ? "disabled" : ""}>قبلی</button>
    <span class="muted">${faNum(histOffset + 1)}–${faNum(histOffset + data.items.length)} از ${faNum(data.total)}</span>
    <button class="btn sm" id="h-next" ${histOffset + data.items.length >= data.total ? "disabled" : ""}>بعدی</button></div>`;
  $("#h-list").querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => (location.hash = `#history/${tr.dataset.id}`)));
  $("#h-prev").onclick = () => { histOffset = Math.max(0, histOffset - 20); renderHistory(); };
  $("#h-next").onclick = () => { histOffset += 20; renderHistory(); };
}

async function renderRunDetail(id) {
  view().innerHTML = `<div class="muted" style="margin-top:20px">در حال بارگذاری…</div>`;
  let d; try { d = await api("GET", `/runs/${id}?include_tool_messages=false`); } catch (e) { return fail(e); }
  const r = d.run;
  // Merge timeline: messages, commands, verdicts, permissions, manual fixes — by time.
  const items = [
    ...d.messages.filter((m) => m.content?.trim()).map((m) => ({ t: m.created_at, html: `<div class="msg ${m.agent}"><div class="msg-head">${badge(m.agent, AGENT_FA[m.agent])}<span>${esc(KIND_FA[m.kind] || m.kind)}</span>${m.round ? `<span>· دور ${faNum(m.round)}</span>` : ""}<span>· ${faDate(m.created_at)}</span></div><div class="msg-body">${esc(m.content)}</div></div>` })),
    ...d.commands.map((c) => ({ t: c.created_at, html: `<div class="cmd"><div class="cmd-head"><span>$</span><code class="mono">${esc(c.command)}</code><span class="grow"></span>${c.timed_out ? badge("bad", "پایان زمان") : c.killed ? badge("stopped", "کشته شد") : c.exit_code === 0 ? badge("ok", "کد ۰") : badge("bad", `کد ${c.exit_code ?? "?"}`)}<span style="color:#a9b5ae">${ms(c.duration_ms)}</span></div>${c.stdout || c.stderr ? `<pre>${esc(c.stdout || "")}<span class="err">${esc(c.stderr || "")}</span></pre>` : ""}</div>` })),
    ...d.verdicts.map((v) => ({ t: v.created_at, html: verdictHtml(v) })),
    ...d.permissions.map((p) => ({ t: p.created_at, html: `<div class="tool">🛡️ <code class="mono">${esc(p.command)}</code> ${badge(p.status)}<div class="sum" style="direction:rtl;text-align:right;font-family:inherit">${esc(p.danger_reason)}</div></div>` })),
    ...d.manual_fixes.map((f) => ({ t: f.created_at, html: `<div class="tool">✍️ اصلاح دستی — افزوده ${faNum(f.added.length)}، حذف ${faNum(f.removed.length)}، تغییر ${faNum(f.modified.length)}${f.note ? ` — ${esc(f.note)}` : ""}</div>` })),
  ].sort((a, b) => new Date(a.t) - new Date(b.t));
  view().innerHTML = `<div class="row" style="margin-top:18px"><a href="#history" class="btn sm">→ بازگشت</a><h2 style="margin:0">اجرای #${faNum(r.id)}</h2>${badge(r.status)}</div>
    <div class="run-layout"><div class="feed">${items.map((x) => x.html).join("") || `<div class="empty">پیامی ثبت نشده.</div>`}</div>
    <div class="run-side"><div class="card"><dl class="kv">
      <dt>شروع:</dt><dd>${faDate(r.started_at)}</dd><dt>پایان:</dt><dd>${faDate(r.finished_at)}</dd>
      <dt>رئیس:</dt><dd>${esc(r.boss?.name || "—")}</dd><dt>کارگر:</dt><dd>${esc(r.worker?.name || "—")}</dd>
      <dt>پروژه:</dt><dd class="mono">${esc(r.project_path)}</dd></dl>
      ${r.error_message ? `<div class="alert danger" style="margin-top:10px">${esc(r.error_message)}</div>` : ""}</div>
      <div class="card"><h3>پرامپت‌ها</h3>${d.prompts.map((p) => `<div style="border-top:1px solid var(--line);padding:6px 0"><div class="row">${badge(p.status)}<span class="hint">${faNum(p.rounds_used)} دور</span></div><div class="hint q-text">${esc(p.text)}</div></div>`).join("")}</div>
    </div></div>`;
}

// ---------- boot ----------
(async function boot() {
  renderHeader();
  route();
  try { await api("GET", "/health"); } catch { setConn(false, "سرور در دسترس نیست"); }
  connectWs();
  api("GET", "/providers").then((p) => (S.providers = p)).catch(() => {});
})();
