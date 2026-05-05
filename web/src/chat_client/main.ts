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
// Pending streams live in their own LS slot so the persist-every-N-chunks
// hot path doesn't have to re-serialize the entire ChatStore (which can be
// hundreds of KB for a chatty user). A 200KB streaming reply was costing
// ~20 full-store JSON.stringifies and triggering frame jank on weaker
// hardware. Splitting it keeps streaming writes O(small).
const LS_PENDING_STREAMS = "wcg.chat.pending_streams";

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
interface HistoryItem { role: Role; text: string; ts: number; incomplete?: boolean; }
interface SessionMeta {
  id: string;
  title: string;
  titleManual?: boolean;
  pinned?: boolean;
  lastActiveAt: number;
  history: HistoryItem[];
}
// Survives refresh. When the SSE stream is interrupted (network drop, page
// reload) we preserve enough state here to attach back to the same server
// stream via GET /chat/stream/{id}/resume?after_seq=N.
//   * stream_id: opaque token assigned by the server in the first SSE data frame.
//   * last_seq: highest seq we've already consumed; the resume endpoint
//     replays seq>last_seq plus continues live.
//   * pending_text: rehydrate seed for the streaming bubble — what the bubble
//     looked like at last_seq, so a refresh can show the partial immediately
//     before the resume socket starts delivering more chunks.
//   * started_at: client-side ms timestamp; useful for diagnostics + future TTL.
interface PendingStream {
  stream_id: string;
  session_id: string;
  last_seq: number;
  pending_text: string;
  started_at: number;
}
interface ChatStore {
  activeId: string;
  sessions: Record<string, SessionMeta>;
  pendingStreams: Record<string, PendingStream>;
}

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
interface ServerMessage { role: ServerRole; content: string; ts?: number; incomplete?: boolean; }
interface ServerConversationDetail {
  session_id: string;
  title: string;
  title_manual?: boolean;
  pinned?: boolean;
  updated_at: number;
  messages: ServerMessage[];
}
type EventType =
  | "session_created"
  | "session_meta_updated"
  | "history_cleared"
  | "message_added"
  | "stream_started"
  | "stream_ended";
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
  if (o.incomplete !== undefined && typeof o.incomplete !== "boolean") return false;
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
function isPendingStream(it: unknown): it is PendingStream {
  if (!it || typeof it !== "object") return false;
  const o = it as Record<string, unknown>;
  return typeof o.stream_id === "string" && typeof o.session_id === "string" &&
    typeof o.last_seq === "number" && typeof o.pending_text === "string" &&
    typeof o.started_at === "number";
}

// Cache-only loader. Server is authoritative; this just gets us a non-blank
// first paint while the cold refetch is in flight. Corrupt JSON → blank store.
function loadStore(): ChatStore {
  let parsed: unknown = null;
  try { parsed = JSON.parse(localStorage.getItem(LS_STORE) || "null"); } catch {}
  if (!parsed || typeof parsed !== "object") {
    const fresh = blankSession();
    return { activeId: fresh.id, sessions: { [fresh.id]: fresh }, pendingStreams: loadPendingStreams() };
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
  // pendingStreams now persists to its own LS key. Anything still embedded
  // in the main store is from a build that was running between the v2 ship
  // and this fix; absorb it here so we don't lose an in-flight resume on
  // refresh, then a saveStore() at the end of this function will rewrite
  // the main blob without the field.
  const legacyPendingIn = (p.pendingStreams && typeof p.pendingStreams === "object")
    ? p.pendingStreams as Record<string, unknown>
    : {};
  const pendingStreams = loadPendingStreams();
  for (const [k, v] of Object.entries(legacyPendingIn)) {
    if (pendingStreams[k]) continue;
    if (isPendingStream(v) && v.session_id === k) pendingStreams[k] = v;
  }
  return { activeId, sessions, pendingStreams };
}

function loadPendingStreams(): Record<string, PendingStream> {
  let parsed: unknown = null;
  try { parsed = JSON.parse(localStorage.getItem(LS_PENDING_STREAMS) || "null"); } catch {}
  if (!parsed || typeof parsed !== "object") return {};
  const out: Record<string, PendingStream> = {};
  for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
    if (isPendingStream(v) && v.session_id === k) out[k] = v;
  }
  return out;
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
// Main store does NOT include pendingStreams — that lives in its own LS
// key (LS_PENDING_STREAMS) so the persist-every-N-chunks hot path doesn't
// re-serialize the entire history. The destructure picks out the fields we
// actually want to persist.
const saveStore = (): void => {
  try {
    const { activeId, sessions } = store;
    localStorage.setItem(LS_STORE, JSON.stringify({ activeId, sessions }));
  } catch {}
};
const savePendingStreams = (): void => {
  try { localStorage.setItem(LS_PENDING_STREAMS, JSON.stringify(store.pendingStreams)); } catch {}
};
const saveLastPts = (pts: number): void => { try { localStorage.setItem(LS_LAST_PTS, String(pts)); } catch {} };
const currentSession = (): SessionMeta => store.sessions[store.activeId]!;
const serverRoleToLocal = (r: ServerRole): Role => r === "assistant" ? "bot" : "user";

function replayActive(): void {
  clearMsgList();
  for (const item of currentSession().history) {
    addMessageBubble(item.role, item.text);
    if (item.role === "bot" && item.incomplete) appendIncompleteNoticeToLastBubble();
  }
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

// Append a "（回复未完整）" notice to the most recently rendered bot bubble
// (assumed to be the last `.msg.bot` in the message list). Used by the
// history-replay path when a stored assistant message has `incomplete: true`,
// and by the resume path when the server's terminal frame says incomplete.
function appendIncompleteNoticeToLastBubble(): void {
  const list = msgs.querySelectorAll<HTMLDivElement>(".msg.bot");
  const last = list.length ? list[list.length - 1] : null;
  if (!last) return;
  if (last.querySelector(".stream-notice.incomplete")) return;
  const note = document.createElement("div");
  note.className = "stream-notice incomplete";
  note.textContent = "（回复未完整）";
  last.appendChild(note);
}

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
  streamAbort: AbortController | null;  // active /chat/stream POST fetch+reader; null when not streaming
  streamFailedAt: number[];          // ms timestamps of recent /chat/stream failures
  streamSkipRemaining: number;       // sends to bypass /chat/stream after a trip
  // Per-session resume controllers. A resume runs in the background even
  // after the user switches away from the session, so we track who owns
  // each one to avoid double-attaching when the user comes back. Cleared
  // when the resume settles (success/failure/abort).
  activeResumeAborts: Record<string, AbortController>;
  // Peer-device stream notifications keyed by session_id. Populated by
  // the long-poll `stream_started` event and consumed when the user opens
  // the session (which triggers attemptCrossDeviceLiveAttach). NOT
  // persisted — these are ephemeral cues, not durable state.
  peerStreamsBySession: Record<string, string>;
  // Sessions with a typing indicator on their sidebar entry (because a
  // peer device is currently driving a stream there). Mirrors the set
  // implied by peerStreamsBySession but kept explicit so renderSessionList
  // can dot the entry without re-checking activeResumeAborts.
  sidebarTypingFor: Set<string>;
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
  activeResumeAborts: {},
  peerStreamsBySession: {},
  sidebarTypingFor: new Set(),
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
      const incomplete = payload["incomplete"] === true;
      if ((role !== "user" && role !== "assistant") || typeof content !== "string") break;
      // Dedup: if we already rendered this locally on this device, drop the event.
      if (consumeIfDuplicate(sid, role, content, ev.ts)) {
        // Still bump lastActiveAt so the sidebar order matches the server.
        const s = store.sessions[sid];
        if (s) s.lastActiveAt = Math.max(s.lastActiveAt, ev.ts * 1000);
        // If the originating-device dedup matched but the server says
        // incomplete and the locally-stored entry doesn't, patch the flag
        // so a future replay shows the notice. The bubble already on
        // screen was rendered (potentially with its own notice) by the
        // streaming path; nothing to mutate there.
        if (incomplete && role === "assistant") {
          const last = s?.history[s.history.length - 1];
          if (last && last.role === "bot" && last.text === content) last.incomplete = true;
        }
        break;
      }
      let sess = store.sessions[sid];
      if (!sess) {
        sess = blankSession(sid);
        store.sessions[sid] = sess;
      }
      const localRole: Role = serverRoleToLocal(role as ServerRole);
      const item: HistoryItem = { role: localRole, text: content, ts: ev.ts * 1000 };
      if (incomplete && role === "assistant") item.incomplete = true;
      sess.history.push(item);
      sess.lastActiveAt = ev.ts * 1000;
      if (role === "user" && (sess.title === "新会话" || !sess.title)) {
        sess.title = deriveTitle(sess.history);
      }
      if (sid === store.activeId) {
        addMessageBubble(localRole, content);
        if (item.incomplete) appendIncompleteNoticeToLastBubble();
      }
      break;
    }
    case "stream_started": {
      const streamId = payload["stream_id"];
      if (typeof streamId !== "string" || !streamId) break;
      // Three cases:
      //   1. Originating device, SSE-first-frame already landed — local
      //      PendingStream has the same stream_id; an attach here would
      //      duplicate the active driver. Skip.
      //   2. Originating device, SSE-first-frame hasn't landed yet but
      //      the POST is in flight (sync.streamAbort set, sid is active).
      //      Skip; the SSE will deliver chunks to the existing bubble.
      //   3. Peer device — no local pending state for this stream_id, no
      //      active POST. Trigger live attach (active session) or sidebar
      //      mark (closed).
      const localPending = store.pendingStreams[sid];
      if (localPending && localPending.stream_id === streamId) break;
      if (sync.streamAbort && sid === store.activeId) break;
      // Already running a resume for this session (e.g. duplicate
      // long-poll delivery from has_more retry) — don't double-attach.
      if (sync.activeResumeAborts[sid]) break;
      sync.peerStreamsBySession[sid] = streamId;
      sync.sidebarTypingFor.add(sid);
      if (sid === store.activeId) {
        // Fire-and-forget; live attach manages its own lifecycle and
        // surfaces failures inline. Errors landing here are already
        // reported on the bubble or notice channel.
        void attemptCrossDeviceLiveAttach(sid, streamId);
      }
      break;
    }
    case "stream_ended": {
      const streamId = payload["stream_id"];
      // Drop sidebar typing indicator + clear the peer stream entry. The
      // message_added events that landed in the same long-poll block
      // already populated the bubble (existing dedup window covers it).
      // We do NOT abort an in-flight resume here: the resume's own
      // terminal frame is the authoritative completion signal, and
      // cancelling it could drop chunks that haven't been parsed yet.
      const peerStream = sync.peerStreamsBySession[sid];
      if (typeof streamId === "string" && peerStream && peerStream !== streamId) break;
      delete sync.peerStreamsBySession[sid];
      sync.sidebarTypingFor.delete(sid);
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
  sess.history = detail.messages.map((m) => {
    const item: HistoryItem = {
      role: serverRoleToLocal(m.role),
      text: m.content,
      ts: (m.ts ?? detail.updated_at) * 1000,
    };
    if (m.role === "assistant" && m.incomplete === true) item.incomplete = true;
    return item;
  });
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
  // Pending streams reference server-side buffers tied to the OLD token.
  // The new login can't resume them (cross-token resume returns 404 on
  // purpose to avoid stream-existence enumeration), so leaving them on
  // disk would just trigger a doomed resume on the next bootstrap.
  localStorage.removeItem(LS_PENDING_STREAMS);
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
    time.className = "session-time";
    if (sync.sidebarTypingFor.has(sess.id)) {
      // Peer device is currently driving a stream for this session that
      // we're not actively attached to. Surface a tiny "正在输入…" cue on
      // the sidebar entry's time line so the user knows there's activity
      // they can tab into.
      time.textContent = "正在输入…";
    } else {
      time.textContent = relativeTime(sess.lastActiveAt);
    }
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
      // Skip the rerender if a resume is in flight for this session: the
      // streaming bubble is in the DOM and replayActive() would wipe it.
      // The completed reply will land via the resume's finalize logic +
      // long-poll's message_added (with dedup), so we won't lose state.
      if (id === store.activeId && !sync.activeResumeAborts[id]) replayActive();
      renderSessionList();
    }).catch(() => {});
    // Stream attach decisions, in priority order:
    //   1. We already have a resume running for this session — leave it
    //      alone (controller stays in activeResumeAborts).
    //   2. We have a local PendingStream from a prior POST that didn't
    //      reach `done` — try to resume it.
    //   3. A peer device started a stream for this session and we got
    //      stream_started while it wasn't open — attach now.
    if (!sync.activeResumeAborts[id]) {
      if (store.pendingStreams[id]) {
        void attemptResumeOnLoad(id);
      } else {
        const peerStream = sync.peerStreamsBySession[id];
        if (peerStream) void attemptCrossDeviceLiveAttach(id, peerStream);
      }
    }
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

// Distinguishes a 404 stream_not_found from other resume failures so the
// caller can fall through to the history-fetch / new-POST path quickly.
class StreamNotFoundError extends Error {
  constructor() {
    super("stream_not_found");
    this.name = "StreamNotFoundError";
  }
}

interface StreamDoneInfo {
  remaining: number;
  daily_quota: number;
  incomplete: boolean;
  stream_id: string;
}

// Per-chunk callback. `seq` is the chunk's sequence number from the wire;
// for legacy frames (no `seq` field) callers receive a synthesized
// last_seq + 1 so persistence accounting still works.
type StreamChunkHandler = (seq: number, text: string) => void;

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
//
// Wire format per PLAN_chat_streaming_v2.md:
//   - First data frame: `{"stream_id": "..."}` (POST path only)
//   - Each chunk: `{"chunk": "...", "seq": N}`
//   - done: `{"done": true, "seq": N, "remaining": ..., "daily_quota": ..., "incomplete": bool}`
//   - error: `{"error": "<code>", "seq": N}`
// Legacy frames without `seq` are tolerated — we synthesize last_seq+1 so
// the persistence path keeps advancing.
async function consumeSseStream(
  resp: Response,
  ctx: {
    onStreamId?: (id: string) => void;
    onChunk: StreamChunkHandler;
    initialSeq: number;
  },
): Promise<StreamDoneInfo> {
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
  let lastSeq = ctx.initialSeq;
  let streamId = "";

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
    // First frame on POST path is `{"stream_id":"..."}` with no chunk; the
    // resume path never receives this frame because the client already
    // knows the id. Legacy server may interleave it with a chunk in the
    // same JSON object, so check for both shapes.
    if (typeof obj.stream_id === "string" && !streamId) {
      streamId = obj.stream_id;
      ctx.onStreamId?.(streamId);
    }
    if (typeof obj.chunk === "string") {
      const seq = typeof obj.seq === "number" ? obj.seq : lastSeq + 1;
      lastSeq = Math.max(lastSeq, seq);
      ctx.onChunk(seq, obj.chunk);
      return;
    }
    if (obj.done === true) {
      const remaining = typeof obj.remaining === "number" ? obj.remaining : 0;
      const daily = typeof obj.daily_quota === "number" ? obj.daily_quota : 0;
      const incomplete = obj.incomplete === true;
      const seq = typeof obj.seq === "number" ? obj.seq : lastSeq + 1;
      lastSeq = Math.max(lastSeq, seq);
      doneInfo = { remaining, daily_quota: daily, incomplete, stream_id: streamId };
      return;
    }
    if (typeof obj.error === "string") {
      const seq = typeof obj.seq === "number" ? obj.seq : lastSeq + 1;
      lastSeq = Math.max(lastSeq, seq);
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

async function streamChat(
  sid: string,
  message: string,
  onChunk: StreamChunkHandler,
  signal: AbortSignal,
  onStreamId?: (id: string) => void,
): Promise<StreamDoneInfo> {
  // No fetch wall-clock timer here. The server's per-chunk idle timeout
  // (default 60s, configurable via llm_timeout_seconds) plus the 20s
  // `: keepalive` SSE comment is the source of truth for "is this stream
  // alive?". A wall-clock cap on the frontend would kill long-running
  // replies even when chunks are still flowing — the bug this fix is
  // targeting. Cancellation is via the AbortController in `signal`
  // (stop button, page unload, session-level lifecycle).
  const resp = await fetch(CHAT_STREAM_URL, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...bearer() },
    body: JSON.stringify({ session_id: sid, username, message }),
    signal,
  });

  if (!resp.ok) {
    let payload: Record<string, unknown> = {};
    try { payload = await resp.json() as Record<string, unknown>; } catch {}
    const code = typeof payload.error === "string" ? payload.error : `http_${resp.status}`;
    throw new StreamChatError(code, resp.status, code, payload);
  }

  return await consumeSseStream(resp, {
    onStreamId,
    onChunk,
    initialSeq: -1,
  });
}

// Reattach to a server-side stream that was created by a prior POST. Used
// from three places:
//   1. `runStreamingSend` recovery path — caller hit send while a
//      PendingStream was still on disk (rare; usually bootstrap handles it).
//   2. `attemptResumeOnLoad` on chat-page bootstrap / session switch.
//   3. Cross-device live attach via the `stream_started` long-poll event.
//
// `after_seq = -1` means "give me everything"; otherwise the server replays
// seq > after_seq plus continues live until terminal.
async function resumeStream(
  stream_id: string,
  after_seq: number,
  onChunk: StreamChunkHandler,
  signal: AbortSignal,
): Promise<StreamDoneInfo> {
  const url = `${CHAT_STREAM_URL}/${encodeURIComponent(stream_id)}/resume?after_seq=${after_seq}`;
  // Same reasoning as streamChat: no fetch wall-clock cap. The server's
  // per-chunk idle timeout + 20s heartbeat is the liveness signal. Long
  // resumes attaching to in-flight streams must not be killed by the
  // frontend mid-stream.
  const resp = await fetch(url, {
    method: "GET",
    credentials: "same-origin",
    headers: bearer(),
    signal,
  });

  if (resp.status === 404) {
    let payload: Record<string, unknown> = {};
    try { payload = await resp.json() as Record<string, unknown>; } catch {}
    if (payload.error === "stream_not_found") throw new StreamNotFoundError();
    // Other 404 (mistyped path, etc.) → bubble as generic error so the
    // caller can decide whether to fall back to history-fetch.
    throw new StreamChatError("stream_not_found", 404, "stream_not_found", payload);
  }
  if (resp.status === 401) {
    handle401();
    throw new StreamChatError("unauthorized", 401, "unauthorized");
  }
  if (!resp.ok) {
    let payload: Record<string, unknown> = {};
    try { payload = await resp.json() as Record<string, unknown>; } catch {}
    const code = typeof payload.error === "string" ? payload.error : `http_${resp.status}`;
    throw new StreamChatError(code, resp.status, code, payload);
  }

  return await consumeSseStream(resp, {
    onChunk,
    initialSeq: after_seq,
  });
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
  const sessBefore = currentSession();
  const sid = sessBefore.id;
  // Resume-in-flight gate. A PendingStream means we're either mid-resume
  // for a previous turn or have one queued from a refresh; firing a new
  // POST now would either 429 concurrent_request server-side or, worse,
  // race with the resume's persisted reply. Surface a soft notice and
  // let the user wait for the existing stream to settle.
  if (store.pendingStreams[sid] || sync.activeResumeAborts[sid]) {
    addMessageBubble("notice", "正在恢复上一次回复…");
    return;
  }
  sendBtn.disabled = true;
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
  autosizeInput();

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

// Outcome of a single streaming bubble's lifecycle. "ok" includes
// successful completion, partial-with-content, and user-cancel-after-chunk.
// "fallback" means the stream attempt failed before any chunk made it to
// the bubble and the caller should retry via /chat (only the POST path
// uses this — resume callers always return "ok" because there's no
// fallback action that makes sense for a server-side stream we attached
// to after the fact).
type StreamingOutcome = "ok" | "fallback";

// Snapshot of streaming progress passed to onBeforeFinalize for the
// race-guard against long-poll's already-emitted message_added.
interface StreamingFinalizeSnapshot {
  pending: string;
  bubble: HTMLDivElement;
  incomplete: boolean;
}

// Configuration for a streaming bubble attachment. Three call sites:
//   1. POST send: kind="post", takes the per-send AbortController as
//      sync.streamAbort, registers itself in pendingStreams once the
//      first stream_id frame arrives, and runs streamChat.
//   2. Recovery resume on bootstrap or session-switch: kind="resume",
//      seeded with PendingStream.pending_text + last_seq, runs resumeStream.
//   3. Peer-device live attach via stream_started event: kind="peer",
//      attaches with after_seq=-1 and no pre-rendered text, runs resumeStream.
interface StreamingAttachOpts {
  sid: string;
  kind: "post" | "resume" | "peer";
  // For "post": the message body to send. Unused otherwise.
  message?: string;
  // For "resume": the stream_id and last_seq to attach with. Unused for "post".
  streamId?: string;
  afterSeq?: number;
  // For "resume": text already rendered before page reload, used to seed
  // the bubble immediately so the user sees something while the resume
  // socket reconnects.
  initialText?: string;
}

// Heart of the streaming machinery — hosts a single streaming bubble for a
// given session and drives it to completion via either streamChat or
// resumeStream. Shared between the POST, recovery-resume, and peer-attach
// code paths so the bubble lifecycle is identical across them.
async function attachStreamingBubble(opts: StreamingAttachOpts): Promise<StreamingOutcome> {
  const { sid, kind } = opts;
  const ac = new AbortController();
  if (kind === "post") {
    sync.streamAbort = ac;
    setSendMode("stop");
    sendBtn.disabled = false;
  } else {
    // For resume / peer attach, register the controller in
    // activeResumeAborts so a duplicate stream_started or a session-switch
    // can detect "already attached" and skip starting a second resume.
    sync.activeResumeAborts[sid] = ac;
  }

  // Empty bot bubble that chunks render into. The streaming class drives
  // the blinking caret; on completion we strip it so the bubble settles.
  hideTyping();
  const bubble = document.createElement("div") as HTMLDivElement;
  bubble.className = "msg bot md streaming";
  msgs.appendChild(bubble);
  scrollToEnd();

  // Resume-from-refresh seed: the user already saw `initialText` rendered
  // before the page reload. Show it back immediately so the bubble isn't
  // empty during the reconnect handshake.
  let pending = "";
  let firstChunkSeen = false;
  // Typewriter state — declared here so the seed-init below can mark the
  // already-on-screen prefix as fully displayed and the typewriter loop
  // only animates new chunks that arrive AFTER the resume hook-up.
  let displayedLength = 0;
  let rafToken = 0;
  if (kind === "resume" && typeof opts.initialText === "string" && opts.initialText.length) {
    pending = opts.initialText;
    bubble.textContent = pending;
    displayedLength = pending.length;
    firstChunkSeen = true;
    scrollToEnd();
  }

  let lastSeq = typeof opts.afterSeq === "number" ? opts.afterSeq : -1;
  let streamId = typeof opts.streamId === "string" ? opts.streamId : "";
  // Persist the PendingStream periodically so a refresh halfway through a
  // long reply doesn't lose ground. Every CHUNKS_PER_PERSIST chunks is a
  // good balance: cheap enough to not thrash localStorage, frequent
  // enough to bound rewind on refresh.
  const CHUNKS_PER_PERSIST = 10;
  let chunksSincePersist = 0;

  // Typewriter renderer. Provider chunks usually land word-level (5-15
  // chars at a time) at irregular ~100-300ms intervals. A naive
  // "render-on-arrival" strategy makes the UI lurch in word-bursts; a
  // fixed-interval debounce just batches lurches into bigger ones.
  // Instead, decouple network arrival from display: `pending` is the
  // ground truth from chunks, `displayedLength` is what's currently in
  // the DOM, and a requestAnimationFrame loop closes the gap.
  //
  // Rendering during streaming uses textContent (cheap O(n) DOM update,
  // no markdown parsing) — running marked + DOMPurify per frame for a
  // multi-KB reply would saturate the main thread and stutter the
  // animation, especially on slower devices. Markdown formatting kicks
  // in once on the terminal frame via flushRender, where the bubble
  // swaps to innerHTML with the full markdown render. The CSS rule on
  // `.msg.bot.md.streaming` keeps newlines + spaces visible during the
  // textContent phase so paragraph structure stays readable.
  const renderTo = (n: number): void => {
    bubble.textContent = pending.slice(0, n);
    scrollToEnd();
  };
  const tick = (): void => {
    rafToken = 0;
    const backlog = pending.length - displayedLength;
    if (backlog <= 0) return;
    const advance = Math.max(2, Math.ceil(backlog / 30));
    displayedLength = Math.min(pending.length, displayedLength + advance);
    renderTo(displayedLength);
    if (displayedLength < pending.length) {
      rafToken = requestAnimationFrame(tick);
    }
  };
  const scheduleRender = (): void => {
    if (rafToken !== 0) return;
    rafToken = requestAnimationFrame(tick);
  };
  // flushRender: terminal path. Stop the rAF, snap to full text, and
  // perform the ONE markdown render of the entire reply. The streaming
  // class is removed by the caller (finalizeBubble / similar), which
  // also reverts white-space back to the default so nested <pre>
  // / <code> blocks render correctly.
  const flushRender = (): void => {
    if (rafToken !== 0) {
      cancelAnimationFrame(rafToken);
      rafToken = 0;
    }
    displayedLength = pending.length;
    bubble.innerHTML = renderMarkdown(pending);
    scrollToEnd();
  };
  const cancelRender = (): void => {
    if (rafToken !== 0) {
      cancelAnimationFrame(rafToken);
      rafToken = 0;
    }
  };

  const persistPending = (): void => {
    if (!streamId) return;
    store.pendingStreams[sid] = {
      stream_id: streamId,
      session_id: sid,
      last_seq: lastSeq,
      pending_text: pending,
      started_at: store.pendingStreams[sid]?.started_at ?? Date.now(),
    };
    savePendingStreams();
  };
  const clearPending = (): void => {
    if (store.pendingStreams[sid]) {
      delete store.pendingStreams[sid];
      savePendingStreams();
    }
  };

  const onStreamId = (id: string): void => {
    streamId = id;
    // Persist immediately on the first stream_id frame so a refresh
    // BEFORE any chunks arrive can still resume.
    persistPending();
  };

  const onChunk: StreamChunkHandler = (seq, text) => {
    firstChunkSeen = true;
    pending += text;
    if (seq > lastSeq) lastSeq = seq;
    scheduleRender();
    chunksSincePersist += 1;
    if (chunksSincePersist >= CHUNKS_PER_PERSIST) {
      chunksSincePersist = 0;
      persistPending();
    }
  };

  // Render the small notice + persist + dedup-record. Used by both
  // settlePartial (network drop / abort) and finalizeOk-incomplete
  // (server's done frame says incomplete). The two callers differ only
  // in which notice text and which kind of `incomplete` flag they record.
  const finalizeBubble = (
    snapshot: StreamingFinalizeSnapshot,
    noticeKind: "" | "incomplete" | "interrupted" | "error",
  ): void => {
    cancelRender();
    flushRender();
    bubble.classList.remove("streaming");
    if (noticeKind) {
      const note = document.createElement("div");
      const isIncomplete = noticeKind === "incomplete";
      note.className = isIncomplete ? "stream-notice incomplete" : "stream-notice";
      note.textContent = isIncomplete
        ? "（回复未完整）"
        : noticeKind === "interrupted"
          ? "[已中断]"
          : "[网络中断]";
      bubble.appendChild(note);
    }
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
        (h) => h.role === "bot" && h.text === snapshot.pending && (Date.now() - h.ts) < 30_000,
      );
      if (echoedByEvent) {
        bubble.remove();
        return;
      }
      const item: HistoryItem = { role: "bot", text: snapshot.pending, ts: Date.now() };
      if (snapshot.incomplete) item.incomplete = true;
      sess.history.push(item);
      sess.lastActiveAt = Date.now();
      saveStore();
      renderSessionList();
    }
    recordOptimistic(sid, "assistant", snapshot.pending);
  };

  const finalizeOk = (info: StreamDoneInfo): void => {
    setBadge(info.remaining, info.daily_quota);
    finalizeBubble(
      { pending, bubble, incomplete: info.incomplete },
      info.incomplete ? "incomplete" : "",
    );
    clearPending();
  };

  // Drop the empty/partial bubble. Used when the stream attempt fails
  // pre-first-chunk and we're falling back to /chat.
  const discardBubble = (): void => {
    cancelRender();
    bubble.remove();
    clearPending();
  };

  const settlePartial = (kind2: "interrupted" | "error" | "incomplete", incomplete: boolean): void => {
    finalizeBubble({ pending, bubble, incomplete }, kind2);
    clearPending();
  };

  try {
    let info: StreamDoneInfo;
    if (kind === "post") {
      info = await streamChat(sid, opts.message ?? "", onChunk, ac.signal, onStreamId);
    } else {
      info = await resumeStream(streamId, lastSeq, onChunk, ac.signal);
    }
    finalizeOk(info);
    return "ok";
  } catch (e) {
    const err = e as { name?: string; message?: string };
    const isAbort = err.name === "AbortError";
    const sce = e instanceof StreamChatError ? e : null;
    const notFound = e instanceof StreamNotFoundError;

    if (notFound) {
      // Resume target is gone (past the 30s grace TTL or never existed).
      // For "resume" kind we have rendered partial text; settle it as
      // incomplete and let the long-poll's eventual message_added (if any)
      // dedup against this entry.
      if (kind === "resume" && firstChunkSeen) {
        settlePartial("incomplete", true);
        return "ok";
      }
      // Peer attach landed too late — server already evicted the buffer.
      // Drop the empty bubble; the message_added events that were emitted
      // alongside stream_ended will populate history through applyEvent.
      discardBubble();
      return "ok";
    }

    if (isAbort) {
      if (firstChunkSeen) {
        settlePartial("interrupted", false);
        // For "post" we DO leave PendingStream in place so a refresh
        // resumes (server-side keeps generating). The clearPending() in
        // settlePartial above already removed it; re-persist on abort
        // for the post path. Resume-kind aborts (session switch with
        // "abort+restart" choice) also re-persist so the next attach
        // picks up where this one left off.
        persistPending();
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
      if (kind === "post") {
        discardBubble();
        recordStreamFailure();
        return "fallback";
      }
      // Resume / peer attach: surface the error inline so the user knows
      // the recovery failed; long-poll will eventually backfill via
      // message_added if the server persisted anything. Don't drop the
      // bubble if we already painted the seeded partial — keep it visible
      // so the user doesn't see content disappear, settle it as incomplete.
      if (firstChunkSeen) {
        settlePartial("incomplete", true);
      } else {
        discardBubble();
      }
      addMessageBubble("error", streamErrorCopy(sce.code));
      return "ok";
    }

    // Mid-stream error frame from the server (`data: {"error": "..."}`).
    // status is 0 in this branch. Surface the outcome inline; if we already
    // rendered partial text, keep it visible.
    // Note: do NOT call recordStreamFailure() here — the SSE transport
    // worked end-to-end; the LLM (or upstream provider) is the one that
    // errored. Tripping the circuit breaker would force the next sends
    // through /chat where they'd hit the same LLM and fail identically,
    // and disable streaming for unrelated future sends.
    if (sce && sce.status === 0) {
      // llm_timeout (server's idle-chunk timeout cut the stream) and
      // empty_reply (upstream LLM returned finish_reason=stop with zero
      // tokens) are both soft outcomes — service is fine, this particular
      // turn just didn't produce a complete reply. Render them as amber
      // notices with actionable copy, NOT as red errors. Other mid-stream
      // error codes (llm_call_failed, internal_error) ARE service-side
      // failures and keep the red treatment.
      const isSoft = sce.code === "llm_timeout" || sce.code === "empty_reply";
      if (firstChunkSeen) {
        if (isSoft) {
          settlePartial("incomplete", true);
        } else {
          settlePartial("error", false);
          addMessageBubble("error", streamErrorCopy(sce.code));
        }
        return "ok";
      }
      discardBubble();
      addMessageBubble(
        isSoft ? "notice" : "error",
        streamErrorCopy(sce.code),
      );
      return "ok";
    }

    // Network/timeout/abort-from-timeout. If we have partial content,
    // keep it and don't re-fire /chat (the server already started
    // generating; a second hit would either 429 concurrent_request or
    // double-charge quota). If we have nothing, fall back.
    if (firstChunkSeen) {
      settlePartial("error", false);
      // Leave PendingStream alive on net-drop so a future attach can
      // recover; settlePartial cleared it, restore.
      persistPending();
      if (kind === "post") recordStreamFailure();
      return "ok";
    }
    discardBubble();
    if (kind === "post") {
      recordStreamFailure();
      return "fallback";
    }
    return "ok";
  } finally {
    if (kind === "post") {
      if (sync.streamAbort === ac) sync.streamAbort = null;
      setSendMode("send");
    } else {
      if (sync.activeResumeAborts[sid] === ac) delete sync.activeResumeAborts[sid];
    }
  }
}

// Returns "ok" if the stream completed (success, mid-stream error rendered
// inline, or user-cancel after at least one chunk) and the caller should
// stop. Returns "fallback" if the streaming attempt failed before any
// content reached the bubble and the caller should retry via /chat.
async function runStreamingSend(sid: string, message: string): Promise<"ok" | "fallback"> {
  return await attachStreamingBubble({ sid, kind: "post", message });
}

// Called when the chat page boots or when the user switches sessions. If
// a PendingStream exists for the session we attach to it via resume; the
// rendered partial pops up immediately and the resume socket continues
// from last_seq. On 404 (past grace TTL) we drop the pending state and
// settle the bubble with the incomplete notice; the long-poll will fill
// in the actual completed message_added when (and if) it lands.
async function attemptResumeOnLoad(sid: string): Promise<void> {
  const pending = store.pendingStreams[sid];
  if (!pending) return;
  if (sync.activeResumeAborts[sid]) return;
  await attachStreamingBubble({
    sid,
    kind: "resume",
    streamId: pending.stream_id,
    afterSeq: pending.last_seq,
    initialText: pending.pending_text,
  });
}

// Called on `stream_started` long-poll events for a session whose stream
// the local device did NOT POST (different tab or different device on
// the same token). When the session is currently open we attach with
// after_seq=-1 so we receive every chunk live. When it's not open the
// caller (applyEvent) just pins the stream_id into peerStreamsBySession
// so a later session-open triggers this same path.
async function attemptCrossDeviceLiveAttach(sid: string, streamId: string): Promise<void> {
  if (sync.activeResumeAborts[sid]) return;
  await attachStreamingBubble({
    sid,
    kind: "peer",
    streamId,
    afterSeq: -1,
  });
  // After the peer attach settles (success or failure), drop any stale
  // sidebar typing indicator. The stream_ended event is the authoritative
  // signal but covering this in the success-path keeps the indicator from
  // sticking if we lose the long-poll connection mid-stream.
  delete sync.peerStreamsBySession[sid];
  sync.sidebarTypingFor.delete(sid);
  renderSessionList();
}

function streamErrorCopy(code: string): string {
  // Soft outcomes (server is healthy, this turn just didn't complete) get
  // actionable copy that doesn't imply a service outage. Hard failures
  // keep the "稍后再试" hint.
  if (code === "llm_timeout") return "这次回复没有完整生成。可以重新发送；如果任务较大，请缩小范围或分步提问。";
  if (code === "empty_reply") return "上游模型这次没有输出内容（可能上下文过长或被过滤）。可换种说法或缩短问题后重新提问。";
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
    else if (s === 504 && err === "llm_timeout") addMessageBubble("notice", streamErrorCopy("llm_timeout"));
    else if (s === 502 && err === "empty_reply") addMessageBubble("notice", streamErrorCopy("empty_reply"));
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
  for (const k of [LS_TOKEN, LS_USERNAME, LS_STORE, LS_LAST_PTS, LS_PENDING_STREAMS]) localStorage.removeItem(k);
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

// Telegram-style auto-grow: the textarea expands as the user types and
// shrinks back when text is deleted. CSS min-height / max-height cap both
// ends; once scrollHeight exceeds max-height the browser falls back to
// internal scrolling. Setting `height = "auto"` first is needed to let
// scrollHeight collapse before re-measuring (otherwise it only grows,
// never shrinks).
function autosizeInput(): void {
  inputEl.style.height = "auto";
  inputEl.style.height = inputEl.scrollHeight + "px";
}
inputEl.addEventListener("input", autosizeInput);
// Reset to one line on initial render and any external value clear.
autosizeInput();
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
  // Recovery resume on boot. If the active session has a PendingStream
  // (from a prior session that was cut off mid-stream), reattach to it
  // before the long-poll spins up — the long-poll's eventual
  // message_added would otherwise race the resume to render the same
  // partial reply twice. attemptResumeOnLoad handles 404 (past grace TTL)
  // by settling the bubble with the incomplete notice; the message_added
  // event still gets to fill in the rest via the existing dedup path.
  if (!sync.stopped) {
    const activeId = store.activeId;
    if (activeId && store.pendingStreams[activeId]) {
      void attemptResumeOnLoad(activeId);
    }
  }
  if (!sync.stopped) void runLongPoll();
})();
