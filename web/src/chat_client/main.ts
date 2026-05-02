import { LS_TOKEN, LS_FAMILY, $, resolveTheme, setupThemeToggle } from "../shared/site";
import type { SiteConfig } from "../shared/site";

const LS_USERNAME = "wcg.username";
const LS_STORE = "wcg.chat.sessions";
// Legacy keys — read once for migration, NEVER removed (manual rollback path).
const LS_LEGACY_SESSION = "wcg.sessionId";
const SS_LEGACY_HISTORY = "wcg.history";

const CHAT_URL = "/api/webchat/chat";
const ME_URL = "/api/webchat/me";
const SITE_URL = "/api/webchat/site";

const TITLE_MAX = 25;
const TRASH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6"/></svg>';

const token = (localStorage.getItem(LS_TOKEN) || "").trim();
if (!token) { location.replace("/login"); throw new Error("redirecting to /login"); }

type Role = "user" | "bot" | "error" | "notice";
interface HistoryItem { role: Role; text: string; ts: number; }
interface SessionMeta { id: string; title: string; lastActiveAt: number; history: HistoryItem[]; }
interface ChatStore { activeId: string; sessions: Record<string, SessionMeta>; }

const inputEl = $<HTMLTextAreaElement>("input");
const sendBtn = $<HTMLButtonElement>("send");
const msgs = $("messages");
const badge = $("quotaBadge");
const whoEl = $("who");
const sidebarEl = $("sidebar");
const sidebarToggleBtn = $<HTMLButtonElement>("sidebarToggle");
const sidebarBackdrop = $("sidebarBackdrop");
const sessionListEl = $<HTMLUListElement>("sessionList");

const username = (localStorage.getItem(LS_USERNAME) || "Friend").trim() || "Friend";
const strong = document.createElement("strong");
strong.textContent = username;
whoEl.append("你好，", strong);
whoEl.hidden = false;

const newId = (): string => crypto.randomUUID?.() ?? `s-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
const blankSession = (id?: string): SessionMeta =>
  ({ id: id ?? newId(), title: "新会话", lastActiveAt: Date.now(), history: [] });

function isHistoryItem(it: unknown): it is HistoryItem {
  if (!it || typeof it !== "object") return false;
  const o = it as Record<string, unknown>;
  return typeof o.text === "string" && typeof o.ts === "number" &&
    (o.role === "user" || o.role === "bot" || o.role === "error" || o.role === "notice");
}
function isSessionMeta(it: unknown): it is SessionMeta {
  if (!it || typeof it !== "object") return false;
  const o = it as Record<string, unknown>;
  return typeof o.id === "string" && typeof o.title === "string" &&
    typeof o.lastActiveAt === "number" && Array.isArray(o.history) && o.history.every(isHistoryItem);
}

// Crash-tolerant: never throws. On corrupt JSON / shape mismatch, falls back
// to legacy migration or fresh blank.
function loadStore(): ChatStore {
  let parsed: unknown = null;
  try { parsed = JSON.parse(localStorage.getItem(LS_STORE) || "null"); } catch {}
  if (!parsed || typeof parsed !== "object") return migrateLegacy();
  const p = parsed as Record<string, unknown>;
  const sessIn = (p.sessions && typeof p.sessions === "object") ? p.sessions as Record<string, unknown> : {};
  const sessions: Record<string, SessionMeta> = {};
  for (const [k, v] of Object.entries(sessIn)) if (isSessionMeta(v) && v.id === k) sessions[k] = v;
  let activeId = typeof p.activeId === "string" && sessions[p.activeId] ? p.activeId : "";
  if (!activeId) {
    let best = "";
    for (const [id, s] of Object.entries(sessions)) if (!best || s.lastActiveAt > sessions[best]!.lastActiveAt) best = id;
    activeId = best;
  }
  if (!activeId) { const fresh = blankSession(); sessions[fresh.id] = fresh; activeId = fresh.id; }
  return { activeId, sessions };
}

// One-shot read of legacy keys. We DO NOT remove them — leaving them gives
// users a manual rollback path in case the new store layout regresses.
function migrateLegacy(): ChatStore {
  const legacyId = (localStorage.getItem(LS_LEGACY_SESSION) || "").trim();
  let legacyHistory: HistoryItem[] = [];
  try {
    const raw: unknown = JSON.parse(sessionStorage.getItem(SS_LEGACY_HISTORY) || "[]");
    if (Array.isArray(raw)) legacyHistory = raw.filter(isHistoryItem);
  } catch {}
  const sess = blankSession(legacyId || undefined);
  if (legacyHistory.length) {
    sess.history = legacyHistory;
    sess.title = deriveTitle(legacyHistory);
    sess.lastActiveAt = legacyHistory[legacyHistory.length - 1]!.ts;
  }
  return { activeId: sess.id, sessions: { [sess.id]: sess } };
}

function deriveTitle(history: HistoryItem[]): string {
  const first = history.find((h) => h.role === "user");
  if (!first) return "新会话";
  const t = first.text.slice(0, TITLE_MAX);
  return first.text.length > TITLE_MAX ? t + "…" : t;
}

const store: ChatStore = loadStore();
const saveStore = (): void => { try { localStorage.setItem(LS_STORE, JSON.stringify(store)); } catch {} };
const currentSession = (): SessionMeta => store.sessions[store.activeId]!;

function replayActive(): void {
  clearMsgList();
  for (const item of currentSession().history) addMessage(item.role, item.text, true);
  scrollToEnd();
}

function addMessage(role: Role, text: string, skipPersist = false): void {
  hideTyping();
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
  msgs.appendChild(div);
  if (!skipPersist && (role === "user" || role === "bot")) {
    const sess = currentSession();
    sess.history.push({ role, text, ts: Date.now() });
    sess.lastActiveAt = Date.now();
    if (role === "user" && (sess.title === "新会话" || !sess.title)) sess.title = deriveTitle(sess.history);
    saveStore();
    renderSessionList();
  }
  scrollToEnd();
}

const scrollToEnd = (): void => { msgs.scrollTop = msgs.scrollHeight; };
const clearMsgList = (): void => { msgs.querySelectorAll(".msg").forEach((m) => m.remove()); };

function showTyping(): void {
  if (document.getElementById("_typing")) return;
  const t = document.createElement("div");
  t.id = "_typing";
  t.className = "msg bot typing";
  t.setAttribute("aria-label", "对方正在输入");
  for (let i = 0; i < 3; i++) { const dot = document.createElement("span"); dot.className = "dot"; t.appendChild(dot); }
  msgs.appendChild(t);
  scrollToEnd();
}
const hideTyping = (): void => { document.getElementById("_typing")?.remove(); };

function setBadge(remaining: number | null | undefined, quota: number | null | undefined): void {
  if (remaining == null || quota == null) { badge.textContent = "--"; badge.className = "badge"; return; }
  badge.textContent = `今日剩余 ${remaining} / ${quota}`;
  const ratio = remaining / Math.max(1, quota);
  badge.className = "badge " + (ratio >= 0.3 ? "good" : ratio >= 0.1 ? "warn" : "bad");
}

function relativeTime(ts: number): string {
  const diff = Date.now() - ts;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 172_800_000) return "昨天";
  const d = new Date(ts);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function renderSessionList(): void {
  sessionListEl.replaceChildren();
  const sorted = Object.values(store.sessions).sort((a, b) => b.lastActiveAt - a.lastActiveAt);
  for (const sess of sorted) {
    const li = document.createElement("li");
    li.className = "session-item";
    li.setAttribute("role", "listitem");
    li.dataset.sessionId = sess.id;
    if (sess.id === store.activeId) li.setAttribute("aria-current", "page");

    const pick = document.createElement("button");
    pick.className = "session-pick"; pick.type = "button";
    const title = document.createElement("span");
    title.className = "session-title"; title.textContent = sess.title || "新会话";
    const time = document.createElement("span");
    time.className = "session-time"; time.textContent = relativeTime(sess.lastActiveAt);
    pick.append(title, time);
    pick.addEventListener("click", () => switchSession(sess.id));

    const del = document.createElement("button");
    del.className = "session-del"; del.type = "button";
    del.setAttribute("aria-label", "删除该会话"); del.title = "删除该会话";
    del.innerHTML = TRASH_SVG;
    del.addEventListener("click", (e) => { e.stopPropagation(); deleteSession(sess.id); });

    li.append(pick, del);
    sessionListEl.appendChild(li);
  }
}

function switchSession(id: string): void {
  if (!store.sessions[id]) return;
  if (id !== store.activeId) {
    store.activeId = id;
    saveStore();
    replayActive();
    renderSessionList();
  }
  closeMobileSidebar();
}

function deleteSession(id: string): void {
  if (!store.sessions[id]) return;
  if (!confirm("删除该会话？")) return;
  delete store.sessions[id];
  if (id === store.activeId) {
    const remaining = Object.values(store.sessions).sort((a, b) => b.lastActiveAt - a.lastActiveAt);
    if (remaining.length) store.activeId = remaining[0]!.id;
    else { const fresh = blankSession(); store.sessions[fresh.id] = fresh; store.activeId = fresh.id; }
    replayActive();
  }
  saveStore();
  renderSessionList();
}

function newSession(): void {
  const fresh = blankSession();
  store.sessions[fresh.id] = fresh;
  store.activeId = fresh.id;
  saveStore(); replayActive(); renderSessionList(); closeMobileSidebar(); inputEl.focus();
}

function openMobileSidebar(): void {
  sidebarEl.classList.add("open");
  sidebarBackdrop.hidden = false;
  sidebarToggleBtn.setAttribute("aria-expanded", "true");
  sessionListEl.querySelector<HTMLButtonElement>("button")?.focus();
}
function closeMobileSidebar(): void {
  sidebarEl.classList.remove("open");
  sidebarBackdrop.hidden = true;
  sidebarToggleBtn.setAttribute("aria-expanded", "false");
}

async function loadChatSite(): Promise<void> {
  try {
    const resp = await fetch(SITE_URL, { credentials: "same-origin" });
    if (!resp.ok) return;
    const data = (await resp.json()) as SiteConfig;
    const name = (data.site_name || "").trim() || "WebChat Gateway";
    document.title = `${name} · Chat`;
    $("brandName").textContent = name;
    const family = data.theme_family === "classic" ? "classic" : "notebook";
    if (localStorage.getItem(LS_FAMILY) === family) return;
    try { localStorage.setItem(LS_FAMILY, family); } catch {}
    const cur = document.documentElement.getAttribute("data-theme");
    const isDark = cur === "midnight" || cur === "classic-dark";
    const resolved = resolveTheme(family, isDark ? "dark" : "light");
    if (resolved !== cur) document.documentElement.setAttribute("data-theme", resolved);
  } catch {}
}

async function probeQuota(): Promise<void> {
  try {
    const resp = await fetch(ME_URL, { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" });
    if (resp.status === 401) { localStorage.removeItem(LS_TOKEN); location.replace("/login"); return; }
    if (!resp.ok) return;
    const data = await resp.json();
    if (typeof data.remaining === "number" && typeof data.daily_quota === "number") setBadge(data.remaining, data.daily_quota);
  } catch {}
}

async function send(): Promise<void> {
  const message = inputEl.value.trim();
  if (!message) return;
  sendBtn.disabled = true;
  addMessage("user", message);
  inputEl.value = "";
  showTyping();

  try {
    const resp = await fetch(CHAT_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ session_id: currentSession().id, username, message }),
    });
    let payload: Record<string, unknown> = {};
    try { payload = await resp.json(); } catch {}

    if (resp.ok) {
      setBadge(payload.remaining as number, payload.daily_quota as number);
      addMessage("bot", (payload.reply as string) || "(空回复)");
      return;
    }

    const err = (payload.error as string) || `http_${resp.status}`;
    const s = resp.status;
    if (s === 401) {
      addMessage("error", "Token 无效或已撤销，请重新登录。");
      setTimeout(() => { localStorage.removeItem(LS_TOKEN); location.replace("/login"); }, 1500);
    } else if (s === 429 && err === "quota_exceeded") {
      setBadge(0, payload.daily_quota as number);
      addMessage("notice", "今日额度已用完，明日 0 点重置。");
    } else if (s === 429 && err === "concurrent_request") addMessage("notice", "上一条还在处理中，稍候。");
    else if (s === 429 && err === "ip_blocked") {
      const retry = resp.headers.get("Retry-After") || payload.retry_after || "?";
      addMessage("error", `请求过于频繁，已暂时封禁，${retry} 秒后重试。`);
    } else if (s === 400 && err === "message_too_long") addMessage("error", `消息过长 (上限 ${payload.max_length})。`);
    else if (s === 403 && err === "forbidden_origin") addMessage("error", "页面来源未在 allowed_origins 中。");
    else addMessage("error", `请求失败: ${err} ${payload.detail || ""}`);
  } catch (error) {
    addMessage("error", `网络错误: ${String(error)}`);
  } finally {
    hideTyping();
    sendBtn.disabled = false;
  }
}

$<HTMLButtonElement>("clearHistory").onclick = () => {
  const sess = currentSession();
  sess.history.length = 0; sess.title = "新会话";
  saveStore(); clearMsgList(); renderSessionList();
};
$<HTMLButtonElement>("newSessionBtn").onclick = newSession;
$<HTMLButtonElement>("logout").onclick = () => {
  if (!confirm("登出会清除本机保存的 token 与对话历史。继续？")) return;
  // Legacy keys cleared on logout — full reset semantics.
  for (const k of [LS_TOKEN, LS_USERNAME, LS_STORE, LS_LEGACY_SESSION]) localStorage.removeItem(k);
  sessionStorage.removeItem(SS_LEGACY_HISTORY);
  location.replace("/");
};

sidebarToggleBtn.addEventListener("click", () => {
  if (sidebarEl.classList.contains("open")) closeMobileSidebar();
  else openMobileSidebar();
});
sidebarBackdrop.addEventListener("click", closeMobileSidebar);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && sidebarEl.classList.contains("open")) {
    closeMobileSidebar();
    sidebarToggleBtn.focus();
  }
});

inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    send();
  }
});
sendBtn.onclick = send;

saveStore(); renderSessionList(); replayActive();
loadChatSite(); setupThemeToggle(); probeQuota();
