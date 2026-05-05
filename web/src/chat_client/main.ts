import {
  LS_TOKEN,
  LS_FAMILY,
  $,
  modeFromTheme,
  paintBrowserChrome,
  reloadIOSChromeOnce,
  resolveTheme,
  setupThemeToggle,
} from "../shared/site";
import type { SiteConfig } from "../shared/site";
import { marked } from "marked";
import markedAlert from "marked-alert";
import markedFootnote from "marked-footnote";
import DOMPurify from "dompurify";

const LS_USERNAME = "wcg.username";
const LS_STORE = "wcg.chat.sessions";
const LS_LAST_PTS = "wcg.chat.lastPts";

const API = "/api/webchat";
const CHAT_URL = `${API}/chat`;
const CHAT_STREAM_URL = `${API}/chat/stream`;
const ME_URL = `${API}/me`;
const SITE_URL = `${API}/site`;
const TITLE_URL = `${API}/title`;
const CONV_URL = `${API}/conversations`;
const EVENTS_URL = `${API}/events`;

const TITLE_MAX = 25;
const RENAME_MAX = 40;
const TRASH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6"/></svg>';
const PENCIL_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';

const LONG_POLL_TIMEOUT_S = 25;
const SHORT_POLL_INTERVAL_MS = 30_000;
const SHORT_POLL_PROBE_INTERVAL_MS = 5 * 60_000;
const FAIL_WINDOW_MS = 5_000;
const FAIL_THRESHOLD = 3;
const BACKOFF_LADDER_MS = [1_000, 2_000, 5_000, 10_000, 30_000];
// Client-side fetch deadlines. Without these, a TCP connection that the
// peer silently dropped (captive portal, dead WiFi, GFW reset eaten by an
// intermediate hop) hangs the browser fetch until the OS-level TCP timeout
// (~2 min). The numbers below are conservative upper bounds that still let
// legitimate slow operations complete.
const FETCH_TIMEOUT_FAST_MS = 15_000;       // list/detail/PATCH/clear
const FETCH_TIMEOUT_LONG_POLL_MS = (LONG_POLL_TIMEOUT_S + 10) * 1000;
const FETCH_TIMEOUT_CHAT_MS = 90_000;       // covers llm_timeout (60s default) + slack
// User message: optimistic record happens BEFORE /chat fires, but the
// backend stamps event.ts only AFTER LLM responds (record_chat_pair runs
// at chat.py step 9). LLM is typically 5–30s, can be up to llm_timeout
// (default 60s, max 600s). The window must cover that gap for both the
// user event AND the assistant event in race-B (long-poll delivers events
// before /chat returns). Pair with PENDING_TTL_MS so entries live long
// enough to actually match.
const DEDUP_TS_WINDOW_S = 600;
const DEDUP_CONTENT_LEN = 200;

// Stream-failure circuit breaker: if the /chat/stream endpoint trips
// repeatedly within a short window (server has it disabled, an upstream
// proxy is buffering and timing out, etc.), stop trying it for the next
// few sends and go straight to /chat. Reset naturally as the window slides.
const STREAM_FAIL_WINDOW_MS = 60_000;
const STREAM_FAIL_THRESHOLD = 3;
const STREAM_SKIP_AFTER_TRIP = 5;
const STREAM_RENDER_DEBOUNCE_MS = 30;

const token = (localStorage.getItem(LS_TOKEN) || "").trim();
if (!token) { location.replace("/login"); throw new Error("redirecting to /login"); }
const bearer = (): Record<string, string> => ({ Authorization: `Bearer ${token}` });

// fetch + deadline. If `parentSignal` is also provided (e.g. the long-poll's
// visibility-tied AbortController), either side's abort will cancel. AbortError
// surfaces as a normal fetch rejection so callers' existing catch arms
// (registerFailure / inline error) handle the timeout the same as a network
// drop — that's the goal: turn silent zombie connections into observable
// failures the state machine can react to.
function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  parentSignal: AbortSignal | null = null,
): Promise<Response> {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(new DOMException("timeout", "AbortError")), timeoutMs);
  let parentAbort: (() => void) | null = null;
  if (parentSignal) {
    if (parentSignal.aborted) ctl.abort();
    else {
      parentAbort = (): void => ctl.abort();
      parentSignal.addEventListener("abort", parentAbort, { once: true });
    }
  }
  return fetch(url, { ...init, signal: ctl.signal }).finally(() => {
    clearTimeout(t);
    if (parentSignal && parentAbort) parentSignal.removeEventListener("abort", parentAbort);
  });
}

type Role = "user" | "bot" | "error" | "notice";
type ServerRole = "user" | "assistant";
interface HistoryItem { role: Role; text: string; ts: number; }
interface SessionMeta {
  id: string;
  title: string;
  titleManual?: boolean;
  pinned?: boolean;
  lastActiveAt: number;
  history: HistoryItem[];
}
interface ChatStore { activeId: string; sessions: Record<string, SessionMeta>; }

interface ServerSessionListItem {
  session_id: string;
  title: string;
  title_manual?: boolean;
  pinned?: boolean;
  updated_at: number;
  message_count: number;
  preview?: string;
}
interface ServerConversationsResponse {
  last_pts: number;
  conversations: ServerSessionListItem[];
}
interface ServerMessage { role: ServerRole; content: string; ts?: number; }
interface ServerConversationDetail {
  session_id: string;
  title: string;
  title_manual?: boolean;
  pinned?: boolean;
  updated_at: number;
  messages: ServerMessage[];
}
type EventType = "session_created" | "session_meta_updated" | "history_cleared" | "message_added";
interface ServerEvent {
  pts: number;
  ts: number;
  event_type: EventType;
  session_id: string;
  payload: Record<string, unknown>;
}
interface ServerEventsResponse {
  events?: ServerEvent[];
  last_pts: number;
  has_more?: boolean;
  tooFar?: boolean;
}

const inputEl = $<HTMLTextAreaElement>("input");
const sendBtn = $<HTMLButtonElement>("send");
const msgs = $("messages");
const badge = $("quotaBadge");
const syncStatusEl = $("syncStatus");
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
const nowSec = (): number => Math.floor(Date.now() / 1000);

function isHistoryItem(it: unknown): it is HistoryItem {
  if (!it || typeof it !== "object") return false;
  const o = it as Record<string, unknown>;
  return typeof o.text === "string" && typeof o.ts === "number" &&
    (o.role === "user" || o.role === "bot" || o.role === "error" || o.role === "notice");
}
function isSessionMeta(it: unknown): it is SessionMeta {
  if (!it || typeof it !== "object") return false;
  const o = it as Record<string, unknown>;
  if (o.titleManual !== undefined && typeof o.titleManual !== "boolean") return false;
  if (o.pinned !== undefined && typeof o.pinned !== "boolean") return false;
  return typeof o.id === "string" && typeof o.title === "string" &&
    typeof o.lastActiveAt === "number" && Array.isArray(o.history) && o.history.every(isHistoryItem);
}

// Cache-only loader. Server is authoritative; this just gets us a non-blank
// first paint while the cold refetch is in flight. Corrupt JSON → blank store.
function loadStore(): ChatStore {
  let parsed: unknown = null;
  try { parsed = JSON.parse(localStorage.getItem(LS_STORE) || "null"); } catch {}
  if (!parsed || typeof parsed !== "object") {
    const fresh = blankSession();
    return { activeId: fresh.id, sessions: { [fresh.id]: fresh } };
  }
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

function loadLastPts(): number {
  const raw = localStorage.getItem(LS_LAST_PTS);
  const n = raw == null ? NaN : Number(raw);
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

function deriveTitle(history: HistoryItem[]): string {
  const first = history.find((h) => h.role === "user");
  if (!first) return "新会话";
  const t = first.text.slice(0, TITLE_MAX);
  return first.text.length > TITLE_MAX ? t + "…" : t;
}

const store: ChatStore = loadStore();
const saveStore = (): void => { try { localStorage.setItem(LS_STORE, JSON.stringify(store)); } catch {} };
const saveLastPts = (pts: number): void => { try { localStorage.setItem(LS_LAST_PTS, String(pts)); } catch {} };
const currentSession = (): SessionMeta => store.sessions[store.activeId]!;
const serverRoleToLocal = (r: ServerRole): Role => r === "assistant" ? "bot" : "user";

function replayActive(): void {
  clearMsgList();
  for (const item of currentSession().history) addMessageBubble(item.role, item.text);
  scrollToEnd();
}

// Render-only: never mutates store, never persists. Used by replay + by
// applyEvents (which mutates store separately so that user/error/notice
// flows can opt out).
// Markdown rendering for assistant replies. marked → DOMPurify → DOM.
// `breaks: true` so single newlines from the LLM render as <br> (most LLMs
// do not double-newline paragraphs aggressively, especially in Chinese
// output). User echo, error, and notice bubbles stay plain-text via
// `textContent` because their content is either user-typed (don't trust)
// or our own status copy (no markdown features needed).
marked.setOptions({ gfm: true, breaks: true });

// Extension stack — keep additions narrow and grounded. Each one closes a
// specific "leaks raw symbols" gap from the LLM's typical output:
//   * markedAlert     → GitHub-style `> [!NOTE]` blockquotes
//   * markedFootnote  → `[^1]` references + `[^1]: definition` blocks
//   * highlight ext   → `==text==` mark spans (custom; ~10 lines)
marked.use(markedAlert());
marked.use(markedFootnote());
marked.use({
  extensions: [
    {
      name: "mark",
      level: "inline",
      start(src: string) { return src.indexOf("=="); },
      tokenizer(src: string) {
        const m = /^==([^=\n]+?)==/.exec(src);
        if (m) return { type: "mark", raw: m[0], text: m[1]! };
        return undefined;
      },
      renderer(token) { return `<mark>${(token as unknown as { text: string }).text}</mark>`; },
    },
  ],
});

// Open every link in a new tab with safe rel. DOMPurify lets attributes
// like target/rel through but doesn't add them — that's a separate hook.
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

function renderMarkdown(text: string): string {
  // marked.parse runs sync when given a string with no async extensions,
  // but its declared return type is `string | Promise<string>`. Force
  // sync via the parse-as-string options.
  const html = marked.parse(text, { async: false }) as string;
  return DOMPurify.sanitize(html, {
    FORBID_TAGS: ["style", "script", "iframe", "object", "embed", "form"],
    FORBID_ATTR: ["formaction"],
  });
}

// Append a chat bubble. `text` is the raw content as stored in history;
// for the bot role we render markdown (sanitized), everything else stays
// plain text (textContent) — user input must never be HTML-rendered
// because it's untrusted input echoed back to the same DOM.
function addMessageBubble(role: Role, text: string): void {
  hideTyping();
  const div = document.createElement("div");
  div.className = "msg " + role;
  if (role === "bot") {
    div.classList.add("md");
    div.innerHTML = renderMarkdown(text);
  } else {
    div.textContent = text;
  }
  msgs.appendChild(div);
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

type SyncStatusState = "live" | "polling" | "offline";
function setSyncStatus(state: SyncStatusState): void {
  syncStatusEl.dataset.state = state;
  syncStatusEl.textContent = state === "live" ? "实时" : state === "polling" ? "轮询" : "离线";
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

let editingSessionId: string | null = null;

// ---------- Sync state ----------

interface SyncState {
  transport: SyncStatusState;        // "live" = long_poll, "polling" = short_poll, "offline" = giving up but still trying
  lastPts: number;
  recentFails: number[];             // ms timestamps of recent failures, capped to FAIL_THRESHOLD
  longPollAbort: AbortController | null;
  shortPollTimer: ReturnType<typeof setTimeout> | null;
  probeTimer: ReturnType<typeof setTimeout> | null;
  retryTimer: ReturnType<typeof setTimeout> | null;
  consecutiveBackoffSteps: number;   // index into BACKOFF_LADDER_MS while in offline-ish state
  stopped: boolean;                  // logout sets this; loops bail out
  loopRunning: boolean;              // re-entrancy guard for runLongPoll/runShortPoll
  streamAbort: AbortController | null;  // active /chat/stream fetch+reader; null when not streaming
  streamFailedAt: number[];          // ms timestamps of recent /chat/stream failures
  streamSkipRemaining: number;       // sends to bypass /chat/stream after a trip
}

const sync: SyncState = {
  transport: "live",
  lastPts: loadLastPts(),
  recentFails: [],
  longPollAbort: null,
  shortPollTimer: null,
  probeTimer: null,
  retryTimer: null,
  consecutiveBackoffSteps: 0,
  stopped: false,
  loopRunning: false,
  streamAbort: null,
  streamFailedAt: [],
  streamSkipRemaining: 0,
};
setSyncStatus("live");

// ---------- Optimistic-echo dedup buffer ----------
//
// Background: when the user clicks send we render a "user" bubble + push it to
// store.history immediately (optimistic echo). Server processes the chat,
// emits a `message_added` for the user message AND one for the assistant
// reply. The long-poll delivers both. The originating device must drop the
// user echo (to avoid duplicate bubbles) but render the assistant bubble.
//
// We also dedup the assistant message: once /chat returns, we render the
// assistant locally so the user isn't stuck on "typing..." if long-poll is
// momentarily wedged. The corresponding `message_added` event is then dropped
// the same way.
//
// Match key: `(session_id, role, content trimmed to first DEDUP_CONTENT_LEN
// chars)` plus event ts within ±DEDUP_TS_WINDOW_S of the locally recorded ts.
// Buffer entries are consumed-on-match (one event consumes one local pending
// entry) and time out after 60s so a long-poll outage can't leak entries
// forever.

interface PendingLocal {
  sessionId: string;
  role: ServerRole;
  contentKey: string;
  recordedAtSec: number;            // local clock when we rendered it; matches against event ts
  expiresAt: number;                 // ms
}
const PENDING_TTL_MS = 660_000;
const pendingLocals: PendingLocal[] = [];

function trimContent(s: string): string {
  return s.trim().slice(0, DEDUP_CONTENT_LEN);
}

function recordOptimistic(sessionId: string, role: ServerRole, content: string): void {
  pendingLocals.push({
    sessionId,
    role,
    contentKey: trimContent(content),
    recordedAtSec: nowSec(),
    expiresAt: Date.now() + PENDING_TTL_MS,
  });
}

function consumeIfDuplicate(sessionId: string, role: ServerRole, content: string, eventTs: number): boolean {
  const now = Date.now();
  // Drop expired entries first so we don't hold stale matches forever.
  for (let i = pendingLocals.length - 1; i >= 0; i--) {
    if (pendingLocals[i]!.expiresAt < now) pendingLocals.splice(i, 1);
  }
  const key = trimContent(content);
  for (let i = 0; i < pendingLocals.length; i++) {
    const p = pendingLocals[i]!;
    if (p.sessionId !== sessionId || p.role !== role || p.contentKey !== key) continue;
    if (Math.abs(eventTs - p.recordedAtSec) > DEDUP_TS_WINDOW_S) continue;
    pendingLocals.splice(i, 1);
    return true;
  }
  return false;
}

// ---------- Apply events ----------

function applyEvent(ev: ServerEvent): void {
  const sid = ev.session_id;
  const payload = ev.payload || {};
  switch (ev.event_type) {
    case "session_created": {
      if (!store.sessions[sid]) {
        const title = typeof payload["title"] === "string" ? payload["title"] : "";
        store.sessions[sid] = {
          id: sid,
          title: title || "新会话",
          lastActiveAt: ev.ts * 1000,
          history: [],
        };
      }
      break;
    }
    case "session_meta_updated": {
      // Handle delete first so a "tombstone" event for a session we don't
      // know about (e.g. we already optimistically removed it) doesn't
      // resurrect it just to delete it again.
      if (payload["deleted"] === true) {
        if (!store.sessions[sid]) break;
        const wasActive = sid === store.activeId;
        delete store.sessions[sid];
        if (wasActive) {
          const remaining = Object.values(store.sessions).sort((a, b) => b.lastActiveAt - a.lastActiveAt);
          if (remaining.length) store.activeId = remaining[0]!.id;
          else { const fresh = blankSession(); store.sessions[fresh.id] = fresh; store.activeId = fresh.id; }
          replayActive();
        }
        break;
      }
      let sess = store.sessions[sid];
      if (!sess) {
        sess = blankSession(sid);
        store.sessions[sid] = sess;
      }
      if (typeof payload["title"] === "string") sess.title = payload["title"] || "新会话";
      if (typeof payload["title_manual"] === "boolean") sess.titleManual = payload["title_manual"] as boolean;
      if (typeof payload["pinned"] === "boolean") sess.pinned = payload["pinned"] as boolean;
      break;
    }
    case "history_cleared": {
      const sess = store.sessions[sid];
      if (sess) {
        sess.history.length = 0;
        sess.title = "新会话";
        sess.titleManual = false;
        if (sid === store.activeId) clearMsgList();
      }
      break;
    }
    case "message_added": {
      const role = payload["role"];
      const content = payload["content"];
      if ((role !== "user" && role !== "assistant") || typeof content !== "string") break;
      // Dedup: if we already rendered this locally on this device, drop the event.
      if (consumeIfDuplicate(sid, role, content, ev.ts)) {
        // Still bump lastActiveAt so the sidebar order matches the server.
        const s = store.sessions[sid];
        if (s) s.lastActiveAt = Math.max(s.lastActiveAt, ev.ts * 1000);
        break;
      }
      let sess = store.sessions[sid];
      if (!sess) {
        sess = blankSession(sid);
        store.sessions[sid] = sess;
      }
      const localRole: Role = serverRoleToLocal(role as ServerRole);
      sess.history.push({ role: localRole, text: content, ts: ev.ts * 1000 });
      sess.lastActiveAt = ev.ts * 1000;
      if (role === "user" && (sess.title === "新会话" || !sess.title)) {
        sess.title = deriveTitle(sess.history);
      }
      if (sid === store.activeId) addMessageBubble(localRole, content);
      break;
    }
  }
}

function applyEvents(events: ServerEvent[]): void {
  if (!events.length) return;
  for (const ev of events) applyEvent(ev);
  saveStore();
  renderSessionList();
}

// ---------- Cold refetch ----------

async function fetchConversations(): Promise<ServerConversationsResponse | null> {
  const resp = await fetchWithTimeout(
    CONV_URL,
    { credentials: "same-origin", headers: bearer() },
    FETCH_TIMEOUT_FAST_MS,
  );
  if (resp.status === 401) { handle401(); return null; }
  if (!resp.ok) throw new Error(`http_${resp.status}`);
  return await resp.json() as ServerConversationsResponse;
}

async function fetchConversation(sessionId: string): Promise<ServerConversationDetail | null> {
  const resp = await fetchWithTimeout(
    `${CONV_URL}/${encodeURIComponent(sessionId)}`,
    { credentials: "same-origin", headers: bearer() },
    FETCH_TIMEOUT_FAST_MS,
  );
  if (resp.status === 401) { handle401(); return null; }
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`http_${resp.status}`);
  return await resp.json() as ServerConversationDetail;
}

// Replace local sidebar with the server's list. Sessions present locally but
// missing from the server are dropped UNLESS the server's list is empty (fresh
// deploy, no events yet) — in that case we keep the local cache so the user
// doesn't lose their pre-sync history.
function ingestConversationList(resp: ServerConversationsResponse): void {
  const incoming = resp.conversations;
  if (!incoming.length && Object.keys(store.sessions).length > 0) {
    sync.lastPts = resp.last_pts;
    saveLastPts(resp.last_pts);
    return;
  }
  const next: Record<string, SessionMeta> = {};
  for (const row of incoming) {
    const existing = store.sessions[row.session_id];
    next[row.session_id] = {
      id: row.session_id,
      title: row.title || (existing?.title || "新会话"),
      titleManual: row.title_manual ?? existing?.titleManual,
      pinned: row.pinned ?? existing?.pinned,
      lastActiveAt: row.updated_at * 1000,
      history: existing?.history ?? [],
    };
  }
  store.sessions = next;
  if (!store.sessions[store.activeId]) {
    const sorted = Object.values(store.sessions).sort((a, b) => b.lastActiveAt - a.lastActiveAt);
    if (sorted.length) store.activeId = sorted[0]!.id;
    else { const fresh = blankSession(); store.sessions[fresh.id] = fresh; store.activeId = fresh.id; }
  }
  sync.lastPts = resp.last_pts;
  saveLastPts(resp.last_pts);
}

function ingestConversationDetail(detail: ServerConversationDetail): void {
  let sess = store.sessions[detail.session_id];
  if (!sess) {
    sess = blankSession(detail.session_id);
    store.sessions[detail.session_id] = sess;
  }
  sess.title = detail.title || sess.title || "新会话";
  if (typeof detail.title_manual === "boolean") sess.titleManual = detail.title_manual;
  if (typeof detail.pinned === "boolean") sess.pinned = detail.pinned;
  sess.lastActiveAt = detail.updated_at * 1000;
  sess.history = detail.messages.map((m) => ({
    role: serverRoleToLocal(m.role),
    text: m.content,
    ts: (m.ts ?? detail.updated_at) * 1000,
  }));
}

async function coldRefetch(): Promise<void> {
  const list = await fetchConversations();
  if (!list) return;
  ingestConversationList(list);
  const activeId = store.activeId;
  if (activeId && store.sessions[activeId]) {
    try {
      const detail = await fetchConversation(activeId);
      if (detail) ingestConversationDetail(detail);
    } catch {
      // Swallow: the list refetch already gave us session entries; details
      // missing means we keep stale local history until next event arrives.
    }
  }
  saveStore();
  renderSessionList();
  if (activeId && store.sessions[activeId]) replayActive();
}

// ---------- Sync loops ----------

function clearTimer(slot: "shortPollTimer" | "probeTimer" | "retryTimer"): void {
  const h = sync[slot];
  if (h !== null) {
    clearTimeout(h);
    sync[slot] = null;
  }
}

function abortInflightLongPoll(): void {
  if (sync.longPollAbort) {
    sync.longPollAbort.abort();
    sync.longPollAbort = null;
  }
}

function registerFailure(): void {
  const now = Date.now();
  sync.recentFails.push(now);
  if (sync.recentFails.length > FAIL_THRESHOLD) sync.recentFails.shift();
}

function recentFailsInWindow(): number {
  const cutoff = Date.now() - FAIL_WINDOW_MS;
  return sync.recentFails.filter((t) => t >= cutoff).length;
}

function currentBackoffMs(): number {
  const idx = Math.min(sync.consecutiveBackoffSteps, BACKOFF_LADDER_MS.length - 1);
  return BACKOFF_LADDER_MS[idx]!;
}

function sleep(ms: number, signal: AbortSignal | null): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("aborted", "AbortError"));
    const handle = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(handle);
      reject(new DOMException("aborted", "AbortError"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function handle401(): void {
  sync.stopped = true;
  abortInflightLongPoll();
  if (sync.streamAbort) { sync.streamAbort.abort(); sync.streamAbort = null; }
  clearTimer("shortPollTimer");
  clearTimer("probeTimer");
  clearTimer("retryTimer");
  localStorage.removeItem(LS_TOKEN);
  // pts is per-token; a stale value from the old token would make the
  // next login's long-poll trail the new token's max_pts indefinitely.
  localStorage.removeItem(LS_LAST_PTS);
  location.replace("/login");
}

function handleEventsResponse(data: ServerEventsResponse): { needsImmediateRefetch: boolean } {
  if (data.tooFar) {
    return { needsImmediateRefetch: true };
  }
  applyEvents(data.events ?? []);
  if (typeof data.last_pts === "number" && data.last_pts >= sync.lastPts) {
    sync.lastPts = data.last_pts;
    saveLastPts(data.last_pts);
  }
  return { needsImmediateRefetch: false };
}

async function runLongPoll(): Promise<void> {
  if (sync.loopRunning) return;
  sync.loopRunning = true;
  setSyncStatus("live");
  try {
    while (!sync.stopped && sync.transport === "live" && !document.hidden) {
      const ac = new AbortController();
      sync.longPollAbort = ac;
      try {
        const resp = await fetchWithTimeout(
          `${EVENTS_URL}?since=${sync.lastPts}&timeout=${LONG_POLL_TIMEOUT_S}`,
          { credentials: "same-origin", headers: bearer() },
          FETCH_TIMEOUT_LONG_POLL_MS,
          ac.signal,
        );
        if (resp.status === 401) { handle401(); return; }
        if (!resp.ok) throw new Error(`http_${resp.status}`);
        const data = await resp.json() as ServerEventsResponse;
        sync.consecutiveBackoffSteps = 0;
        sync.recentFails = [];
        const { needsImmediateRefetch } = handleEventsResponse(data);
        if (needsImmediateRefetch) {
          await coldRefetch();
          continue;
        }
        // has_more=true → re-request immediately so we drain the backlog.
        if (data.has_more) continue;
      } catch (e) {
        sync.longPollAbort = null;
        if (sync.stopped) return;
        if ((e as { name?: string }).name === "AbortError") return;
        registerFailure();
        if (recentFailsInWindow() >= FAIL_THRESHOLD) {
          // Too many failures while in long-poll → degrade to short-poll.
          sync.transport = "polling";
          setSyncStatus("polling");
          startShortPoll();
          startProbeTimer();
          return;
        }
        // Ride out a transient failure with a short backoff, then resume.
        sync.consecutiveBackoffSteps = Math.min(sync.consecutiveBackoffSteps + 1, BACKOFF_LADDER_MS.length - 1);
        try { await sleep(currentBackoffMs(), null); } catch {}
      } finally {
        if (sync.longPollAbort === ac) sync.longPollAbort = null;
      }
    }
  } finally {
    sync.loopRunning = false;
  }
}

function startShortPoll(): void {
  clearTimer("shortPollTimer");
  // Run one probe right now, then on a timer until transport switches back.
  void shortPollOnce();
}

async function shortPollOnce(): Promise<void> {
  if (sync.stopped || sync.transport !== "polling") return;
  try {
    const resp = await fetchWithTimeout(
      `${EVENTS_URL}?since=${sync.lastPts}&timeout=0`,
      { credentials: "same-origin", headers: bearer() },
      FETCH_TIMEOUT_FAST_MS,
    );
    if (resp.status === 401) { handle401(); return; }
    if (!resp.ok) throw new Error(`http_${resp.status}`);
    const data = await resp.json() as ServerEventsResponse;
    sync.recentFails = [];
    sync.consecutiveBackoffSteps = 0;
    const { needsImmediateRefetch } = handleEventsResponse(data);
    if (needsImmediateRefetch) {
      await coldRefetch();
    }
    setSyncStatus("polling");
    sync.shortPollTimer = setTimeout(() => { void shortPollOnce(); }, SHORT_POLL_INTERVAL_MS);
  } catch (e) {
    if ((e as { name?: string }).name === "AbortError") return;
    registerFailure();
    sync.consecutiveBackoffSteps = Math.min(sync.consecutiveBackoffSteps + 1, BACKOFF_LADDER_MS.length - 1);
    if (recentFailsInWindow() >= FAIL_THRESHOLD) setSyncStatus("offline");
    sync.shortPollTimer = setTimeout(() => { void shortPollOnce(); }, currentBackoffMs());
  }
}

function startProbeTimer(): void {
  clearTimer("probeTimer");
  sync.probeTimer = setTimeout(() => { void probeLongPoll(); }, SHORT_POLL_PROBE_INTERVAL_MS);
}

// While in short_poll mode, every 5 minutes try a single long-poll request
// against the events endpoint. If it succeeds (no error, any 2xx including
// timeout-empty), promote back to long_poll. If it fails, stay short_poll
// and rearm the probe timer for another 5 min.
async function probeLongPoll(): Promise<void> {
  if (sync.stopped || sync.transport !== "polling" || document.hidden) {
    startProbeTimer();
    return;
  }
  try {
    const resp = await fetchWithTimeout(
      `${EVENTS_URL}?since=${sync.lastPts}&timeout=${LONG_POLL_TIMEOUT_S}`,
      { credentials: "same-origin", headers: bearer() },
      FETCH_TIMEOUT_LONG_POLL_MS,
    );
    if (resp.status === 401) { handle401(); return; }
    if (!resp.ok) throw new Error(`http_${resp.status}`);
    const data = await resp.json() as ServerEventsResponse;
    handleEventsResponse(data);
    // Promote: cancel short-poll loop and resume long-polling.
    sync.transport = "live";
    sync.recentFails = [];
    sync.consecutiveBackoffSteps = 0;
    clearTimer("shortPollTimer");
    clearTimer("probeTimer");
    setSyncStatus("live");
    void runLongPoll();
  } catch {
    if (sync.transport === "polling") startProbeTimer();
  }
}

function onVisibilityChange(): void {
  if (document.hidden) {
    abortInflightLongPoll();
  } else if (!sync.stopped) {
    if (sync.transport === "live") void runLongPoll();
    else if (sync.transport === "polling") void shortPollOnce();
  }
}

// ---------- Sidebar render ----------

function renderSessionList(): void {
  sessionListEl.replaceChildren();
  const sorted = Object.values(store.sessions).sort((a, b) => {
    const ap = a.pinned ? 1 : 0;
    const bp = b.pinned ? 1 : 0;
    if (ap !== bp) return bp - ap;
    return b.lastActiveAt - a.lastActiveAt;
  });
  for (const sess of sorted) {
    const li = document.createElement("li");
    li.className = "session-item";
    li.setAttribute("role", "listitem");
    li.dataset.sessionId = sess.id;
    if (sess.id === store.activeId) li.setAttribute("aria-current", "page");
    const isEditing = editingSessionId === sess.id;
    if (isEditing) li.dataset.editing = "true";

    if (isEditing) {
      const input = document.createElement("input");
      input.className = "session-title-input"; input.type = "text";
      input.maxLength = RENAME_MAX; input.value = sess.title || "";
      input.setAttribute("aria-label", "重命名会话");
      let settled = false;
      const commit = (): void => { if (settled) return; settled = true; void commitRename(sess.id, input.value); };
      const cancel = (): void => { if (settled) return; settled = true; cancelRename(); };
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); commit(); }
        else if (e.key === "Escape") { e.preventDefault(); cancel(); }
      });
      input.addEventListener("blur", commit);
      li.append(input); sessionListEl.appendChild(li);
      queueMicrotask(() => { input.focus(); input.select(); });
      continue;
    }

    const pick = document.createElement("button");
    pick.className = "session-pick"; pick.type = "button";
    const title = document.createElement("span");
    title.className = "session-title"; title.textContent = sess.title || "新会话";
    const time = document.createElement("span");
    time.className = "session-time"; time.textContent = relativeTime(sess.lastActiveAt);
    pick.append(title, time);
    pick.addEventListener("click", () => switchSession(sess.id));

    const edit = document.createElement("button");
    edit.className = "session-edit"; edit.type = "button";
    edit.setAttribute("aria-label", "重命名"); edit.title = "重命名";
    edit.innerHTML = PENCIL_SVG;
    edit.addEventListener("click", (e) => { e.stopPropagation(); beginRename(sess.id); });

    const del = document.createElement("button");
    del.className = "session-del"; del.type = "button";
    del.setAttribute("aria-label", "删除该会话"); del.title = "删除该会话";
    del.innerHTML = TRASH_SVG;
    del.addEventListener("click", (e) => { e.stopPropagation(); void deleteSession(sess.id); });

    li.append(pick, edit, del);
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
    void fetchConversation(id).then((detail) => {
      if (!detail) return;
      ingestConversationDetail(detail);
      saveStore();
      if (id === store.activeId) replayActive();
      renderSessionList();
    }).catch(() => {});
  }
  closeMobileSidebar();
}

// ---------- Mutations (API-first) ----------

async function patchSession(sessionId: string, body: Record<string, unknown>): Promise<void> {
  const resp = await fetchWithTimeout(
    `${CONV_URL}/${encodeURIComponent(sessionId)}`,
    {
      method: "PATCH",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...bearer() },
      body: JSON.stringify(body),
    },
    FETCH_TIMEOUT_FAST_MS,
  );
  if (resp.status === 401) { handle401(); throw new Error("unauthorized"); }
  if (!resp.ok) {
    let err = `http_${resp.status}`;
    try { const j = await resp.json() as { error?: string }; if (j.error) err = j.error; } catch {}
    throw new Error(err);
  }
}

async function postClear(sessionId: string): Promise<void> {
  const resp = await fetchWithTimeout(
    `${CONV_URL}/${encodeURIComponent(sessionId)}/clear`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: bearer(),
    },
    FETCH_TIMEOUT_FAST_MS,
  );
  if (resp.status === 401) { handle401(); throw new Error("unauthorized"); }
  if (!resp.ok) {
    let err = `http_${resp.status}`;
    try { const j = await resp.json() as { error?: string }; if (j.error) err = j.error; } catch {}
    throw new Error(err);
  }
}

async function commitRename(sid: string, raw: string): Promise<void> {
  const sess = store.sessions[sid];
  if (!sess) return;
  const newTitle = raw.trim() || "新会话";
  const prev = { title: sess.title, titleManual: sess.titleManual };
  // Optimistic local update so the UI feels instant.
  sess.title = newTitle;
  sess.titleManual = true;
  editingSessionId = null;
  saveStore();
  renderSessionList();
  try {
    await patchSession(sid, { title: newTitle, title_manual: true });
  } catch (e) {
    // Roll back and re-open the editor with the user's edit intact.
    sess.title = prev.title;
    sess.titleManual = prev.titleManual;
    editingSessionId = sid;
    saveStore();
    renderSessionList();
    addMessageBubble("error", `重命名失败: ${(e as Error).message}`);
  }
}

function cancelRename(): void { editingSessionId = null; renderSessionList(); }

function beginRename(sid: string): void {
  if (!store.sessions[sid]) return;
  editingSessionId = sid; renderSessionList();
}

async function deleteSession(id: string): Promise<void> {
  if (!store.sessions[id]) return;
  if (!confirm("删除该会话？")) return;
  const snapshot = store.sessions[id];
  const wasActive = id === store.activeId;
  delete store.sessions[id];
  if (wasActive) {
    const remaining = Object.values(store.sessions).sort((a, b) => b.lastActiveAt - a.lastActiveAt);
    if (remaining.length) store.activeId = remaining[0]!.id;
    else { const fresh = blankSession(); store.sessions[fresh.id] = fresh; store.activeId = fresh.id; }
    replayActive();
  }
  saveStore();
  renderSessionList();
  try {
    await patchSession(id, { deleted: true });
  } catch (e) {
    // Restore the local entry on failure so the user doesn't silently lose it.
    store.sessions[id] = snapshot;
    if (wasActive) {
      store.activeId = id;
      replayActive();
    }
    saveStore();
    renderSessionList();
    addMessageBubble("error", `删除失败: ${(e as Error).message}`);
  }
}

async function clearActiveHistory(): Promise<void> {
  const sess = currentSession();
  const id = sess.id;
  const prevHistory = sess.history.slice();
  const prevTitle = sess.title;
  const prevManual = sess.titleManual;
  sess.history.length = 0;
  sess.title = "新会话";
  sess.titleManual = false;
  saveStore();
  clearMsgList();
  renderSessionList();
  try {
    await postClear(id);
  } catch (e) {
    sess.history = prevHistory;
    sess.title = prevTitle;
    sess.titleManual = prevManual;
    saveStore();
    replayActive();
    renderSessionList();
    addMessageBubble("error", `清空失败: ${(e as Error).message}`);
  }
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
    const resp = await fetchWithTimeout(
      SITE_URL,
      { credentials: "same-origin" },
      FETCH_TIMEOUT_FAST_MS,
    );
    if (!resp.ok) return;
    const data = (await resp.json()) as SiteConfig;
    const name = (data.site_name || "").trim() || "WebChat Gateway";
    document.title = `${name} · Chat`;
    $("brandName").textContent = name;
    const family = data.theme_family === "classic" ? "classic" : "notebook";
    if (localStorage.getItem(LS_FAMILY) === family) return;
    try { localStorage.setItem(LS_FAMILY, family); } catch {}
    const cur = document.documentElement.getAttribute("data-theme");
    const mode = modeFromTheme(cur);
    const resolved = resolveTheme(family, mode);
    if (resolved !== cur) {
      document.documentElement.setAttribute("data-theme", resolved);
      paintBrowserChrome(resolved);
    }
    reloadIOSChromeOnce("wcg.theme.family.reload", `${family}:${mode}`);
  } catch {}
}

async function probeQuota(): Promise<void> {
  try {
    const resp = await fetchWithTimeout(
      ME_URL,
      { headers: bearer(), credentials: "same-origin" },
      FETCH_TIMEOUT_FAST_MS,
    );
    if (resp.status === 401) { handle401(); return; }
    if (!resp.ok) return;
    const data = await resp.json();
    if (typeof data.remaining === "number" && typeof data.daily_quota === "number") setBadge(data.remaining, data.daily_quota);
  } catch {}
}

async function requestAutoTitle(sid: string, firstUserMsg: string): Promise<void> {
  const resp = await fetchWithTimeout(
    TITLE_URL,
    {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...bearer() },
      body: JSON.stringify({ session_id: sid, conversation: [{ role: "user", text: firstUserMsg }] }),
    },
    FETCH_TIMEOUT_CHAT_MS,
  );
  if (!resp.ok) return;
  const data = await resp.json() as { title?: string; remaining?: number; daily_quota?: number };
  const newTitle = (data.title || "").trim();
  if (typeof data.remaining === "number" && typeof data.daily_quota === "number") setBadge(data.remaining, data.daily_quota);
  if (!newTitle) return;
  const sess = store.sessions[sid];
  if (!sess || sess.titleManual) return;
  sess.title = newTitle;
  saveStore();
  renderSessionList();
  // Persist to server so the auto-title syncs to other devices. title_manual:
  // false so a later manual rename still wins.
  try { await patchSession(sid, { title: newTitle, title_manual: false }); } catch {}
}

// ---------- Streaming chat ----------

// Typed error shape thrown by streamChat for the caller's status-aware
// fallback. `status` mirrors HTTP status (or 0 for in-stream errors that
// arrived as `data: {error: "..."}`); `code` is the application-level
// error string when one was returned, or "" otherwise.
class StreamChatError extends Error {
  status: number;
  code: string;
  payload: Record<string, unknown>;
  constructor(message: string, status: number, code: string, payload: Record<string, unknown> = {}) {
    super(message);
    this.name = "StreamChatError";
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

interface StreamDoneInfo { remaining: number; daily_quota: number; }

// Streaming requires Response.body (a ReadableStream). All evergreen
// browsers ship it, but Safari versions before 15.1 / older mobile WebViews
// may not. If absent, callers treat the stream attempt as unsupported and
// fall back to non-stream /chat without burning a circuit-breaker slot.
function streamingSupported(): boolean {
  return typeof TextDecoder !== "undefined" && typeof ReadableStream !== "undefined";
}

// Hand-rolled SSE parser per WHATWG/HTML5 spec rules we actually need:
//   - Frames are separated by "\n\n" (we accept both LF and CRLF; the
//     decoded text is normalized at frame boundaries).
//   - Lines beginning with ":" are comments and ignored (used here for
//     `: ready` and `: keepalive`).
//   - Multiple `data:` lines per frame concatenate with "\n" and are then
//     parsed as a single JSON object per the backend contract.
//   - Anything else (event:, id:, retry:) is not used by this contract,
//     so we silently skip those lines.
async function streamChat(
  sid: string,
  message: string,
  onChunk: (text: string) => void,
  signal: AbortSignal,
): Promise<StreamDoneInfo> {
  const resp = await fetchWithTimeout(
    CHAT_STREAM_URL,
    {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...bearer() },
      body: JSON.stringify({ session_id: sid, username, message }),
    },
    FETCH_TIMEOUT_CHAT_MS,
    signal,
  );

  if (!resp.ok) {
    let payload: Record<string, unknown> = {};
    try { payload = await resp.json() as Record<string, unknown>; } catch {}
    const code = typeof payload.error === "string" ? payload.error : `http_${resp.status}`;
    throw new StreamChatError(code, resp.status, code, payload);
  }

  const ct = resp.headers.get("Content-Type") || "";
  if (!ct.toLowerCase().includes("text/event-stream") || !resp.body) {
    // Server returned 200 but not SSE — treat as unsupported endpoint so
    // the caller falls back without keeping the bubble in streaming state.
    throw new StreamChatError("not_event_stream", resp.status, "not_event_stream");
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: false });
  let buffer = "";
  let doneInfo: StreamDoneInfo | null = null;

  const handleFrame = (frame: string): void => {
    if (!frame) return;
    const jsonLines: string[] = [];
    for (const rawLine of frame.split("\n")) {
      const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("data:")) {
        // Per spec, exactly one space after the colon is stripped if present.
        const v = line.slice(5);
        jsonLines.push(v.startsWith(" ") ? v.slice(1) : v);
      }
      // event:/id:/retry: → unused under this contract
    }
    if (!jsonLines.length) return;
    let obj: Record<string, unknown>;
    try {
      obj = JSON.parse(jsonLines.join("\n")) as Record<string, unknown>;
    } catch {
      // Malformed JSON in a data frame is unusual — skip it rather than
      // killing the whole stream; the keepalive/ready frames are comments
      // and never reach this path.
      return;
    }
    if (typeof obj.chunk === "string") {
      onChunk(obj.chunk);
      return;
    }
    if (obj.done === true) {
      const remaining = typeof obj.remaining === "number" ? obj.remaining : 0;
      const daily = typeof obj.daily_quota === "number" ? obj.daily_quota : 0;
      doneInfo = { remaining, daily_quota: daily };
      return;
    }
    if (typeof obj.error === "string") {
      throw new StreamChatError(obj.error, 0, obj.error, obj);
    }
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        handleFrame(frame);
        if (doneInfo) return doneInfo;
      }
    }
    // Stream closed without sending a `done` frame. Flush the decoder and
    // try one last frame in case the server forgot the trailing blank
    // line, then surface a truncation error if still no done.
    buffer += decoder.decode();
    if (buffer.trim()) {
      handleFrame(buffer);
      if (doneInfo) return doneInfo;
    }
    throw new StreamChatError("stream_truncated", 0, "stream_truncated");
  } finally {
    // Cancel-on-exit guarantees the underlying fetch is released even when
    // we throw mid-loop or break early; calling cancel() on a finished
    // reader is a no-op, so this is always safe.
    try { await reader.cancel(); } catch {}
  }
}

function setSendMode(mode: "send" | "stop"): void {
  sendBtn.dataset.mode = mode;
  sendBtn.setAttribute("aria-label", mode === "stop" ? "停止" : "发送");
}

function isStreamCircuitOpen(): boolean {
  if (sync.streamSkipRemaining > 0) {
    sync.streamSkipRemaining -= 1;
    return true;
  }
  return false;
}

function recordStreamFailure(): void {
  const now = Date.now();
  const cutoff = now - STREAM_FAIL_WINDOW_MS;
  sync.streamFailedAt = sync.streamFailedAt.filter((t) => t >= cutoff);
  sync.streamFailedAt.push(now);
  if (sync.streamFailedAt.length >= STREAM_FAIL_THRESHOLD) {
    sync.streamSkipRemaining = STREAM_SKIP_AFTER_TRIP;
    sync.streamFailedAt = [];
  }
}

async function send(): Promise<void> {
  const message = inputEl.value.trim();
  if (!message) return;
  // Re-entry guard: while a stream is in flight the same button is the
  // stop button, so only the streaming click path should reach abort, not
  // a second send().
  if (sync.streamAbort) return;
  sendBtn.disabled = true;
  const sessBefore = currentSession();
  const sid = sessBefore.id;
  const isFirstUserMsg = !sessBefore.history.some((h) => h.role === "user");
  const eligibleForAutoTitle = isFirstUserMsg && sessBefore.titleManual !== true;

  // Optimistic user echo: render immediately, push to local history, register
  // dedup entry so the eventual `message_added` from long-poll is dropped
  // (matched on session_id+role+content+ts, see consumeIfDuplicate).
  addMessageBubble("user", message);
  sessBefore.history.push({ role: "user", text: message, ts: Date.now() });
  sessBefore.lastActiveAt = Date.now();
  if (sessBefore.title === "新会话" || !sessBefore.title) sessBefore.title = deriveTitle(sessBefore.history);
  recordOptimistic(sid, "user", message);
  saveStore();
  renderSessionList();

  inputEl.value = "";

  if (eligibleForAutoTitle) {
    requestAutoTitle(sid, message).catch(() => {});
  }

  const tryStream = streamingSupported() && !isStreamCircuitOpen();

  try {
    if (tryStream) {
      const outcome = await runStreamingSend(sid, message);
      if (outcome === "fallback") {
        // runStreamingSend's finally re-enabled the button so the stop
        // button stayed clickable during the streaming attempt; we're
        // about to fire a second HTTP request, so re-disable to prevent
        // the user double-clicking through fallback (which would either
        // duplicate the optimistic user echo or trip server-side
        // concurrent_request).
        sendBtn.disabled = true;
        showTyping();
        await runNonStreamingSend(sid, message);
      }
    } else {
      showTyping();
      await runNonStreamingSend(sid, message);
    }
  } finally {
    hideTyping();
    sendBtn.disabled = false;
    setSendMode("send");
  }
}

// Returns "ok" if the stream completed (success, mid-stream error rendered
// inline, or user-cancel after at least one chunk) and the caller should
// stop. Returns "fallback" if the streaming attempt failed before any
// content reached the bubble and the caller should retry via /chat.
async function runStreamingSend(sid: string, message: string): Promise<"ok" | "fallback"> {
  const ac = new AbortController();
  sync.streamAbort = ac;
  setSendMode("stop");
  // Stop button must remain clickable while the rest of the composer is
  // disabled-ish; sendBtn.disabled was set true at the top of send(), so
  // re-enable it here for the duration of the stream.
  sendBtn.disabled = false;

  // Empty bot bubble that chunks render into. The streaming class drives
  // the blinking caret; on completion we strip it so the bubble settles.
  hideTyping();
  const bubble = document.createElement("div");
  bubble.className = "msg bot md streaming";
  msgs.appendChild(bubble);
  scrollToEnd();

  let pending = "";
  let renderTimer: number | null = null;
  let firstChunkSeen = false;

  const flushRender = (): void => {
    bubble.innerHTML = renderMarkdown(pending);
    scrollToEnd();
  };
  const scheduleRender = (): void => {
    if (renderTimer !== null) return;
    renderTimer = window.setTimeout(() => {
      renderTimer = null;
      flushRender();
    }, STREAM_RENDER_DEBOUNCE_MS);
  };
  const cancelRender = (): void => {
    if (renderTimer !== null) {
      clearTimeout(renderTimer);
      renderTimer = null;
    }
  };

  const onChunk = (text: string): void => {
    firstChunkSeen = true;
    pending += text;
    scheduleRender();
  };

  const finalizeOk = (info: StreamDoneInfo): void => {
    cancelRender();
    flushRender();
    bubble.classList.remove("streaming");
    setBadge(info.remaining, info.daily_quota);
    const sess = store.sessions[sid];
    if (sess) {
      // Race-guard, mirror of the non-stream fix in commit 59d5da3:
      // server fires record_chat_pair + EventBus.notify before the SSE
      // `done` frame finishes draining. The two responses (events GET +
      // chat/stream POST) race on separate connections. If long-poll
      // delivered the matching `message_added` first, `applyEvent`
      // already pushed history + rendered a sibling bot bubble; pushing
      // again here would duplicate both. Detect via a content-equality
      // probe on the last 2 history entries and a 30s freshness window
      // — matches → drop the streaming bubble + skip push + skip
      // recordOptimistic (no future event will match).
      const tail = sess.history.slice(-2);
      const echoedByEvent = tail.some(
        (h) => h.role === "bot" && h.text === pending && (Date.now() - h.ts) < 30_000,
      );
      if (echoedByEvent) {
        bubble.remove();
        return;
      }
      sess.history.push({ role: "bot", text: pending, ts: Date.now() });
      sess.lastActiveAt = Date.now();
      saveStore();
      renderSessionList();
    }
    recordOptimistic(sid, "assistant", pending);
  };

  // Drop the empty/partial bubble. Used when the stream attempt fails
  // pre-first-chunk and we're falling back to /chat.
  const discardBubble = (): void => {
    cancelRender();
    bubble.remove();
  };

  // Settle the partial bubble when something cut the stream off after at
  // least one chunk arrived. We keep what was rendered, append a small
  // notice, and persist to history + dedup so a future `message_added`
  // event for the (potentially identical) full reply doesn't double-render.
  const settlePartial = (noticeText: string): void => {
    cancelRender();
    flushRender();
    bubble.classList.remove("streaming");
    if (noticeText) {
      const note = document.createElement("div");
      note.className = "stream-notice";
      note.textContent = noticeText;
      bubble.appendChild(note);
    }
    const sess = store.sessions[sid];
    if (sess) {
      sess.history.push({ role: "bot", text: pending, ts: Date.now() });
      sess.lastActiveAt = Date.now();
      saveStore();
      renderSessionList();
    }
    recordOptimistic(sid, "assistant", pending);
  };

  try {
    const info = await streamChat(sid, message, onChunk, ac.signal);
    finalizeOk(info);
    return "ok";
  } catch (e) {
    const err = e as { name?: string; message?: string };
    const isAbort = err.name === "AbortError";
    const sce = e instanceof StreamChatError ? e : null;

    if (isAbort) {
      if (firstChunkSeen) {
        settlePartial("[已中断]");
        return "ok";
      }
      // User cancelled before any text arrived: drop the empty bubble and
      // do not fall back — the user explicitly stopped the request.
      discardBubble();
      return "ok";
    }

    // 4xx and similar pre-stream HTTP failures: caller should fall back
    // to /chat which renders the appropriate inline error using existing
    // status-aware copy. Drop the empty bubble first.
    if (sce && sce.status >= 400 && sce.status < 600) {
      discardBubble();
      recordStreamFailure();
      return "fallback";
    }

    // Mid-stream error frame from the server (`data: {"error": "..."}`).
    // status is 0 in this branch. Surface the error inline; if we already
    // rendered partial text, keep it visible above the error banner.
    // Note: do NOT call recordStreamFailure() here — the SSE transport
    // worked end-to-end; the LLM (or upstream provider) is the one that
    // errored. Tripping the circuit breaker would force the next sends
    // through /chat where they'd hit the same LLM and fail identically,
    // and disable streaming for unrelated future sends.
    if (sce && sce.status === 0) {
      if (firstChunkSeen) {
        settlePartial("");
        addMessageBubble("error", streamErrorCopy(sce.code));
        return "ok";
      }
      discardBubble();
      addMessageBubble("error", streamErrorCopy(sce.code));
      return "ok";
    }

    // Network/timeout/abort-from-timeout. If we have partial content,
    // keep it and don't re-fire /chat (the server already started
    // generating; a second hit would either 429 concurrent_request or
    // double-charge quota). If we have nothing, fall back.
    if (firstChunkSeen) {
      settlePartial("[网络中断]");
      recordStreamFailure();
      return "ok";
    }
    discardBubble();
    recordStreamFailure();
    return "fallback";
  } finally {
    if (sync.streamAbort === ac) sync.streamAbort = null;
    setSendMode("send");
  }
}

function streamErrorCopy(code: string): string {
  if (code === "llm_timeout") return "回复生成超时，请稍后再试。";
  if (code === "llm_call_failed") return "上游模型调用失败，请稍后再试。";
  if (code === "stream_truncated") return "流式响应被截断。";
  return `请求失败: ${code}`;
}

async function runNonStreamingSend(sid: string, message: string): Promise<void> {
  try {
    const resp = await fetchWithTimeout(
      CHAT_URL,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...bearer() },
        body: JSON.stringify({ session_id: sid, username, message }),
      },
      FETCH_TIMEOUT_CHAT_MS,
    );
    let payload: Record<string, unknown> = {};
    try { payload = await resp.json(); } catch {}

    if (resp.ok) {
      setBadge(payload.remaining as number, payload.daily_quota as number);
      const reply = (payload.reply as string) || "(空回复)";
      // Race: long-poll's `/events` response can land BEFORE /chat 200,
      // because record_chat_pair fires + notifies the EventBus right
      // before the chat handler returns. The two responses then race on
      // separate HTTP connections. If the event arrives first, applyEvent
      // already rendered + pushed the assistant bubble (no pending entry
      // existed because we hadn't reached this branch yet, so dedup
      // couldn't fire). Without this guard, the same reply gets rendered
      // twice. Match key: last history entry must be the bot, same exact
      // content, written within the last 30s.
      const sessNow = store.sessions[sid];
      const last = sessNow?.history[sessNow.history.length - 1];
      const eventAlreadyDelivered = !!(
        last
        && last.role === "bot"
        && last.text === reply
        && (Date.now() - last.ts) < 30_000
      );
      if (!eventAlreadyDelivered) {
        // Render assistant reply locally on /chat 200 so the user isn't stuck on
        // the typing indicator if the long-poll is momentarily wedged. Same
        // dedup mechanism as the user echo: register a pending entry so the
        // upcoming `message_added` event for the assistant text is suppressed.
        addMessageBubble("bot", reply);
        if (sessNow) {
          sessNow.history.push({ role: "bot", text: reply, ts: Date.now() });
          sessNow.lastActiveAt = Date.now();
          saveStore();
          renderSessionList();
        }
        recordOptimistic(sid, "assistant", reply);
      } else {
        hideTyping();
      }
      return;
    }

    const err = (payload.error as string) || `http_${resp.status}`;
    const s = resp.status;
    if (s === 401) {
      addMessageBubble("error", "Token 无效或已撤销，请重新登录。");
      setTimeout(() => { handle401(); }, 1500);
    } else if (s === 429 && err === "quota_exceeded") {
      setBadge(0, payload.daily_quota as number);
      addMessageBubble("notice", "今日额度已用完，明日 0 点重置。");
    } else if (s === 429 && err === "concurrent_request") addMessageBubble("notice", "上一条还在处理中，稍候。");
    else if (s === 429 && err === "ip_blocked") {
      const retry = resp.headers.get("Retry-After") || payload.retry_after || "?";
      addMessageBubble("error", `请求过于频繁，已暂时封禁，${retry} 秒后重试。`);
    } else if (s === 400 && err === "message_too_long") addMessageBubble("error", `消息过长 (上限 ${payload.max_length})。`);
    else if (s === 403 && err === "forbidden_origin") addMessageBubble("error", "页面来源未在 allowed_origins 中。");
    else addMessageBubble("error", `请求失败: ${err} ${payload.detail || ""}`);
  } catch (error) {
    addMessageBubble("error", `网络错误: ${String(error)}`);
  }
}

$<HTMLButtonElement>("clearHistory").onclick = () => { void clearActiveHistory(); };
$<HTMLButtonElement>("newSessionBtn").onclick = newSession;
$<HTMLButtonElement>("logout").onclick = () => {
  if (!confirm("登出会清除本机保存的 token 与对话历史。继续？")) return;
  sync.stopped = true;
  abortInflightLongPoll();
  if (sync.streamAbort) { sync.streamAbort.abort(); sync.streamAbort = null; }
  clearTimer("shortPollTimer");
  clearTimer("probeTimer");
  clearTimer("retryTimer");
  for (const k of [LS_TOKEN, LS_USERNAME, LS_STORE, LS_LAST_PTS]) localStorage.removeItem(k);
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
    void send();
  }
});
sendBtn.onclick = (): void => {
  // Same button doubles as stop while a stream is in flight. Click during
  // stream cancels the AbortController; the streaming path catches the
  // resulting AbortError and either keeps the partial bubble or drops it.
  if (sync.streamAbort) {
    sync.streamAbort.abort();
    return;
  }
  void send();
};

document.addEventListener("visibilitychange", onVisibilityChange);

// Cold boot: paint cache, then refetch authoritative state, then start sync.
saveStore();
renderSessionList();
replayActive();
loadChatSite();
setupThemeToggle();
void probeQuota();

void (async (): Promise<void> => {
  try {
    await coldRefetch();
  } catch {
    // Cold refetch failed: keep local cache, mark offline-ish, still try the
    // long-poll loop — it will retry the events endpoint and surface failure
    // through the status badge.
    setSyncStatus("offline");
  }
  if (!sync.stopped) void runLongPoll();
})();
