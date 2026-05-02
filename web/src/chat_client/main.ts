import { LS_TOKEN, LS_FAMILY, $, resolveTheme, setupThemeToggle } from "../shared/site";
import type { SiteConfig } from "../shared/site";

const LS_USERNAME = "wcg.username";
const LS_SESSION_ID = "wcg.sessionId";
const SS_HISTORY = "wcg.history";

const CHAT_URL = "/api/webchat/chat";
const ME_URL = "/api/webchat/me";
const SITE_URL = "/api/webchat/site";

// No token in localStorage → not logged in. Bounce to /login before any DOM
// access so quota probe / history hydration don't fire for a guest.
const token = (localStorage.getItem(LS_TOKEN) || "").trim();
if (!token) {
  location.replace("/login");
  throw new Error("redirecting to /login");
}

interface HistoryItem { role: Role; text: string; ts: number; }
type Role = "user" | "bot" | "error" | "notice";

const inputEl = $<HTMLTextAreaElement>("input");
const sendBtn = $<HTMLButtonElement>("send");
const msgs = $("messages");
const badge = $("quotaBadge");
const whoEl = $("who");

const username = (localStorage.getItem(LS_USERNAME) || "Friend").trim() || "Friend";
const strong = document.createElement("strong");
strong.textContent = username;
whoEl.append("你好，", strong);
whoEl.hidden = false;

let sessionId = localStorage.getItem(LS_SESSION_ID);
if (!sessionId) {
  sessionId = crypto.randomUUID?.() ?? String(Date.now());
  try { localStorage.setItem(LS_SESSION_ID, sessionId); } catch {}
}

// Tolerate corrupt sessionStorage (devtools edit, extension): start fresh
// rather than crash the page out of the composer.
let history: HistoryItem[] = [];
try {
  const raw: unknown = JSON.parse(sessionStorage.getItem(SS_HISTORY) || "[]");
  if (Array.isArray(raw)) {
    history = raw.filter((it): it is HistoryItem =>
      it && typeof it.text === "string" &&
      (it.role === "user" || it.role === "bot" || it.role === "error" || it.role === "notice")
    );
  }
} catch {}
for (const item of history) addMessage(item.role, item.text, true);
scrollToEnd();

function addMessage(role: Role, text: string, skipPersist = false): void {
  hideTyping();
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
  msgs.appendChild(div);
  if (!skipPersist && (role === "user" || role === "bot")) {
    history.push({ role, text, ts: Date.now() });
    try { sessionStorage.setItem(SS_HISTORY, JSON.stringify(history)); } catch {}
  }
  scrollToEnd();
}

function scrollToEnd(): void { msgs.scrollTop = msgs.scrollHeight; }

// Removes only .msg children; preserves the empty-state placeholder.
function clearMsgList(): void {
  msgs.querySelectorAll(".msg").forEach((m) => m.remove());
}

// Single typing indicator at a time.
function showTyping(): void {
  if (document.getElementById("_typing")) return;
  const t = document.createElement("div");
  t.id = "_typing";
  t.className = "msg bot typing";
  t.setAttribute("aria-label", "对方正在输入");
  for (let i = 0; i < 3; i++) {
    const dot = document.createElement("span");
    dot.className = "dot";
    t.appendChild(dot);
  }
  msgs.appendChild(t);
  scrollToEnd();
}
function hideTyping(): void {
  document.getElementById("_typing")?.remove();
}

function setBadge(remaining: number | null | undefined, quota: number | null | undefined): void {
  if (remaining == null || quota == null) {
    badge.textContent = "--";
    badge.className = "badge";
    return;
  }
  badge.textContent = `今日剩余 ${remaining} / ${quota}`;
  const ratio = remaining / Math.max(1, quota);
  badge.className = "badge " + (ratio >= 0.3 ? "good" : ratio >= 0.1 ? "warn" : "bad");
}

// Chat-only branding loader: shared loadSite() touches landing-only IDs
// (heroTitle/footerName/...) that don't exist on this page.
async function loadChatSite(): Promise<void> {
  try {
    const resp = await fetch(SITE_URL, { credentials: "same-origin" });
    if (!resp.ok) return;
    const data = (await resp.json()) as SiteConfig;
    const name = (data.site_name || "").trim() || "WebChat Gateway";
    document.title = `${name} · Chat`;
    $("brandName").textContent = name;
    const family = data.theme_family === "classic" ? "classic" : "notebook";
    const stored = localStorage.getItem(LS_FAMILY);
    if (stored !== family) {
      try { localStorage.setItem(LS_FAMILY, family); } catch {}
      const cur = document.documentElement.getAttribute("data-theme");
      const isDark = cur === "midnight" || cur === "classic-dark";
      const resolved = resolveTheme(family, isDark ? "dark" : "light");
      if (resolved !== cur) document.documentElement.setAttribute("data-theme", resolved);
    }
  } catch {}
}

// Quota probe on every page load so badge has a value before first send.
// 401 means the saved token was revoked — clear and bounce to /login.
async function probeQuota(): Promise<void> {
  try {
    const resp = await fetch(ME_URL, {
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
    });
    if (resp.status === 401) {
      localStorage.removeItem(LS_TOKEN);
      location.replace("/login");
      return;
    }
    if (!resp.ok) return;
    const data = await resp.json();
    if (typeof data.remaining === "number" && typeof data.daily_quota === "number") {
      setBadge(data.remaining, data.daily_quota);
    }
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
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ session_id: sessionId, username, message }),
    });
    let payload: Record<string, unknown> = {};
    try { payload = await resp.json(); } catch {}

    if (resp.ok) {
      setBadge(payload.remaining as number, payload.daily_quota as number);
      addMessage("bot", (payload.reply as string) || "(空回复)");
      return;
    }

    const err = (payload.error as string) || `http_${resp.status}`;
    if (resp.status === 401) {
      addMessage("error", "Token 无效或已撤销，请重新登录。");
      setTimeout(() => {
        localStorage.removeItem(LS_TOKEN);
        location.replace("/login");
      }, 1500);
    } else if (resp.status === 429 && err === "quota_exceeded") {
      setBadge(0, payload.daily_quota as number);
      addMessage("notice", "今日额度已用完，明日 0 点重置。");
    } else if (resp.status === 429 && err === "concurrent_request") {
      addMessage("notice", "上一条还在处理中，稍候。");
    } else if (resp.status === 429 && err === "ip_blocked") {
      const retry = resp.headers.get("Retry-After") || payload.retry_after || "?";
      addMessage("error", `请求过于频繁，已暂时封禁，${retry} 秒后重试。`);
    } else if (resp.status === 400 && err === "message_too_long") {
      addMessage("error", `消息过长 (上限 ${payload.max_length})。`);
    } else if (resp.status === 403 && err === "forbidden_origin") {
      addMessage("error", "页面来源未在 allowed_origins 中。");
    } else {
      addMessage("error", `请求失败: ${err} ${payload.detail || ""}`);
    }
  } catch (error) {
    addMessage("error", `网络错误: ${String(error)}`);
  } finally {
    hideTyping();
    sendBtn.disabled = false;
  }
}

$<HTMLButtonElement>("clearHistory").onclick = () => {
  history.length = 0;
  sessionStorage.removeItem(SS_HISTORY);
  clearMsgList();
};
$<HTMLButtonElement>("newSession").onclick = () => {
  sessionId = crypto.randomUUID?.() ?? String(Date.now());
  try { localStorage.setItem(LS_SESSION_ID, sessionId); } catch {}
  history.length = 0;
  sessionStorage.removeItem(SS_HISTORY);
  clearMsgList();
  addMessage("notice", `已开启新会话: ${sessionId}`);
};
$<HTMLButtonElement>("logout").onclick = () => {
  if (!confirm("登出会清除本机保存的 token 与对话历史。继续？")) return;
  localStorage.removeItem(LS_TOKEN);
  localStorage.removeItem(LS_USERNAME);
  localStorage.removeItem(LS_SESSION_ID);
  sessionStorage.removeItem(SS_HISTORY);
  location.replace("/");
};

// keyCode 229 = IME composition in progress on browsers that don't fire
// isComposing — guard both so Enter doesn't fire mid-compose on CJK input.
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    send();
  }
});
sendBtn.onclick = send;

loadChatSite();
setupThemeToggle();
probeQuota();
