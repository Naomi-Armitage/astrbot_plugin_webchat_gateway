import {
  LS_TOKEN,
  LS_FAMILY,
  $,
  applySiteIcon,
  modeFromTheme,
  paintBrowserChrome,
  reloadIOSChromeOnce,
  resolveTheme,
  setupThemeToggle,
} from "../shared/site";
import type { SiteConfig } from "../shared/site";
import { installFocusTrap, type FocusTrap } from "../shared/focus-trap";
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
// Optimistic-echo dedup buffer. Persisted so a refresh fired between the
// local echo and the server's `message_added` doesn't drop the dedup entry
// and produce a duplicate bubble. Also lets ingestConversationDetail know
// which local-only tail entries are still "in flight" and must be preserved
// across a coldRefetch that arrives before the server has persisted them.
const LS_PENDING_LOCALS = "wcg.chat.pending_locals";
// Pending-delete dedup buffer. Same shape rationale as LS_PENDING_LOCALS but
// keyed on (sessionId, index, role) rather than content, because delete is
// the only place where two different messages can share identical text but
// must be tracked as separate optimistic actions.
const LS_PENDING_LOCAL_DELETES = "wcg.chat.pending_local_deletes";

const API = "/api/webchat";
const CHAT_URL = `${API}/chat`;
const CHAT_STREAM_URL = `${API}/chat/stream`;
const ME_URL = `${API}/me`;
const SITE_URL = `${API}/site`;
const TITLE_URL = `${API}/title`;
const CONV_URL = `${API}/conversations`;
const EVENTS_URL = `${API}/events`;
const UPLOAD_URL = `${API}/upload`;
const FILES_URL = `${API}/files`;
// Per-message action endpoints. `sid` and `index` are URL-segments — the
// server's path layout matches cfg.conversations_message_path and
// cfg.conversations_regenerate_path.
const messageItemUrl = (sid: string, index: number): string =>
  `${CONV_URL}/${encodeURIComponent(sid)}/messages/${index}`;
const regenerateUrl = (sid: string): string =>
  `${CONV_URL}/${encodeURIComponent(sid)}/regenerate`;

// Upload defaults — overridable from /api/webchat/site at boot so an
// operator who raises max_attachments_per_message on the server side
// gets a UI that respects the new cap without a frontend rebuild.
// `let` (not `const`) so loadChatSite() can swap them in.
let MAX_ATTACHMENTS_PER_MESSAGE = 4;
let MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024;
let ALLOWED_MIME: readonly string[] = ["image/jpeg", "image/png", "image/webp", "image/gif"];
let UPLOADS_ENABLED = true;
const RESIZE_TARGET_LONG_EDGE = 2048;
const RESIZE_JPEG_QUALITY = 0.85;
// Files already comfortably below the long-edge cap AND under this size get
// uploaded as-is (no re-encode). Saves a Canvas decode/encode pass for tiny
// screenshots / icons / thumbnails where the resize would be a no-op.
const RESIZE_SKIP_MAX_BYTES = 2 * 1024 * 1024;

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
// Server-issued reference to an uploaded file. `mime` is required after upload
// commit; width/height are reserved for future per-image grid hints.
interface AttachmentRef {
  file_id: string;
  mime: string;
  width?: number;
  height?: number;
}
// Per-composer pending attachment. Lives only in the in-memory queue between
// the user adding the file and the message being sent.
interface PendingAttachment {
  local_id: string;
  file_id?: string;
  mime: string;
  size: number;
  preview_url: string;
  state: "uploading" | "ready" | "error";
  error_message?: string;
}
interface HistoryItem {
  role: Role;
  text: string;
  ts: number;
  incomplete?: boolean;
  attachments?: AttachmentRef[];
  // Local-only failure marker for user-side turns that didn't go through
  // (cancelled before any chunk, network error, server rejected, etc).
  // We render a small status caption + retry/edit links under the bubble
  // and the entry is NOT promoted to the server CM (so the next LLM turn
  // doesn't see it). Cleared on successful retry/edit.
  failure?: { reason: FailureReason };
}
type FailureReason = "stopped" | "send_failed";
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
interface ServerMessage {
  role: ServerRole;
  content: string;
  ts?: number;
  incomplete?: boolean;
  attachments?: AttachmentRef[];
}
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
  | "message_deleted"
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
const attachBtn = $<HTMLButtonElement>("attachBtn");
const imageBtn = $<HTMLButtonElement>("imageBtn");
const fileInputEl = $<HTMLInputElement>("fileInput");
const composerAttachmentsEl = $("composer-attachments");
const dropOverlayEl = $("dropOverlay");
const footerEl = document.querySelector("footer") as HTMLElement;

const username = (localStorage.getItem(LS_USERNAME) || "Friend").trim() || "Friend";
const strong = document.createElement("strong");
strong.textContent = username;
whoEl.append("你好，", strong);
whoEl.hidden = false;

const newId = (): string => crypto.randomUUID?.() ?? `s-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
const blankSession = (id?: string): SessionMeta =>
  ({ id: id ?? newId(), title: "新会话", lastActiveAt: Date.now(), history: [] });
const nowSec = (): number => Math.floor(Date.now() / 1000);

function isAttachmentRef(it: unknown): it is AttachmentRef {
  if (!it || typeof it !== "object") return false;
  const o = it as Record<string, unknown>;
  if (typeof o.file_id !== "string" || !o.file_id) return false;
  if (typeof o.mime !== "string") return false;
  if (o.width !== undefined && typeof o.width !== "number") return false;
  if (o.height !== undefined && typeof o.height !== "number") return false;
  return true;
}
function isHistoryItem(it: unknown): it is HistoryItem {
  if (!it || typeof it !== "object") return false;
  const o = it as Record<string, unknown>;
  if (o.incomplete !== undefined && typeof o.incomplete !== "boolean") return false;
  if (o.attachments !== undefined) {
    if (!Array.isArray(o.attachments)) return false;
    if (!o.attachments.every(isAttachmentRef)) return false;
  }
  if (o.failure !== undefined) {
    if (!o.failure || typeof o.failure !== "object") return false;
    const f = o.failure as Record<string, unknown>;
    if (f.reason !== "stopped" && f.reason !== "send_failed") return false;
  }
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
const localRoleToServer = (r: Role): ServerRole => r === "bot" ? "assistant" : "user";

function replayActive(): void {
  // Preserve scroll position when the user wasn't already pinned to
  // the bottom. Mid-history regenerate / mid-history delete used to
  // snap the viewport to the new bottom even when the user was
  // reading several screens up — disorienting. Threshold ~80 px
  // matches the "near bottom" feel used elsewhere on the page.
  const prevScrollTop = msgs.scrollTop;
  const prevScrollHeight = msgs.scrollHeight;
  const prevClientHeight = msgs.clientHeight;
  const wasNearBottom =
    prevScrollHeight - (prevScrollTop + prevClientHeight) < 80;
  clearMsgList();
  const history = currentSession().history;
  for (const item of history) {
    addMessageBubble(item.role, item.text, item.attachments, item.failure);
    if (item.role === "bot" && item.incomplete) appendIncompleteNoticeToLastBubble();
  }
  if (wasNearBottom) {
    scrollToEnd();
  } else {
    // Re-anchor on the pre-render scroll position, biased by any
    // height change above the viewport so the same content stays
    // visually stable. If the new content is shorter than the old
    // scroll offset (e.g. a big tail was truncated), clamp at 0.
    const delta = msgs.scrollHeight - prevScrollHeight;
    msgs.scrollTop = Math.max(0, prevScrollTop + delta);
  }
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
      renderer(token) {
        // Pre-escape the captured text. DOMPurify is the real safety
        // net but layering the escape here keeps the mark extension
        // safe even if the sanitize config ever drifts.
        const text = (token as unknown as { text: string }).text;
        return `<mark>${escapeCodeHtml(text)}</mark>`;
      },
    },
  ],
});

// Telegram-style fenced code blocks: header (lang + copy-all button) +
// per-line clickable spans. The marked renderer only emits a SAFE
// sentinel — `<pre class="codeblock-raw" data-codeblock-lang="...">
// <code>{escaped code}</code></pre>` — which survives DOMPurify
// unchanged. `decorateCodeblocks` then walks the sanitized DOM and
// replaces each sentinel with the full chrome (`.codeblock` wrapper,
// header, real <button>, per-line <span>s). This keeps the trusted
// JS as the only source of buttons/data-action — a malicious assistant
// reply can't smuggle an actionable button through markdown because
// the renderMarkdown FORBID_TAGS/FORBID_ATTR config below also strips
// <button> and data-action from the rendered HTML.
function escapeCodeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
marked.use({
  renderer: {
    code({ text, lang }) {
      const langLabel = (lang || "").trim();
      const body = text.replace(/\n$/, "");
      const langAttr = langLabel
        ? ` data-codeblock-lang="${escapeCodeHtml(langLabel)}"`
        : "";
      return (
        `<pre class="codeblock-raw"${langAttr}>`
        + `<code>${escapeCodeHtml(body)}</code>`
        + `</pre>`
      );
    },
  },
});

function decorateCodeblocks(root: Element): void {
  const sentinels = root.querySelectorAll<HTMLElement>("pre.codeblock-raw");
  sentinels.forEach((pre) => {
    const codeEl = pre.querySelector("code");
    if (!codeEl) return;
    const text = codeEl.textContent ?? "";
    const lang = pre.dataset.codeblockLang ?? "";
    const wrapper = document.createElement("div");
    wrapper.className = "codeblock";
    const header = document.createElement("div");
    header.className = "codeblock-header";
    const langSpan = document.createElement("span");
    if (lang) {
      langSpan.className = "codeblock-lang";
      langSpan.textContent = lang;
    } else {
      langSpan.className = "codeblock-lang codeblock-lang-empty";
    }
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "codeblock-copy";
    copyBtn.setAttribute("aria-label", "复制全部");
    copyBtn.textContent = "复制";
    header.appendChild(langSpan);
    header.appendChild(copyBtn);
    const newPre = document.createElement("pre");
    newPre.className = "codeblock-pre";
    const newCode = document.createElement("code");
    newCode.className = "codeblock-code";
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i]!;
      const span = document.createElement("span");
      span.className = "codeblock-line";
      span.textContent = line.length > 0 ? line : "​";
      newCode.appendChild(span);
      if (i < lines.length - 1) newCode.appendChild(document.createTextNode("\n"));
    }
    newPre.appendChild(newCode);
    wrapper.appendChild(header);
    wrapper.appendChild(newPre);
    pre.replaceWith(wrapper);
  });
}

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
    // FORBID_TAGS includes `button` and FORBID_ATTR includes
    // `data-action` so a malicious / hallucinated assistant reply can't
    // smuggle a clickable `<button class="msg-action-btn"
    // data-action="delete">` past sanitize — DOMPurify's default
    // allow-list permits both, and our delegated handler dispatches on
    // those exact selectors. Our trusted JS-built buttons (codeblock
    // copy, per-message actions) are constructed via createElement
    // after sanitize and bypass this filter entirely.
    FORBID_TAGS: ["style", "script", "iframe", "object", "embed", "form", "button"],
    FORBID_ATTR: ["formaction", "data-action"],
  });
}

// Code-block interactions: one delegated listener handles every fenced
// block under the message list. We can't use inline `onclick` because
// DOMPurify strips event-handler attributes during sanitize.
// navigator.clipboard requires a secure context (HTTPS or localhost), so
// plain-HTTP intranet deployments fall back to a hidden textarea +
// document.execCommand. Returns true on success so callers can branch on
// visual feedback (button flash, line flash).
async function writeClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return true; } catch {}
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
function gatherBlockText(block: Element): string {
  const lineEls = block.querySelectorAll<HTMLElement>(".codeblock-line");
  const lines: string[] = [];
  lineEls.forEach((el) => { lines.push(el.textContent ?? ""); });
  return lines.join("\n");
}
msgs.addEventListener("click", (e: MouseEvent) => {
  const target = e.target as Element | null;
  if (!target) return;
  const copyBtn = target.closest<HTMLButtonElement>(".codeblock-copy");
  if (copyBtn) {
    const block = copyBtn.closest(".codeblock");
    if (!block) return;
    void writeClipboard(gatherBlockText(block)).then((ok) => {
      if (!ok) return;
      const orig = copyBtn.textContent ?? "复制";
      copyBtn.textContent = "已复制";
      copyBtn.classList.add("copied");
      window.setTimeout(() => {
        copyBtn.textContent = orig;
        copyBtn.classList.remove("copied");
      }, 1200);
    });
    return;
  }
  const line = target.closest<HTMLElement>(".codeblock-line");
  if (line) {
    void writeClipboard(line.textContent ?? "").then((ok) => {
      if (!ok) return;
      line.classList.add("copied");
      window.setTimeout(() => { line.classList.remove("copied"); }, 600);
    });
    return;
  }
  // Per-message action buttons. Each carries a data-action of
  // "copy" / "delete" / "regen" / "edit" / "regen-user"; we dispatch
  // via the closest .msg-row → .msg bubble. Replaces three closure-
  // bound per-button listeners per bubble with a single delegated
  // handler.
  const actionBtn = target.closest<HTMLButtonElement>(".msg-action-btn");
  if (!actionBtn) return;
  const row = actionBtn.closest<HTMLElement>(".msg-row");
  const bubble = row?.querySelector<HTMLDivElement>(".msg");
  if (!bubble) return;
  switch (actionBtn.dataset.action) {
    case "copy": {
      const idx = indexOfRenderedBubble(bubble);
      const sess = currentSession();
      const text = idx >= 0 && sess.history[idx]
        ? sess.history[idx]!.text
        : bubble.textContent ?? "";
      void copyMessage(text, actionBtn);
      return;
    }
    case "delete":
      void deleteMessage(bubble);
      return;
    case "regen":
      void regenerateMessage(bubble);
      return;
    case "edit":
      void editUserMessage(bubble);
      return;
    case "regen-user":
      void regenerateUserMessage(bubble);
      return;
  }
});

// Long-press to reveal action chrome on touch devices. Desktop already
// uses :hover; mobile has no hover state, so without this the
// delete/regen buttons effectively don't exist on phones. 350ms hold
// + 10px move tolerance matches Telegram's bubble-menu interaction.
// A pointerdown outside any revealed row closes it (tap-out dismiss).
const LONG_PRESS_MS = 350;
const LONG_PRESS_MOVE_TOL_SQ = 100; // 10px squared
let lpTimer: number | null = null;
let lpStartXY: [number, number] | null = null;
let lpRow: HTMLElement | null = null;
function cancelLongPress(): void {
  if (lpTimer !== null) { window.clearTimeout(lpTimer); lpTimer = null; }
  lpStartXY = null;
  lpRow = null;
}
function closeAllRevealedActions(except?: Element | null): void {
  msgs.querySelectorAll<HTMLElement>(".msg-row.actions-revealed").forEach((r) => {
    if (r !== except) r.classList.remove("actions-revealed");
  });
}
msgs.addEventListener("touchstart", (e: TouchEvent) => {
  // Don't treat a tap on the action buttons themselves as a long-press
  // candidate — that would re-trigger reveal on a row that's already
  // open and feel laggy.
  const target = e.target as Element | null;
  if (!target || target.closest(".msg-action-btn")) return;
  const row = target.closest<HTMLElement>(".msg-row");
  if (!row) return;
  // Streaming bubble: actions are hidden via CSS while .streaming is
  // present, but if we set actions-revealed during streaming the class
  // would still be set after streaming finishes and actions would pop
  // visible without the user re-pressing. Skip long-press on streaming.
  if (row.querySelector(".msg.streaming")) return;
  const t = e.touches[0];
  if (!t) return;
  lpRow = row;
  lpStartXY = [t.clientX, t.clientY];
  lpTimer = window.setTimeout(() => {
    if (lpRow) {
      closeAllRevealedActions(lpRow);
      lpRow.classList.add("actions-revealed");
    }
    lpTimer = null;
  }, LONG_PRESS_MS);
}, { passive: true });
msgs.addEventListener("touchmove", (e: TouchEvent) => {
  if (lpTimer === null || !lpStartXY) return;
  const t = e.touches[0];
  if (!t) return;
  const dx = t.clientX - lpStartXY[0];
  const dy = t.clientY - lpStartXY[1];
  if (dx * dx + dy * dy > LONG_PRESS_MOVE_TOL_SQ) cancelLongPress();
}, { passive: true });
msgs.addEventListener("touchend", cancelLongPress, { passive: true });
msgs.addEventListener("touchcancel", cancelLongPress, { passive: true });
document.addEventListener("pointerdown", (e: PointerEvent) => {
  // Fast path: no revealed rows means nothing to close. Skips the
  // closest() walk on every pointerdown when long-press isn't active
  // (the common case on desktop).
  if (!msgs.querySelector(".msg-row.actions-revealed")) return;
  const target = e.target as Element | null;
  if (!target) return;
  // A pointerdown inside a revealed row's actions is a button press —
  // let the click resolve, don't pre-emptively close. Anywhere else
  // dismisses all revealed rows.
  if (target.closest(".msg-action-btn")) return;
  const insideRevealed = target.closest(".msg-row.actions-revealed");
  closeAllRevealedActions(insideRevealed);
}, { passive: true });

// Append a chat bubble. `text` is the raw content as stored in history;
// for the bot role we render markdown (sanitized), everything else stays
// plain text (textContent) — user input must never be HTML-rendered
// because it's untrusted input echoed back to the same DOM. User bubbles
// may also include an image grid (1..4 attachments) rendered above the
// text; clicking any thumbnail opens a lightbox with carousel.
//
// Return contract: the inner `<div class="msg">` element. Callers (the
// streaming pipeline, applyEvent, ingestConversationDetail, etc.) hold
// the returned reference to mutate the bubble in place (innerHTML swap,
// classList tweaks, scrolling). The hover-action row + the user-failure
// chrome are appended as separate siblings inside an outer row wrapper;
// they MUST NOT change the returned identity.
function addMessageBubble(
  role: Role,
  text: string,
  attachments?: AttachmentRef[],
  failure?: { reason: FailureReason },
): HTMLDivElement {
  hideTyping();
  const div = document.createElement("div");
  div.className = "msg " + role;
  // Attachments render for BOTH user and bot bubbles. User-side
  // covers image uploads; bot-side covers /image-command generations
  // returned via the JSON `attachments` field. The previous
  // `role === "user"` guard dropped the assistant's generated image
  // on the floor, so the bubble showed "[已生成 1 张图片]" with no
  // image — which is exactly what the operator reported.
  const hasImages = (role === "user" || role === "bot")
    && Array.isArray(attachments)
    && attachments.length > 0;
  // Telegram-style image bubble classification:
  //   has-image       — image + text. Image flushes to the bubble's
  //                     top + sides; text continues below with the
  //                     bubble's normal padding.
  //   has-image-only  — image, no text. Drop the bubble chrome
  //                     entirely (no background, no border, no
  //                     padding); the image floats naked at the
  //                     msg-row's edge alignment.
  // Text emptiness is the discriminator. /image command generations
  // come through with text="" (the image IS the reply), so they
  // become has-image-only automatically.
  if (hasImages) {
    div.classList.add("has-image");
    if (!text) div.classList.add("has-image-only");
  }
  if (hasImages) {
    const list = attachments as AttachmentRef[];
    const count = Math.min(list.length, MAX_ATTACHMENTS_PER_MESSAGE);
    const grid = document.createElement("div");
    grid.className = "msg-attachments cnt-" + count;
    for (let i = 0; i < count; i++) {
      const a = list[i]!;
      const img = document.createElement("img");
      img.className = "msg-image";
      img.loading = "lazy";
      img.alt = "";
      img.src = fileServeUrl(a.file_id);
      attachImgErrorRetry(img);
      const captureIdx = i;
      img.addEventListener("click", () => openLightbox(list, captureIdx));
      grid.appendChild(img);
    }
    div.appendChild(grid);
  }
  if (role === "bot") {
    div.classList.add("md");
    if (text) {
      if (hasImages) {
        // Image grid was just appended above; setting innerHTML on
        // the bubble itself would wipe it out. Render markdown into
        // a child wrapper instead so both render side-by-side
        // (image first, then text — same ordering as the user-side
        // hasImages branch below).
        const md = document.createElement("div");
        md.className = "msg-text md";
        md.innerHTML = renderMarkdown(text);
        decorateCodeblocks(md);
        div.appendChild(md);
      } else {
        div.innerHTML = renderMarkdown(text);
        decorateCodeblocks(div);
      }
    }
  } else if (text) {
    if (hasImages) {
      const span = document.createElement("div");
      span.className = "msg-text";
      span.textContent = text;
      div.appendChild(span);
    } else {
      div.textContent = text;
    }
  }
  if (role === "user" && failure) {
    applyUserFailureChrome(div, text, attachments);
  }
  // Wrap user/bot bubbles in a row container so the hover-action chrome
  // (.msg-actions) can sit as a sibling next to the bubble without
  // breaking the bubble's existing align-self anchoring. error/notice
  // are full-width and explicitly don't get actions — they're
  // client-side statuses, not server-tracked messages, so delete/copy
  // semantics don't apply.
  if (role === "user" || role === "bot") {
    const row = document.createElement("div");
    row.className = "msg-row " + role + "-row";
    // If failure chrome wrapped the bubble already, lift the wrapper
    // into the row instead of the bare bubble; otherwise put the
    // bubble directly into the row.
    const wrapper = div.parentElement && div.parentElement.classList.contains("msg-user-failed")
      ? div.parentElement
      : null;
    if (wrapper) {
      // applyUserFailureChrome appended a sibling .msg-edit-icon after
      // the wrapper. Move both into the row so they stay glued.
      const editIcon = wrapper.nextElementSibling;
      row.appendChild(wrapper);
      if (editIcon && (editIcon as HTMLElement).classList?.contains("msg-edit-icon")) {
        row.appendChild(editIcon);
      }
    } else {
      row.appendChild(div);
    }
    row.appendChild(buildMessageActions(role, div));
    msgs.appendChild(row);
  } else {
    msgs.appendChild(div);
  }
  scrollToEnd();
  return div;
}

// Build the hover-revealed action row that sits next to a user / bot bubble.
// Buttons:
//   - 复制 — both roles; hover-revealed
//   - 删除 — both roles; hover-revealed
//   - 重新生成 — both roles; hover-revealed (semantics differ — see below)
//   - 编辑 — user only; hover-revealed
// All buttons start at opacity:0 (see .msg-action-btn in styles.css). The
// whole cluster reveals together on .msg-row:hover / :focus-within so the
// user-row's right-aligned cluster stays symmetric with the bot-row's
// left-aligned one (an always-visible copy left a phantom slot beside it).
//
// Bot 重新生成 (data-action=regen): destructive — drops everything from
// the target onward and runs a new LLM turn on the truncated history.
// Existing behavior, unchanged.
//
// User 再问一次 (data-action=regen-user): non-destructive — dispatches
// the original user text + attachments as a NEW turn at the bottom of
// the conversation via the regular `/chat/stream` path. The LLM sees
// the full current context (including any intervening turns) so the
// same question can yield a different answer than the original.
// The original bubble stays untouched.
//
// 编辑 (data-action=edit): loads the original text into the composer
// for the user to rewrite + manually send. Same destination — a new
// turn at the bottom — just with text the user could change first.
// Both flows use the existing send path; no special server endpoint.
//
// Click handlers look up the bubble's current position in the rendered
// history at click time (via indexOfRenderedBubble) so a delete that's
// preceded by other deletes / inserts doesn't desync the index.
function buildMessageActions(role: "user" | "bot", _bubble: HTMLDivElement): HTMLDivElement {
  const actions = document.createElement("div");
  actions.className = "msg-actions";
  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "msg-action-btn msg-action-copy";
  copyBtn.setAttribute("aria-label", "复制消息");
  copyBtn.title = "复制";
  copyBtn.dataset.action = "copy";
  copyBtn.innerHTML = ICON_COPY;
  actions.appendChild(copyBtn);

  const delBtn = document.createElement("button");
  delBtn.type = "button";
  delBtn.className = "msg-action-btn msg-action-delete";
  delBtn.setAttribute("aria-label", "删除消息");
  delBtn.title = "删除";
  delBtn.dataset.action = "delete";
  delBtn.innerHTML = ICON_TRASH;
  actions.appendChild(delBtn);

  if (role === "bot") {
    const regenBtn = document.createElement("button");
    regenBtn.type = "button";
    regenBtn.className = "msg-action-btn msg-action-regen";
    regenBtn.setAttribute("aria-label", "重新生成");
    regenBtn.title = "重新生成";
    regenBtn.dataset.action = "regen";
    regenBtn.innerHTML = ICON_REGEN;
    actions.appendChild(regenBtn);
  } else {
    // User-side 再问一次 + 编辑. Both append a new turn at the bottom
    // of the conversation (via the regular /chat/stream path) using
    // the current full context. The original bubble is never modified.
    const regenUserBtn = document.createElement("button");
    regenUserBtn.type = "button";
    regenUserBtn.className = "msg-action-btn msg-action-regen";
    regenUserBtn.setAttribute("aria-label", "再问一次");
    regenUserBtn.title = "再问一次";
    regenUserBtn.dataset.action = "regen-user";
    regenUserBtn.innerHTML = ICON_REGEN;
    actions.appendChild(regenUserBtn);

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "msg-action-btn msg-action-edit";
    editBtn.setAttribute("aria-label", "编辑消息");
    editBtn.title = "编辑";
    editBtn.dataset.action = "edit";
    editBtn.innerHTML = ICON_PENCIL;
    actions.appendChild(editBtn);
  }
  return actions;
}

// Telegram-style red "!" badge on the LEFT of the bubble (click =
// resend) plus a small ChatGPT-style pencil icon BELOW the bubble
// (click = load into composer for edit). No status text — the badge's
// color + symbol already signals failure unambiguously and the user
// asked to keep the chrome minimal.
//
// We wrap the bubble in a flex row so the badge can sit beside it
// while the row stays right-aligned overall; the edit icon is a
// separate sibling under the row so it lines up with the bubble's
// trailing edge.
function applyUserFailureChrome(
  bubble: HTMLDivElement,
  text: string,
  attachments: AttachmentRef[] | undefined,
): void {
  // Idempotent: skip if already wrapped from an earlier mark/render.
  if (bubble.parentElement && bubble.parentElement.classList.contains("msg-user-failed")) {
    return;
  }
  // parent here is either the .msg-row (post-addMessageBubble) or the
  // raw message list (when called from addMessageBubble itself, before
  // the row wrap happens). Both cases work: insertBefore writes the
  // wrapper into the same parent the bubble currently sits in, and
  // addMessageBubble's lift-into-row code picks up the wrapper either
  // way via `div.parentElement.classList.contains("msg-user-failed")`.
  const parent = bubble.parentElement;
  const wrapper = document.createElement("div");
  wrapper.className = "msg-user-failed";
  if (parent) parent.insertBefore(wrapper, bubble);
  const badge = document.createElement("button");
  badge.type = "button";
  badge.className = "msg-retry-badge";
  badge.setAttribute("aria-label", "重试发送");
  badge.title = "重试";
  badge.innerHTML = ICON_BADGE_REFRESH;
  badge.addEventListener("click", () => retryFailedUserMessage(bubble, text, attachments));
  wrapper.appendChild(badge);
  wrapper.appendChild(bubble);
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "msg-edit-icon";
  edit.setAttribute("aria-label", "编辑消息");
  edit.title = "编辑";
  edit.innerHTML = ICON_PENCIL;
  edit.addEventListener("click", () => editFailedUserMessage(bubble, text, attachments));
  if (parent) parent.insertBefore(edit, wrapper.nextSibling);
}

// Inline SVGs kept local so the rest of the file stays the single
// source of truth for the chat UI. Both icons are sized via CSS
// width/height on the <svg>; viewBox is intrinsic so they scale
// crisply at any DPI.
//
// Badge: single circular arrow (Lucide RotateCw). One arrowhead
// reads as "redo / refresh" unambiguously; a two-arrow ring
// (RefreshCw) has both ends meeting nose-to-nose and adds no
// information, just visual clutter.
const ICON_BADGE_REFRESH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"`
  + ` stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">`
  + `<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/>`
  + `<path d="M21 3v5h-5"/>`
  + `</svg>`;
// Pencil: heavier-stroke outline (Lucide Pencil at stroke-width 2.5)
// to read as "chubby hollow pencil" per the design call. Hollow =
// no fill; chubby = thicker strokes than the default 1.5-2 Feather
// weight used elsewhere on the page. Still part of the same
// outline family so it doesn't clash visually.
const ICON_PENCIL = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"`
  + ` stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">`
  + `<path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>`
  + `<path d="m15 5 4 4"/>`
  + `</svg>`;
// Per-message action icons. Sized via CSS .msg-action-btn svg.
//   * copy: stacked-cards "copy to clipboard" pictogram (Lucide Copy)
//   * trash: outline can with two vertical bars (Lucide Trash2)
//   * regen: circular arrow + small spark hint to read as "redo" without
//     visually colliding with the failure-state retry badge (which uses
//     the same RotateCw pictogram but with red coloring)
const ICON_COPY = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"`
  + ` stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">`
  + `<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>`
  + `<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>`
  + `</svg>`;
const ICON_TRASH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"`
  + ` stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">`
  + `<path d="M3 6h18"/>`
  + `<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>`
  + `<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>`
  + `<path d="M10 11v6"/>`
  + `<path d="M14 11v6"/>`
  + `</svg>`;
const ICON_REGEN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"`
  + ` stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">`
  + `<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/>`
  + `<path d="M21 3v5h-5"/>`
  + `</svg>`;

// Remove a failed user bubble plus the chrome we wrapped it in
// (parent wrapper + the trailing edit-icon sibling). Used by retry
// and edit so the "three-DOM-nodes-act-as-one" detail lives in a
// single place.
function removeUserBubbleAndFailureChrome(bubble: HTMLDivElement): void {
  // Newer DOM: bubble lives inside a .msg-row (added by addMessageBubble).
  // Removing the row also removes the wrapper, edit-icon, and the
  // .msg-actions sibling in one shot.
  const row = bubble.closest<HTMLElement>(".msg-row");
  if (row) { row.remove(); return; }
  const wrapper = bubble.closest<HTMLElement>(".msg-user-failed");
  if (wrapper) {
    const edit = wrapper.nextElementSibling;
    if (edit && (edit as HTMLElement).classList?.contains("msg-edit-icon")) {
      edit.remove();
    }
    wrapper.remove();
    return;
  }
  // Defensive: not wrapped (shouldn't happen for failed entries, but
  // a transient race could land here). Remove the bare bubble.
  bubble.remove();
}

// Remove a streaming / regenerate bubble whose parent might be a
// .msg-row (post-refactor) or msgs itself (defensive, in case an
// old code path skipped the wrap). Idempotent — safe to call twice.
function removeBubbleWithRow(bubble: HTMLElement): void {
  const row = bubble.closest<HTMLElement>(".msg-row");
  (row ?? bubble).remove();
}

// Remove a failed user history entry by matching on (text, ts? — actually
// we don't carry ts onto the DOM, so we match on text+attachments+
// failure). Returns true if found and removed. Both retry and edit need
// this so a single failed turn doesn't sprout duplicates.
function removeFailedHistoryEntry(text: string, attachments: AttachmentRef[] | undefined): void {
  const sess = currentSession();
  const aKey = attachmentsKeyFromRefs(attachments);
  for (let i = sess.history.length - 1; i >= 0; i--) {
    const h = sess.history[i]!;
    if (h.role !== "user" || !h.failure) continue;
    if (h.text !== text) continue;
    if (attachmentsKeyFromRefs(h.attachments) !== aKey) continue;
    sess.history.splice(i, 1);
    sess.lastActiveAt = Date.now();
    saveStore();
    return;
  }
}

function retryFailedUserMessage(
  bubble: HTMLDivElement,
  text: string,
  attachments: AttachmentRef[] | undefined,
): void {
  // While a stream is in flight retry would race the live POST; we'd
  // either 429 concurrent_request or duplicate the user echo. Silently
  // ignore — the failure caption stays put, user can retry once the
  // current turn settles.
  if (sync.streamAbort) return;
  removeUserBubbleAndFailureChrome(bubble);
  removeFailedHistoryEntry(text, attachments);
  void performSend(text, attachments ? [...attachments] : []);
}

function editFailedUserMessage(
  bubble: HTMLDivElement,
  text: string,
  attachments: AttachmentRef[] | undefined,
): void {
  // Don't clobber whatever the user is currently typing — if they've
  // started a new message in the composer, opening edit would lose it.
  // Confirm first; cancelling leaves the failed bubble in place.
  const draft = inputEl.value.trim();
  if (draft && draft !== text) {
    if (!confirm("当前输入框有未发送的内容，编辑会覆盖。是否继续？")) return;
  }
  removeUserBubbleAndFailureChrome(bubble);
  removeFailedHistoryEntry(text, attachments);
  // Image attachments don't restore into the composer chip row — the
  // local blob URLs are gone, and reconstructing chips from file_ids
  // would need a fetch round-trip for the previews. The original
  // file_ids ARE still uploaded server-side, so the simpler UX is:
  // edit only re-loads text. If the user wants to resend with images,
  // they should use 重试 instead. Surface a one-line hint when this
  // matters so it's not silent.
  inputEl.value = text;
  autosizeInput();
  inputEl.focus();
  // Move caret to end so the user can keep typing.
  try { inputEl.setSelectionRange(text.length, text.length); } catch {}
  if (attachments && attachments.length) {
    addMessageBubble("notice", "已加载文字到输入框；如需保留图片请点「重试」。");
  }
  updateSendButtonState();
}

// Find the last optimistic user history item matching (text, attachments)
// and mark it failed. Re-decorates the matching DOM bubble by wrapping
// it with the failure chrome (badge + edit-icon). Called by the
// streaming POST error branches when the failure happens before any
// chunk lands (close_failed-side outcomes).
function markLastUserAsFailed(
  text: string,
  attachments: AttachmentRef[] | undefined,
  reason: FailureReason,
): void {
  const sess = currentSession();
  const aKey = attachmentsKeyFromRefs(attachments);
  for (let i = sess.history.length - 1; i >= 0; i--) {
    const h = sess.history[i]!;
    if (h.role !== "user") continue;
    if (h.text !== text) continue;
    if (attachmentsKeyFromRefs(h.attachments) !== aKey) continue;
    if (h.failure) return;  // already marked
    h.failure = { reason };
    saveStore();
    // Walk back through user bubbles in the DOM. The match is the most
    // recent one NOT already wrapped by .msg-user-failed — wrappers
    // belong to earlier failed turns.
    const list = msgs.querySelectorAll<HTMLDivElement>(".msg.user");
    for (let k = list.length - 1; k >= 0; k--) {
      const el = list[k]!;
      const wrapped = el.parentElement && el.parentElement.classList.contains("msg-user-failed");
      if (wrapped) continue;
      const tnode = el.querySelector(".msg-text");
      const elText = tnode ? tnode.textContent ?? "" : el.textContent ?? "";
      if (elText !== text) continue;
      applyUserFailureChrome(el, text, attachments);
      break;
    }
    return;
  }
}

const scrollToEnd = (): void => { msgs.scrollTop = msgs.scrollHeight; };
const clearMsgList = (): void => {
  // Wipe ALL chat-list children, not just .msg nodes — failed user
  // turns wrap the bubble in .msg-user-failed and append a sibling
  // .msg-edit-icon; matching only .msg would leave those orphans
  // behind on replayActive / history_cleared.
  msgs.replaceChildren();
  // Any pending long-press refers to a row we just detached. Drop the
  // module-level state so the 350ms timer doesn't fire against a
  // ghost node.
  cancelLongPress();
};

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
  // stream_id of the active POST stream, captured on the first SSE frame
  // via onStreamId. Lets the stop button POST /chat/stream/{id}/cancel so
  // the server-side LLM iteration actually terminates — without this the
  // client-side abort only tears down the live viewer while the server
  // keeps generating (see handlers/chat.py:"Client-disconnect semantics").
  streamAbortId: string | null;
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
  // Per-session handoff flag set by applyEvent when a message_added
  // (assistant) lands while a streaming bubble for that session is
  // still in the DOM. Causes the matching attachStreamingBubble's
  // finalize to drop its bubble + skip the history push regardless
  // of finalizeBubble's race-guard window. Targets the
  // "user backgrounded the tab → SSE done frame throttled → long-poll
  // wins the race after returning" path, where the event's
  // server-stamped ts can be more than 30s old by the time finalize
  // runs and the existing time-window check would miss.
  streamFinalizeSuppressed: Record<string, true>;
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
  streamAbortId: null,
  streamFailedAt: [],
  streamSkipRemaining: 0,
  activeResumeAborts: {},
  peerStreamsBySession: {},
  sidebarTypingFor: new Set(),
  streamFinalizeSuppressed: {},
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
  attachmentsKey: string;            // sorted "|"-joined file_ids ("" for text-only)
  recordedAtSec: number;            // local clock when we rendered it; matches against event ts
  expiresAt: number;                 // ms
}
const PENDING_TTL_MS = 660_000;

function loadPendingLocals(): PendingLocal[] {
  let parsed: unknown = null;
  try { parsed = JSON.parse(localStorage.getItem(LS_PENDING_LOCALS) || "null"); } catch {}
  if (!Array.isArray(parsed)) return [];
  const now = Date.now();
  const out: PendingLocal[] = [];
  for (const v of parsed) {
    if (!v || typeof v !== "object") continue;
    const o = v as Record<string, unknown>;
    if (typeof o.sessionId !== "string") continue;
    if (o.role !== "user" && o.role !== "assistant") continue;
    if (typeof o.contentKey !== "string") continue;
    if (typeof o.attachmentsKey !== "string") continue;
    if (typeof o.recordedAtSec !== "number") continue;
    if (typeof o.expiresAt !== "number") continue;
    if (o.expiresAt < now) continue;
    out.push({
      sessionId: o.sessionId,
      role: o.role as ServerRole,
      contentKey: o.contentKey,
      attachmentsKey: o.attachmentsKey,
      recordedAtSec: o.recordedAtSec,
      expiresAt: o.expiresAt,
    });
  }
  return out;
}

const pendingLocals: PendingLocal[] = loadPendingLocals();

function savePendingLocals(): void {
  try { localStorage.setItem(LS_PENDING_LOCALS, JSON.stringify(pendingLocals)); } catch {}
}

function prunePendingLocals(): void {
  const now = Date.now();
  let mutated = false;
  for (let i = pendingLocals.length - 1; i >= 0; i--) {
    if (pendingLocals[i]!.expiresAt < now) { pendingLocals.splice(i, 1); mutated = true; }
  }
  if (mutated) savePendingLocals();
}

function trimContent(s: string): string {
  return s.trim().slice(0, DEDUP_CONTENT_LEN);
}

function attachmentsKeyFromRefs(refs?: AttachmentRef[] | readonly AttachmentRef[] | null): string {
  if (!refs || !refs.length) return "";
  return refs.map((a) => a.file_id).sort().join("|");
}

function recordOptimistic(sessionId: string, role: ServerRole, content: string, attachments?: AttachmentRef[]): void {
  pendingLocals.push({
    sessionId,
    role,
    contentKey: trimContent(content),
    attachmentsKey: attachmentsKeyFromRefs(attachments),
    recordedAtSec: nowSec(),
    expiresAt: Date.now() + PENDING_TTL_MS,
  });
  savePendingLocals();
}

function consumeIfDuplicate(
  sessionId: string,
  role: ServerRole,
  content: string,
  eventTs: number,
  attachments?: AttachmentRef[],
): boolean {
  const now = Date.now();
  // Drop expired entries first so we don't hold stale matches forever.
  let mutated = false;
  for (let i = pendingLocals.length - 1; i >= 0; i--) {
    if (pendingLocals[i]!.expiresAt < now) { pendingLocals.splice(i, 1); mutated = true; }
  }
  const key = trimContent(content);
  const aKey = attachmentsKeyFromRefs(attachments);
  for (let i = 0; i < pendingLocals.length; i++) {
    const p = pendingLocals[i]!;
    if (p.sessionId !== sessionId || p.role !== role || p.contentKey !== key) continue;
    if (p.attachmentsKey !== aKey) continue;
    if (Math.abs(eventTs - p.recordedAtSec) > DEDUP_TS_WINDOW_S) continue;
    pendingLocals.splice(i, 1);
    savePendingLocals();
    return true;
  }
  if (mutated) savePendingLocals();
  return false;
}

// ---------- Pending-delete dedup buffer ----------
//
// Same intent as pendingLocals (drop the matching server event that comes
// back from our own optimistic action), but the match key is different:
// deletes don't care about content — two distinct messages with identical
// text are still distinct deletes — so we key on (sessionId, index, role)
// instead. Role conversion: the server emits "user"|"assistant" per spec
// (see EVENT_MESSAGE_DELETED in handlers/conversations.py); the buffer
// stores the wire form to make matching trivial.

interface PendingLocalDelete {
  sessionId: string;
  index: number;
  role: ServerRole;
  expiresAt: number;
}
// Short TTL is fine here — peer broadcast typically lands within a second
// or two; the longer LS_PENDING_LOCALS window exists to absorb LLM-pair
// emission delays, which don't apply to a delete. 60s leaves plenty of
// slack for a flaky long-poll without leaking stale entries forever.
const PENDING_DELETE_TTL_MS = 60_000;

function loadPendingLocalDeletes(): PendingLocalDelete[] {
  let parsed: unknown = null;
  try { parsed = JSON.parse(localStorage.getItem(LS_PENDING_LOCAL_DELETES) || "null"); } catch {}
  if (!Array.isArray(parsed)) return [];
  const now = Date.now();
  const out: PendingLocalDelete[] = [];
  for (const v of parsed) {
    if (!v || typeof v !== "object") continue;
    const o = v as Record<string, unknown>;
    if (typeof o.sessionId !== "string") continue;
    if (typeof o.index !== "number" || !Number.isInteger(o.index)) continue;
    if (o.role !== "user" && o.role !== "assistant") continue;
    if (typeof o.expiresAt !== "number") continue;
    if (o.expiresAt < now) continue;
    out.push({
      sessionId: o.sessionId,
      index: o.index,
      role: o.role as ServerRole,
      expiresAt: o.expiresAt,
    });
  }
  return out;
}

const pendingLocalDeletes: PendingLocalDelete[] = loadPendingLocalDeletes();

function savePendingLocalDeletes(): void {
  try {
    localStorage.setItem(LS_PENDING_LOCAL_DELETES, JSON.stringify(pendingLocalDeletes));
  } catch {}
}

function recordPendingDelete(sessionId: string, index: number, role: ServerRole): void {
  pendingLocalDeletes.push({
    sessionId,
    index,
    role,
    expiresAt: Date.now() + PENDING_DELETE_TTL_MS,
  });
  savePendingLocalDeletes();
}

function consumeIfDeleteDuplicate(sessionId: string, index: number, role: ServerRole): boolean {
  const now = Date.now();
  let mutated = false;
  for (let i = pendingLocalDeletes.length - 1; i >= 0; i--) {
    if (pendingLocalDeletes[i]!.expiresAt < now) { pendingLocalDeletes.splice(i, 1); mutated = true; }
  }
  for (let i = 0; i < pendingLocalDeletes.length; i++) {
    const p = pendingLocalDeletes[i]!;
    if (p.sessionId !== sessionId || p.index !== index || p.role !== role) continue;
    pendingLocalDeletes.splice(i, 1);
    savePendingLocalDeletes();
    return true;
  }
  if (mutated) savePendingLocalDeletes();
  return false;
}

// ---------- Per-message action handlers ----------
//
// All three handlers (copy / delete / regenerate) are wired via
// buildMessageActions and share these contracts:
//   * They always read the bubble's current rendered-history index at
//     click time (indexOfRenderedBubble). Caching it at button-build
//     time would desync after any preceding delete / insert.
//   * Network paths use fetchWithTimeout + bearer() + same-origin
//     credentials, matching runNonStreamingSend.
//   * 401 always routes through handle401; 429 concurrent_request is
//     surfaced as a soft notice (the user just needs to wait for the
//     in-flight chat to finish, not a hard error).
//   * On success they update store.sessions + replayActive (for delete)
//     or in-place DOM mutation (for regenerate's bubble swap).

// Find the index of `bubble` among rendered user/bot bubbles in the
// active message list. Matches the server's `message_index` because the
// server renders user + assistant messages in the same order we do.
// Returns -1 if not found (defensive — caller treats as no-op).
function indexOfRenderedBubble(bubble: HTMLDivElement): number {
  const list = msgs.querySelectorAll<HTMLDivElement>(".msg.user, .msg.bot");
  for (let i = 0; i < list.length; i++) {
    if (list[i] === bubble) return i;
  }
  return -1;
}

// Brief visual confirmation on the action button. Restores the original
// HTML after a short window so subsequent clicks find the icon again.
function flashActionButton(btn: HTMLButtonElement): void {
  btn.classList.add("copied");
  window.setTimeout(() => { btn.classList.remove("copied"); }, 1200);
}

async function copyMessage(text: string, btn: HTMLButtonElement): Promise<void> {
  if (!text) return;
  if (await writeClipboard(text)) flashActionButton(btn);
}

async function deleteMessage(bubble: HTMLDivElement): Promise<void> {
  const idx = indexOfRenderedBubble(bubble);
  if (idx < 0) return;
  const sess = currentSession();
  if (!sess.history[idx]) return;
  const sid = sess.id;
  const role = sess.history[idx]!.role;
  if (role !== "user" && role !== "bot") return;
  const wireRole: ServerRole = localRoleToServer(role);
  if (!confirm("删除该消息？")) return;
  let resp: Response;
  try {
    resp = await fetchWithTimeout(
      messageItemUrl(sid, idx),
      { method: "DELETE", credentials: "same-origin", headers: bearer() },
      FETCH_TIMEOUT_FAST_MS,
    );
  } catch {
    addMessageBubble("error", "删除失败，请检查网络。");
    return;
  }
  let payload: Record<string, unknown> = {};
  try { payload = await resp.json(); } catch {}
  if (resp.status === 401) { handle401(); return; }
  if (resp.status === 429 && (payload.error as string) === "concurrent_request") {
    addMessageBubble("notice", "正在处理中，稍候。");
    return;
  }
  if (resp.status === 429 && (payload.error as string) === "ip_blocked") {
    const retry = resp.headers.get("Retry-After") || payload.retry_after || "?";
    addMessageBubble("error", `请求过于频繁，已暂时封禁，${retry} 秒后重试。`);
    return;
  }
  if (resp.status === 403 && (payload.error as string) === "forbidden_origin") {
    addMessageBubble("error", "页面来源未在 allowed_origins 中。");
    return;
  }
  if (resp.status === 404) {
    // The message is already gone on the server (peer device beat us).
    // Splice locally + replay so the UI matches what the server believes.
    const sNow = store.sessions[sid];
    if (sNow && sNow.history[idx]) {
      sNow.history.splice(idx, 1);
      sNow.lastActiveAt = Date.now();
      saveStore();
      if (sid === store.activeId) replayActive();
      renderSessionList();
    }
    return;
  }
  if (!resp.ok) {
    addMessageBubble("error", `删除失败 (${resp.status})。`);
    return;
  }
  // Optimistic local splice + dedup record. We splice BEFORE the server's
  // message_deleted event lands (it'll come back through long-poll and
  // get dropped by consumeIfDeleteDuplicate). replayActive re-renders
  // the whole list so all subsequent message_index references stay
  // aligned with the new history; this is the same pattern as
  // history_cleared and is cheap relative to a single fetch round-trip.
  const sNow = store.sessions[sid];
  if (sNow && sNow.history[idx]) {
    sNow.history.splice(idx, 1);
    sNow.lastActiveAt = Date.now();
    saveStore();
    if (sid === store.activeId) replayActive();
    renderSessionList();
  }
  recordPendingDelete(sid, idx, wireRole);
}

async function regenerateMessage(bubble: HTMLDivElement): Promise<void> {
  const idx = indexOfRenderedBubble(bubble);
  if (idx < 0) return;
  const sess = currentSession();
  const item = sess.history[idx];
  if (!item || item.role !== "bot") return;
  const sid = sess.id;
  // Race-guard: don't fire if any stream is already in flight for this
  // session (a /chat/stream send OR a previous regen). Setting
  // `sync.streamAbort` below makes a second regen click see this same
  // guard and bail.
  if (sync.streamAbort) {
    addMessageBubble("notice", "正在处理中，稍候。");
    return;
  }

  // Pre-record pending-delete for the target index so the long-poll's
  // own `message_deleted(idx, "assistant")` event (emitted server-side
  // when the regen commits) doesn't fire the splice handler against
  // the NEW bot we'll push at the same logical position after `done`.
  // For mid-history regen the server emits additional deletes for the
  // dropped tail; those target indices > idx that don't exist locally
  // after our pre-truncate, so the splice handler's missing-target
  // guard makes them safe no-ops.
  recordPendingDelete(sid, idx, "assistant");

  // Optimistic local truncate. Drops the old bot reply (+ any tail in
  // the mid-history case) so the streaming bubble lands at the same
  // logical position the old reply occupied. saveStore + replayActive
  // so a refresh mid-stream doesn't lose the truncate.
  const sBefore = store.sessions[sid];
  if (sBefore) {
    sBefore.history.length = idx;
    sBefore.lastActiveAt = Date.now();
    saveStore();
    if (sid === store.activeId) replayActive();
  }

  // Stop button + abort hookup, mirroring the /chat/stream POST path.
  // The action buttons in the bubble's row would naturally be disabled
  // once the bubble's removed, but we explicitly disable any held-over
  // refs (covers a second click sneaking in between the replayActive
  // and the bubble removal).
  const ac = new AbortController();
  sync.streamAbort = ac;
  setSendMode("stop");
  sendBtn.disabled = false;

  // Mount an empty streaming bubble at the tail of the message list.
  // Mirrors `attachStreamingBubble`'s shape (Text node + caret span +
  // .msg.bot.md.streaming class) so the race handler at applyEvent's
  // message_added(assistant) case finds it via `.msg.bot.streaming`
  // and the existing CSS hides the hover-action chrome until the
  // streaming class is removed.
  hideTyping();
  const newBubble = document.createElement("div") as HTMLDivElement;
  newBubble.className = "msg bot md streaming";
  const streamTextNode = document.createTextNode("");
  const caretSpan = document.createElement("span");
  caretSpan.className = "stream-caret";
  caretSpan.setAttribute("aria-hidden", "true");
  newBubble.appendChild(streamTextNode);
  newBubble.appendChild(caretSpan);
  const streamRow = document.createElement("div");
  streamRow.className = "msg-row bot-row";
  streamRow.appendChild(newBubble);
  streamRow.appendChild(buildMessageActions("bot", newBubble));
  msgs.appendChild(streamRow);
  scrollToEnd();

  const finishAbort = (): void => {
    if (sync.streamAbort === ac) sync.streamAbort = null;
    setSendMode("send");
  };
  const cleanupBubble = (): void => {
    // Bubble may already be gone (race handler dropped it on
    // message_added). Guarded removeBubbleWithRow is a no-op then.
    if (newBubble.parentElement) removeBubbleWithRow(newBubble);
  };

  let resp: Response;
  try {
    resp = await fetch(regenerateUrl(sid), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...bearer() },
      body: JSON.stringify({ message_index: idx }),
      signal: ac.signal,
    });
  } catch (e) {
    finishAbort();
    cleanupBubble();
    if ((e as { name?: string })?.name === "AbortError") return;
    addMessageBubble("error", "重新生成失败，请检查网络。");
    return;
  }

  if (resp.status === 401) { finishAbort(); cleanupBubble(); handle401(); return; }

  const ct = (resp.headers.get("Content-Type") || "").toLowerCase();
  if (!resp.ok || !ct.includes("text/event-stream") || !resp.body) {
    // Pre-handshake errors (auth / parse / 4xx routing) still come
    // back as plain JSON — match the prior handler's error copy.
    let payload: Record<string, unknown> = {};
    try { payload = await resp.json(); } catch {}
    finishAbort();
    cleanupBubble();
    const code = (payload.error as string) || `http_${resp.status}`;
    if (resp.status === 429 && code === "concurrent_request") {
      addMessageBubble("notice", "正在处理中，稍候。");
    } else if (resp.status === 429 && code === "quota_exceeded") {
      setBadge(0, payload.daily_quota as number);
      addMessageBubble("notice", "今日额度已用完，明日 0 点重置。");
    } else if (resp.status === 404) {
      addMessageBubble("error", "原消息已不存在。");
    } else if (resp.status === 502 || resp.status === 504) {
      addMessageBubble("error", streamErrorCopy(code));
    } else {
      addMessageBubble("error", `重新生成失败: ${code}`);
    }
    return;
  }

  // SSE consumer. Frames are `data: <json>\n\n`; the server emits
  // `{type:"chunk",delta}` / `{type:"done",...}` / `{type:"error",code}`.
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: false });
  let buffer = "";
  let accumulated = "";
  // Wrap done-state + error-code in an object so TS's CFA narrows
  // per-access on the property reads after the loop, instead of
  // collapsing the closure-mutated `let` binding to `never` after a
  // `!doneInfo` truthiness check (a known TS narrowing limitation
  // around closure assignment).
  const out: {
    done: { reply: string; remaining: number; daily_quota: number } | null;
    errorCode: string;
  } = { done: null, errorCode: "" };

  const handleFrame = (frame: string): void => {
    let jsonStr = "";
    for (const rawLine of frame.split("\n")) {
      const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("data:")) {
        const v = line.slice(5);
        jsonStr += v.startsWith(" ") ? v.slice(1) : v;
      }
    }
    if (!jsonStr) return;
    let obj: Record<string, unknown>;
    try { obj = JSON.parse(jsonStr) as Record<string, unknown>; } catch { return; }
    const t = obj.type;
    if (t === "chunk" && typeof obj.delta === "string") {
      accumulated += obj.delta;
      // Only update the DOM if the bubble's still mounted — the race
      // handler may have dropped it (long-poll's message_added beat us
      // to the punch on a backgrounded tab). Subsequent chunks still
      // accumulate so the final done event can run the dedup check.
      if (newBubble.parentElement) {
        streamTextNode.data = accumulated;
        scrollToEnd();
      }
    } else if (t === "done") {
      out.done = {
        reply: typeof obj.reply === "string" ? obj.reply : accumulated,
        remaining: typeof obj.remaining === "number" ? obj.remaining : 0,
        daily_quota: typeof obj.daily_quota === "number" ? obj.daily_quota : 0,
      };
    } else if (t === "error" && typeof obj.code === "string") {
      out.errorCode = obj.code;
    }
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 2);
        handleFrame(frame);
        if (out.done || out.errorCode) break;
      }
      if (out.done || out.errorCode) break;
    }
  } catch (e) {
    finishAbort();
    cleanupBubble();
    if ((e as { name?: string })?.name === "AbortError") return;
    addMessageBubble("error", "重新生成失败：连接被截断。");
    return;
  } finally {
    try { await reader.cancel(); } catch {}
  }

  finishAbort();

  if (out.errorCode) {
    cleanupBubble();
    if (out.errorCode === "concurrent_request") {
      addMessageBubble("notice", "正在处理中，稍候。");
    } else if (out.errorCode === "quota_exceeded") {
      addMessageBubble("notice", "今日额度已用完，明日 0 点重置。");
    } else if (out.errorCode === "llm_timeout" || out.errorCode === "empty_reply") {
      addMessageBubble("error", streamErrorCopy(out.errorCode));
    } else {
      addMessageBubble("error", `重新生成失败: ${out.errorCode}`);
    }
    return;
  }

  if (!out.done) {
    cleanupBubble();
    addMessageBubble("error", "重新生成失败：连接被截断。");
    return;
  }
  const finalDone = out.done;

  // Race protection: if long-poll's message_added arrived mid-stream
  // it dropped our streaming bubble and set streamFinalizeSuppressed —
  // the new bot has already been added to history and rendered. Just
  // refresh the badge and bail.
  if (sync.streamFinalizeSuppressed[sid]) {
    delete sync.streamFinalizeSuppressed[sid];
    setBadge(finalDone.remaining, finalDone.daily_quota);
    renderSessionList();
    return;
  }

  // Defensive secondary check: even if the suppression flag wasn't
  // set, the tail of history might already carry an identical bot
  // entry (added by a stray long-poll batch we didn't catch). Skip
  // the local push in that case so we don't end up with two copies.
  const sNow = store.sessions[sid];
  if (sNow) {
    const tail = sNow.history[sNow.history.length - 1];
    const alreadyAdded =
      tail &&
      tail.role === "bot" &&
      tail.text === finalDone.reply;
    if (alreadyAdded) {
      cleanupBubble();
      setBadge(finalDone.remaining, finalDone.daily_quota);
      renderSessionList();
      return;
    }
  }

  // Normal finalize. Strip the streaming chrome and swap the bubble
  // text for the markdown render. Push to history + record the dedup
  // entry so the upcoming long-poll `message_added` for this reply
  // gets consumed.
  newBubble.classList.remove("streaming");
  caretSpan.remove();
  newBubble.innerHTML = renderMarkdown(finalDone.reply);
  decorateCodeblocks(newBubble);

  if (sNow) {
    sNow.history.push({
      role: "bot",
      text: finalDone.reply,
      ts: Date.now(),
    });
    sNow.lastActiveAt = Date.now();
    saveStore();
  }
  setBadge(finalDone.remaining, finalDone.daily_quota);
  recordOptimistic(sid, "assistant", finalDone.reply);
  renderSessionList();
}

// "再问一次" — dispatch the original user text + attachments as a new
// turn at the bottom of the conversation. The original bubble stays
// untouched; the LLM sees the FULL current context (including every
// intervening turn) when it answers, so the same question can yield a
// different answer than the original. Intentionally distinct from the
// bot-side "重新生成" (`regenerateMessage`), which truncates and
// replaces the bot reply in place.
function regenerateUserMessage(bubble: HTMLDivElement): void {
  const idx = indexOfRenderedBubble(bubble);
  if (idx < 0) return;
  const sess = currentSession();
  const item = sess.history[idx];
  if (!item || item.role !== "user") return;
  if (sync.streamAbort) {
    addMessageBubble("notice", "正在处理中，稍候。");
    return;
  }
  void performSend(item.text, item.attachments ? [...item.attachments] : []);
}

// "编辑" — load the original text into the composer so the user can
// rewrite it and send manually. The original bubble stays in place;
// once the edited version is sent it appears as a new turn at the
// bottom with the new text and full current context (NOT a
// destructive in-place replacement of the original).
function editUserMessage(bubble: HTMLDivElement): void {
  const idx = indexOfRenderedBubble(bubble);
  if (idx < 0) return;
  const sess = currentSession();
  const item = sess.history[idx];
  if (!item || item.role !== "user") return;
  // Don't clobber the current draft silently. If the user has started
  // typing something different, ask first; cancelling leaves both the
  // bubble and the draft alone.
  const draft = inputEl.value.trim();
  if (draft && draft !== item.text) {
    if (!confirm("当前输入框有未发送的内容，编辑会覆盖。是否继续？")) return;
  }
  inputEl.value = item.text;
  autosizeInput();
  inputEl.focus();
  try { inputEl.setSelectionRange(item.text.length, item.text.length); } catch {}
  if (item.attachments && item.attachments.length) {
    addMessageBubble("notice", "已加载文字到输入框；图片附件不会自动还原。");
  }
  updateSendButtonState();
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
      let attachments: AttachmentRef[] | undefined;
      const rawAttachments = payload["attachments"];
      if (Array.isArray(rawAttachments) && rawAttachments.length) {
        const parsed: AttachmentRef[] = [];
        for (const entry of rawAttachments) {
          if (entry && typeof entry === "object") {
            const o = entry as Record<string, unknown>;
            if (typeof o.file_id === "string" && o.file_id) {
              const ref: AttachmentRef = {
                file_id: o.file_id,
                mime: typeof o.mime === "string" ? o.mime : "image/jpeg",
              };
              if (typeof o.width === "number") ref.width = o.width;
              if (typeof o.height === "number") ref.height = o.height;
              parsed.push(ref);
            }
          }
        }
        if (parsed.length) attachments = parsed;
      }
      // Dedup: if we already rendered this locally on this device, drop the event.
      if (consumeIfDuplicate(sid, role, content, ev.ts, attachments)) {
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
        // Backfill attachments onto the locally-recorded entry so a later
        // replay (e.g. session-switch) shows the image grid. Now covers
        // both roles — assistant attachments come from the /image
        // command's generated image; without this branch a refresh
        // would drop the image off the assistant bubble even though
        // the event log still has it.
        if (attachments && s) {
          const historyRole = role === "assistant" ? "bot" : "user";
          for (let i = s.history.length - 1; i >= 0; i--) {
            const h = s.history[i]!;
            if (h.role === historyRole && h.text === content && !h.attachments) {
              h.attachments = attachments;
              break;
            }
          }
        }
        break;
      }
      const localRole: Role = serverRoleToLocal(role as ServerRole);
      // Fallback dedup. The pendingLocals path above can miss in narrow
      // races (a stray double-delivery consumed the entry before the
      // legitimate event arrived; attachments key drifted between the
      // optimistic record and the server event; clock skew exceeded
      // DEDUP_TS_WINDOW_S). Without this guard, a missed match falls
      // through to the render+push below and produces a duplicate
      // bubble that survives only in DOM until the next replayActive.
      // Match the same role+content+attachments key against the tail
      // of sess.history and require the local entry's ts to be within
      // 60s of the event ts — narrow enough that a deliberate repeat
      // send minutes later still goes through, wide enough to absorb
      // typical clock skew + LLM-pair emission delay.
      const aKey = attachmentsKeyFromRefs(attachments);
      const fallbackSess = store.sessions[sid];
      if (fallbackSess) {
        const evTsMs = ev.ts * 1000;
        const tailStart = Math.max(0, fallbackSess.history.length - 4);
        let hit = false;
        for (let i = fallbackSess.history.length - 1; i >= tailStart; i--) {
          const h = fallbackSess.history[i]!;
          if (h.role !== localRole) continue;
          if (h.text !== content) continue;
          if (attachmentsKeyFromRefs(h.attachments) !== aKey) continue;
          if (Math.abs(evTsMs - h.ts) > 60_000) continue;
          if (attachments && !h.attachments) {
            h.attachments = attachments;
          }
          fallbackSess.lastActiveAt = Math.max(fallbackSess.lastActiveAt, evTsMs);
          hit = true;
          break;
        }
        if (hit) break;
      }
      let sess = store.sessions[sid];
      if (!sess) {
        sess = blankSession(sid);
        store.sessions[sid] = sess;
      }
      const item: HistoryItem = { role: localRole, text: content, ts: ev.ts * 1000 };
      if (incomplete && role === "assistant") item.incomplete = true;
      if (attachments) item.attachments = attachments;
      sess.history.push(item);
      sess.lastActiveAt = ev.ts * 1000;
      if (role === "user" && (sess.title === "新会话" || !sess.title)) {
        sess.title = deriveTitle(sess.history);
      }
      if (sid === store.activeId) {
        // Race-guard handoff. If the streaming bubble is still in DOM
        // for this session, long-poll's message_added beat the SSE done
        // frame (typical when the tab was backgrounded during the stream
        // and the SSE delivery was throttled while events GET resumed
        // on visibility-return). Drop the streaming bubble now and flag
        // the in-flight finalize to short-circuit — the bubble we're
        // about to add carries the server's complete text and replaces
        // the partial streaming view both visually and in history.
        if (role === "assistant") {
          const streamingBubble = msgs.querySelector<HTMLElement>(".msg.bot.streaming");
          if (streamingBubble) {
            removeBubbleWithRow(streamingBubble);
            sync.streamFinalizeSuppressed[sid] = true;
          }
        }
        addMessageBubble(localRole, content, item.attachments);
        if (item.incomplete) appendIncompleteNoticeToLastBubble();
      }
      break;
    }
    case "message_deleted": {
      const rawIndex = payload["index"];
      const rawRole = payload["role"];
      if (typeof rawIndex !== "number" || !Number.isInteger(rawIndex) || rawIndex < 0) break;
      if (rawRole !== "user" && rawRole !== "assistant") break;
      // Dedup: this device just issued the delete (via deleteMessage or
      // regenerate's truncate). Bump lastActiveAt for sidebar ordering
      // but skip the splice + replay — replayActive already ran when
      // the optimistic delete completed.
      if (consumeIfDeleteDuplicate(sid, rawIndex, rawRole as ServerRole)) {
        const s = store.sessions[sid];
        if (s) s.lastActiveAt = Math.max(s.lastActiveAt, ev.ts * 1000);
        break;
      }
      const sess = store.sessions[sid];
      if (!sess) break;
      // The peer device deleted the message; mirror locally.
      // Defensive bounds + role check: a stale long-poll batch from
      // before a cold refetch could carry an index that's out of range
      // for our current view of history (already trimmed on this device).
      // Treat that as a no-op rather than crashing or silently splicing
      // the wrong message.
      const target = sess.history[rawIndex];
      if (!target) break;
      const expectedLocalRole: Role = serverRoleToLocal(rawRole as ServerRole);
      if (target.role !== expectedLocalRole) break;
      sess.history.splice(rawIndex, 1);
      sess.lastActiveAt = ev.ts * 1000;
      if (sid === store.activeId) replayActive();
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
  // try/finally so a thrown event handler doesn't strand the DOM
  // updates from earlier successful events in localStorage-disagreement
  // limbo: without this, a refresh after a mid-batch throw would replay
  // a stale store and silently drop the bubbles the user already saw.
  // On the next long-poll the same events arrive again and the
  // pendingLocals + tail-history dedup paths above suppress duplicates.
  try {
    for (const ev of events) applyEvent(ev);
  } finally {
    saveStore();
    renderSessionList();
  }
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
  const merged: HistoryItem[] = detail.messages.map((m) => {
    const item: HistoryItem = {
      role: serverRoleToLocal(m.role),
      text: m.content,
      ts: (m.ts ?? detail.updated_at) * 1000,
    };
    if (m.role === "assistant" && m.incomplete === true) item.incomplete = true;
    if ((m.role === "user" || m.role === "assistant") && Array.isArray(m.attachments) && m.attachments.length) {
      // Both sides of the conversation can carry attachments. User
      // uploads, plus `/image` slash-command generations stored under
      // the assistant turn. Gating this to "user" only used to strip
      // the generated image on every `coldRefetch`, so a chat
      // reopened after restart showed an empty assistant turn where
      // the image had been.
      item.attachments = m.attachments.filter(isAttachmentRef);
    }
    return item;
  });
  // Preserve local tail entries that haven't yet appeared in the server
  // response but are still vouched for by a live pendingLocal (optimistic
  // echo). Without this, a coldRefetch fired while the server is still
  // processing the user's just-sent /chat would wipe the user bubble until
  // the next sync re-delivers it — exactly the "message disappeared after
  // refresh, came back later" race.
  prunePendingLocals();
  const sid = detail.session_id;
  const survivors = pendingLocals.filter((p) => p.sessionId === sid);
  if (survivors.length && sess.history.length) {
    const serverKeys = new Set<string>();
    for (const item of merged) {
      const serverRole: ServerRole = item.role === "bot" ? "assistant" : "user";
      serverKeys.add(`${serverRole}|${trimContent(item.text)}|${attachmentsKeyFromRefs(item.attachments)}`);
    }
    for (const local of sess.history) {
      if (local.role !== "user" && local.role !== "bot") continue;
      const serverRole: ServerRole = local.role === "bot" ? "assistant" : "user";
      const contentKey = trimContent(local.text);
      const aKey = attachmentsKeyFromRefs(local.attachments);
      const key = `${serverRole}|${contentKey}|${aKey}`;
      if (serverKeys.has(key)) continue;
      const vouched = survivors.some(
        (p) => p.role === serverRole && p.contentKey === contentKey && p.attachmentsKey === aKey,
      );
      if (!vouched) continue;
      merged.push(local);
      serverKeys.add(key);
    }
  }
  sess.history = merged;
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
  // Optimistic-echo dedup entries reference the OLD token's session ids;
  // keeping them around would let a stale entry incorrectly suppress a
  // legitimate message_added on the new login.
  localStorage.removeItem(LS_PENDING_LOCALS);
  pendingLocals.length = 0;
  // Pending-delete dedup entries are also keyed on the old token's
  // session indices; clear them so the new login starts clean.
  localStorage.removeItem(LS_PENDING_LOCAL_DELETES);
  pendingLocalDeletes.length = 0;
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
    // Composer attachments are bound to the upload-time session_id at
    // the server. Switching sessions invalidates the queued file_ids
    // (they'd be rejected as `invalid_attachment` if the user clicked
    // send under the new session). Revoke the object URLs and clear
    // the queue so the user starts the new session fresh.
    clearComposerAttachments();
    // Reset image-mode too: the composer is global but 生图 mode is
    // a per-turn intent. Carrying a /image prefix into a fresh
    // session is almost never what the user wants (the .active
    // chip would also remain lit). Strip the prefix and refresh
    // the button state so the new session opens clean.
    if (isImageCommand(inputEl.value)) {
      inputEl.value = inputEl.value.replace(IMAGE_CMD_RE, "").replace(/^\s+/, "");
      autosizeInput();
    }
    refreshImageBtnState();
    store.activeId = id;
    saveStore();
    replayActive();
    renderSessionList();
    // Active session changed — the clear button's enabled state
    // depends on whether THIS session has a pending stream, so the
    // state must refresh here too (not just at stream start/end).
    updateClearButtonState();
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

let sidebarFocusTrap: FocusTrap | null = null;

function openMobileSidebar(): void {
  sidebarEl.classList.add("open");
  sidebarBackdrop.hidden = false;
  sidebarToggleBtn.setAttribute("aria-expanded", "true");
  // Trap Tab inside the drawer so it can't escape into the (visually-
  // dimmed) chat panel beneath. Initial focus lands on the first
  // session button when present, falling back to the trap's default
  // (first focusable in container) otherwise.
  const firstSessionBtn =
    sessionListEl.querySelector<HTMLButtonElement>("button");
  sidebarFocusTrap?.release();
  sidebarFocusTrap = installFocusTrap(sidebarEl, {
    onEscape: () => {
      closeMobileSidebar();
      sidebarToggleBtn.focus();
    },
    initialFocus: firstSessionBtn,
  });
}
function closeMobileSidebar(): void {
  sidebarEl.classList.remove("open");
  sidebarBackdrop.hidden = true;
  sidebarToggleBtn.setAttribute("aria-expanded", "false");
  sidebarFocusTrap?.release();
  sidebarFocusTrap = null;
}

async function loadChatSite(): Promise<void> {
  try {
    const resp = await fetchWithTimeout(
      SITE_URL,
      { credentials: "same-origin" },
      FETCH_TIMEOUT_FAST_MS,
    );
    if (!resp.ok) return;
    const data = (await resp.json()) as SiteConfig & {
      uploads?: {
        enabled?: boolean;
        max_file_size_mb?: number;
        max_attachments_per_message?: number;
        allowed_mime?: string[];
      };
      image_gen?: { enabled?: boolean };
    };
    // 生图按钮的可见性按服务端配置切换。服务端的 image_gen.enabled
    // 是基于 ImageBridge.enabled 读出来的（同时要求 endpoint + api_key
    // 都非空），所以这里的可见性与 /chat 实际行为一致。关闭后顺手
    // 抹掉 composer 里可能残留的 /image 前缀，避免用户按下后停留在
    // image 模式但按钮不见了。
    const imageEnabled = !!(data.image_gen && data.image_gen.enabled);
    if (imageEnabled) {
      imageBtn.hidden = false;
    } else {
      imageBtn.hidden = true;
      if (isImageCommand(inputEl.value)) {
        inputEl.value = inputEl.value.replace(IMAGE_CMD_RE, "").replace(/^\s+/, "");
        autosizeInput();
      }
      refreshImageBtnState();
    }
    // Apply server-driven upload caps (overrides hardcoded defaults).
    // An operator who edits config to raise max_attachments_per_message
    // from 4 to 8 expects the UI to follow without a code change.
    const u = data.uploads;
    if (u) {
      if (typeof u.enabled === "boolean") UPLOADS_ENABLED = u.enabled;
      if (typeof u.max_file_size_mb === "number" && u.max_file_size_mb > 0) {
        MAX_FILE_SIZE_BYTES = u.max_file_size_mb * 1024 * 1024;
      }
      if (
        typeof u.max_attachments_per_message === "number"
        && u.max_attachments_per_message > 0
      ) {
        MAX_ATTACHMENTS_PER_MESSAGE = u.max_attachments_per_message;
      }
      if (Array.isArray(u.allowed_mime) && u.allowed_mime.length > 0) {
        const filtered = u.allowed_mime.filter(
          (m) => typeof m === "string" && m.length > 0,
        );
        if (filtered.length > 0) {
          ALLOWED_MIME = filtered;
          ALLOWED_MIME_SET = new Set(filtered);
        }
      }
    }
    if (!UPLOADS_ENABLED) {
      // Server says uploads are off — hide the paperclip + don't accept
      // drops/paste.
      try { attachBtn.hidden = true; } catch {}
    }
    const name = (data.site_name || "").trim() || "WebChat Gateway";
    document.title = `${name} · Chat`;
    $("brandName").textContent = name;
    applySiteIcon((data as SiteConfig).site_icon_url || "");
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

// /image, /img, /draw + whitespace → triggers image generation server-side.
// Mirror of `core.image_bridge.is_image_command` so the frontend can route
// these to the non-stream /chat endpoint (the image flow returns a single
// JSON 200 with attachments, not an SSE stream — sending it through
// /chat/stream would still work but is pointless overhead).
const IMAGE_CMD_RE = /^\s*\/(?:image|img|draw)\b/i;
function isImageCommand(text: string): boolean {
  return IMAGE_CMD_RE.test(text || "");
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
  attachments: AttachmentRef[],
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
  const body: Record<string, unknown> = { session_id: sid, username, message };
  if (attachments.length) body.attachments = attachments.map((a) => ({ file_id: a.file_id }));
  const resp = await fetch(CHAT_STREAM_URL, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...bearer() },
    body: JSON.stringify(body),
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

// ---------- Composer attachments ----------

let composerAttachments: PendingAttachment[] = [];
// Latch so the "exceeded 4 chips" notice only fires once per add batch even
// if the user dropped 7 files in one go.
let attachmentsCapNoticeShown = false;

let ALLOWED_MIME_SET: ReadonlySet<string> = new Set(ALLOWED_MIME);

function genLocalId(): string {
  return crypto.randomUUID?.() ?? `a-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// Build a URL the browser can fetch for an already-uploaded server file.
// Build the public URL for an attachment's serve endpoint. `<img src>`
// can't set Authorization headers, so the serve endpoint authenticates
// via a path-scoped, HttpOnly, SameSite=Lax cookie that the gateway
// emits on every /me probe (see `core/file_cookie.py` on the server
// side, `probeQuota` below which triggers the cookie issue on boot).
// The bearer token is NEVER in the URL — that would leak it into
// browser history, server access logs, monitoring, and Referer.
function fileServeUrl(file_id: string): string {
  return `${FILES_URL}/${encodeURIComponent(file_id)}`;
}

// Attach a one-shot retry handler to a chat image. The serve endpoint
// authenticates via the wcg_file cookie which is issued by /me. Two
// situations can leave an image temporarily un-authed:
//   1. First paint after boot, before probeQuota has completed the
//      initial /me roundtrip → cookie not yet present → 401.
//   2. Plugin restart in a long-lived session — the HMAC secret
//      rotates, old cookie value no longer verifies → 401.
// Both heal by hitting /me again. We do that lazily, only on actual
// error. Retry-state is tracked per file_id (the URL's base path) with
// a 60s cooldown — defends against an auth-loop with a permanently
// bad token, while still recovering from transient failures.
const _imgRetriedAt = new Map<string, number>();
function attachImgErrorRetry(img: HTMLImageElement): void {
  img.addEventListener("error", () => {
    const base = img.src.split("?")[0] || img.src;
    const last = _imgRetriedAt.get(base);
    if (last !== undefined && Date.now() - last < 60_000) return;
    _imgRetriedAt.set(base, Date.now());
    void probeQuota().then(() => {
      // Cache-bust so the browser re-fetches even though the URL is
      // structurally identical. The new fetch carries the freshly
      // issued cookie via the same-origin path.
      img.src = "";
      img.src = base + "?_r=" + Date.now();
    }).catch(() => {});
  });
}

// Decode an image via createImageBitmap (preferred — runs off-thread on
// modern browsers) with a graceful fallback to <img> + Image.decode for
// older Safari / mobile WebKit where createImageBitmap doesn't accept Blob.
async function decodeImage(blob: Blob): Promise<{ width: number; height: number; bitmap?: ImageBitmap; img?: HTMLImageElement }> {
  if (typeof createImageBitmap === "function") {
    try {
      const bitmap = await createImageBitmap(blob);
      return { width: bitmap.width, height: bitmap.height, bitmap };
    } catch {
      // Fall through to Image element path.
    }
  }
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    return { width: img.naturalWidth, height: img.naturalHeight, img };
  } finally {
    // Don't revoke yet — caller might still need to draw from it. Caller
    // passes the same blob to the canvas in fallback path; the URL is
    // short-lived anyway and gets GC'd when the function returns.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

// Returns the original blob if the input is GIF (preserve animation), small
// enough to skip, or canvas-encoding fails. Otherwise returns a resized
// JPEG blob with long edge ≤ RESIZE_TARGET_LONG_EDGE.
async function resizeIfNeeded(file: File): Promise<Blob> {
  if (file.type === "image/gif") return file;
  if (file.size <= RESIZE_SKIP_MAX_BYTES) {
    // Small file — peek dimensions to decide, but skip the encode pass if
    // the long edge is already under the cap. The decode itself is cheap
    // compared to a full canvas re-encode.
    try {
      const decoded = await decodeImage(file);
      const longEdge = Math.max(decoded.width, decoded.height);
      if (decoded.bitmap) decoded.bitmap.close();
      if (longEdge <= RESIZE_TARGET_LONG_EDGE) return file;
    } catch {
      return file;
    }
  }
  let decoded: { width: number; height: number; bitmap?: ImageBitmap; img?: HTMLImageElement };
  try {
    decoded = await decodeImage(file);
  } catch {
    return file;
  }
  const { width, height, bitmap, img } = decoded;
  const longEdge = Math.max(width, height);
  if (longEdge <= RESIZE_TARGET_LONG_EDGE) {
    if (bitmap) bitmap.close();
    return file;
  }
  const scale = RESIZE_TARGET_LONG_EDGE / longEdge;
  const targetW = Math.round(width * scale);
  const targetH = Math.round(height * scale);
  const canvas = document.createElement("canvas");
  canvas.width = targetW;
  canvas.height = targetH;
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    if (bitmap) bitmap.close();
    return file;
  }
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  if (bitmap) {
    ctx.drawImage(bitmap, 0, 0, targetW, targetH);
    bitmap.close();
  } else if (img) {
    ctx.drawImage(img, 0, 0, targetW, targetH);
  }
  const out = await new Promise<Blob | null>((resolve) => {
    canvas.toBlob((b) => resolve(b), "image/jpeg", RESIZE_JPEG_QUALITY);
  });
  if (!out) return file;
  // Pick whichever is smaller — for low-detail photos the resized JPEG
  // can be larger than a small original PNG. Resize is a hint, not a
  // bandwidth contract.
  if (out.size >= file.size) return file;
  return out;
}

async function uploadAttachment(blob: Blob, sid: string, attachment: PendingAttachment): Promise<void> {
  const fd = new FormData();
  // Browser-side FormData filenames are mostly cosmetic on the server
  // (we re-derive the extension from the validated MIME), but a stable
  // synthetic name keeps the multipart header tidy.
  const ext = attachment.mime === "image/png" ? "png"
    : attachment.mime === "image/webp" ? "webp"
    : attachment.mime === "image/gif" ? "gif"
    : "jpg";
  fd.append("file", blob, `upload.${ext}`);
  fd.append("session_id", sid);
  try {
    const resp = await fetchWithTimeout(
      UPLOAD_URL,
      {
        method: "POST",
        credentials: "same-origin",
        headers: bearer(),
        body: fd,
      },
      FETCH_TIMEOUT_CHAT_MS,
    );
    if (resp.status === 401) {
      attachment.state = "error";
      attachment.error_message = "未授权";
      renderComposerAttachments();
      handle401();
      return;
    }
    let payload: Record<string, unknown> = {};
    try { payload = await resp.json() as Record<string, unknown>; } catch {}
    if (!resp.ok) {
      const code = typeof payload.error === "string" ? payload.error : `http_${resp.status}`;
      attachment.state = "error";
      attachment.error_message = uploadErrorCopy(code, payload);
      renderComposerAttachments();
      updateSendButtonState();
      return;
    }
    const fid = typeof payload.file_id === "string" ? payload.file_id : "";
    const mime = typeof payload.mime === "string" ? payload.mime : attachment.mime;
    const size = typeof payload.size === "number" ? payload.size : blob.size;
    if (!fid) {
      attachment.state = "error";
      attachment.error_message = "上传失败";
      renderComposerAttachments();
      updateSendButtonState();
      return;
    }
    attachment.file_id = fid;
    attachment.mime = mime;
    attachment.size = size;
    attachment.state = "ready";
    renderComposerAttachments();
    updateSendButtonState();
  } catch (e) {
    const err = e as { name?: string };
    attachment.state = "error";
    attachment.error_message = err.name === "AbortError" ? "上传已取消" : "网络错误";
    renderComposerAttachments();
    updateSendButtonState();
  }
}

function uploadErrorCopy(code: string, payload: Record<string, unknown>): string {
  if (code === "payload_too_large") return "文件过大";
  if (code === "unsupported_mime") return "不支持的图片格式";
  if (code === "invalid_image") return "无效的图片文件";
  if (code === "storage_quota_exceeded") return "存储配额已满";
  if (code === "invalid_session_id") return "会话无效";
  if (code === "invalid_payload") return "上传内容无效";
  if (code === "forbidden_origin") return "来源未授权";
  return typeof payload.detail === "string" ? `${code}: ${payload.detail}` : code;
}

function addAttachmentFiles(rawFiles: FileList | File[]): void {
  const files = Array.from(rawFiles);
  if (!files.length) return;
  const remaining = MAX_ATTACHMENTS_PER_MESSAGE - composerAttachments.length;
  if (remaining <= 0) {
    if (!attachmentsCapNoticeShown) {
      attachmentsCapNoticeShown = true;
      addMessageBubble("notice", `最多 ${MAX_ATTACHMENTS_PER_MESSAGE} 张`);
    }
    return;
  }
  let acceptedCount = 0;
  let droppedForCap = 0;
  for (const file of files) {
    if (acceptedCount >= remaining) {
      droppedForCap += 1;
      continue;
    }
    if (!ALLOWED_MIME_SET.has(file.type)) {
      addMessageBubble("notice", `不支持的图片格式: ${file.name || file.type || "?"}`);
      continue;
    }
    if (file.size > MAX_FILE_SIZE_BYTES) {
      // Cap is server-driven via /site — compute the human MB on the
      // fly so the message tracks what the server is actually
      // enforcing (operator may have set 5MB or 50MB).
      const limitMb = Math.max(1, Math.round(MAX_FILE_SIZE_BYTES / (1024 * 1024)));
      addMessageBubble("notice", `文件过大: ${file.name || "图片"}（上限 ${limitMb}MB）`);
      continue;
    }
    acceptedCount += 1;
    const attachment: PendingAttachment = {
      local_id: genLocalId(),
      mime: file.type,
      size: file.size,
      preview_url: URL.createObjectURL(file),
      state: "uploading",
    };
    composerAttachments.push(attachment);
    const sid = currentSession().id;
    // Resize off-thread; chip already in DOM with the unresized preview URL
    // so the user sees a thumbnail immediately. We then upload the (possibly
    // smaller) blob and don't repaint the preview because the original
    // dimensions of the user's source are what they expect to see.
    void (async (): Promise<void> => {
      let blob: Blob;
      try {
        blob = await resizeIfNeeded(file);
      } catch {
        blob = file;
      }
      attachment.size = blob.size;
      // Resize converts non-GIF to JPEG; keep mime in sync so the server
      // accepts the canonical MIME and we render the right extension hint.
      if (blob.type && blob.type !== file.type) {
        attachment.mime = blob.type;
      }
      await uploadAttachment(blob, sid, attachment);
    })();
  }
  if (droppedForCap > 0 && !attachmentsCapNoticeShown) {
    attachmentsCapNoticeShown = true;
    addMessageBubble("notice", `最多 ${MAX_ATTACHMENTS_PER_MESSAGE} 张`);
  }
  renderComposerAttachments();
  updateSendButtonState();
}

function removeAttachment(local_id: string): void {
  const idx = composerAttachments.findIndex((a) => a.local_id === local_id);
  if (idx < 0) return;
  const att = composerAttachments[idx]!;
  try { URL.revokeObjectURL(att.preview_url); } catch {}
  composerAttachments.splice(idx, 1);
  renderComposerAttachments();
  updateSendButtonState();
}

function clearComposerAttachments(): void {
  for (const a of composerAttachments) {
    try { URL.revokeObjectURL(a.preview_url); } catch {}
  }
  composerAttachments = [];
  attachmentsCapNoticeShown = false;
  renderComposerAttachments();
  updateSendButtonState();
}

function renderComposerAttachments(): void {
  composerAttachmentsEl.replaceChildren();
  for (const a of composerAttachments) {
    const chip = document.createElement("div");
    chip.className = "composer-chip";
    chip.dataset.state = a.state;
    chip.dataset.localId = a.local_id;
    if (a.state === "error" && a.error_message) chip.title = a.error_message;

    const img = document.createElement("img");
    img.src = a.preview_url;
    img.alt = "";
    chip.appendChild(img);

    if (a.state === "uploading") {
      const spin = document.createElement("span");
      spin.className = "chip-spinner";
      chip.appendChild(spin);
    }

    const close = document.createElement("button");
    close.type = "button";
    close.className = "composer-chip-remove";
    close.setAttribute("aria-label", "移除");
    close.textContent = "×";
    close.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      removeAttachment(a.local_id);
    });
    chip.appendChild(close);

    composerAttachmentsEl.appendChild(chip);
  }
}

function updateSendButtonState(): void {
  // Stop button stays enabled while a stream is in flight regardless of
  // composer state — the user might want to abort and try again.
  if (sync.streamAbort) {
    sendBtn.disabled = false;
    return;
  }
  const hasUploading = composerAttachments.some((a) => a.state === "uploading");
  const hasReady = composerAttachments.some((a) => a.state === "ready");
  const hasText = inputEl.value.trim().length > 0;
  sendBtn.disabled = hasUploading || (!hasText && !hasReady);
  attachBtn.disabled = composerAttachments.length >= MAX_ATTACHMENTS_PER_MESSAGE;
}

// ---------- Lightbox ----------

let lightboxKeydown: ((e: KeyboardEvent) => void) | null = null;
let lightboxFocusTrap: FocusTrap | null = null;
function openLightbox(attachments: AttachmentRef[], startIndex: number): void {
  if (!attachments.length) return;
  let idx = Math.max(0, Math.min(startIndex, attachments.length - 1));
  const overlay = document.createElement("div");
  overlay.className = "lightbox";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "图片查看");

  const img = document.createElement("img");
  img.className = "lightbox-img";
  img.alt = "";
  attachImgErrorRetry(img);

  const close = document.createElement("button");
  close.type = "button";
  close.className = "lightbox-close";
  close.setAttribute("aria-label", "关闭");
  close.textContent = "×";

  const prev = document.createElement("button");
  prev.type = "button";
  prev.className = "lightbox-nav lightbox-nav-prev";
  prev.setAttribute("aria-label", "上一张");
  prev.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 18 9 12 15 6"/></svg>';

  const next = document.createElement("button");
  next.type = "button";
  next.className = "lightbox-nav lightbox-nav-next";
  next.setAttribute("aria-label", "下一张");
  next.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6"/></svg>';

  const counter = document.createElement("div");
  counter.className = "lightbox-counter";

  const showSingle = attachments.length === 1;
  if (showSingle) {
    prev.style.display = "none";
    next.style.display = "none";
    counter.style.display = "none";
  }

  const paint = (): void => {
    const a = attachments[idx]!;
    img.src = fileServeUrl(a.file_id);
    counter.textContent = `${idx + 1} / ${attachments.length}`;
  };
  paint();

  const closeOverlay = (): void => {
    if (lightboxKeydown) {
      document.removeEventListener("keydown", lightboxKeydown);
      lightboxKeydown = null;
    }
    lightboxFocusTrap?.release();
    lightboxFocusTrap = null;
    overlay.remove();
    document.body.classList.remove("lightbox-open");
  };
  const goPrev = (): void => {
    idx = (idx - 1 + attachments.length) % attachments.length;
    paint();
  };
  const goNext = (): void => {
    idx = (idx + 1) % attachments.length;
    paint();
  };

  close.addEventListener("click", (e) => { e.stopPropagation(); closeOverlay(); });
  prev.addEventListener("click", (e) => { e.stopPropagation(); goPrev(); });
  next.addEventListener("click", (e) => { e.stopPropagation(); goNext(); });
  img.addEventListener("click", (e) => e.stopPropagation());
  overlay.addEventListener("click", closeOverlay);

  lightboxKeydown = (e: KeyboardEvent): void => {
    // Tab + Escape are owned by the focus trap installed below. Only
    // arrow-key carousel navigation lives here to keep concerns split.
    if (e.key === "ArrowLeft" && !showSingle) { e.preventDefault(); goPrev(); }
    else if (e.key === "ArrowRight" && !showSingle) { e.preventDefault(); goNext(); }
  };
  document.addEventListener("keydown", lightboxKeydown);

  overlay.append(img, close, prev, next, counter);
  document.body.appendChild(overlay);
  document.body.classList.add("lightbox-open");
  // Trap focus inside the lightbox so Tab can't escape into the chat
  // page underneath. Initial focus on the close button — most common
  // first action and avoids a confusing focus ring on the arrow nav.
  lightboxFocusTrap = installFocusTrap(overlay, {
    onEscape: closeOverlay,
    initialFocus: close,
  });
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
  const readyAttachments: AttachmentRef[] = composerAttachments
    .filter((a) => a.state === "ready" && a.file_id)
    .map((a) => ({ file_id: a.file_id!, mime: a.mime }));
  if (!message && !readyAttachments.length) return;
  // Don't fire while uploads are still in flight — server would reject
  // unknown file_ids and the user would lose their text.
  if (composerAttachments.some((a) => a.state === "uploading")) return;
  inputEl.value = "";
  autosizeInput();
  // Programmatic value change doesn't fire 'input' — refresh the
  // image-button .active class explicitly so it doesn't stay lit
  // after the /image prompt has already been sent.
  refreshImageBtnState();
  // Clear chips post-render — the optimistic bubble already references the
  // file_ids, so revoking the local preview URLs is safe and we want the
  // composer empty for the next turn.
  clearComposerAttachments();
  await performSend(message, readyAttachments);
}

// Same as send() but takes pre-resolved text + attachments, bypassing the
// composer read. Used by the retry path on a failed user bubble — the
// composer state is preserved (user may have started typing the next
// turn already) and the resend reuses the same file_ids the failed turn
// originally uploaded.
async function performSend(message: string, readyAttachments: AttachmentRef[]): Promise<void> {
  if (!message && !readyAttachments.length) return;
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
  const eligibleForAutoTitle = isFirstUserMsg && sessBefore.titleManual !== true && message.length > 0;

  // Optimistic user echo: render immediately, push to local history, register
  // dedup entry so the eventual `message_added` from long-poll is dropped
  // (matched on session_id+role+content+attachments+ts, see consumeIfDuplicate).
  addMessageBubble("user", message, readyAttachments.length ? readyAttachments : undefined);
  const userItem: HistoryItem = { role: "user", text: message, ts: Date.now() };
  if (readyAttachments.length) userItem.attachments = readyAttachments;
  sessBefore.history.push(userItem);
  sessBefore.lastActiveAt = Date.now();
  if (sessBefore.title === "新会话" || !sessBefore.title) sessBefore.title = deriveTitle(sessBefore.history);
  recordOptimistic(sid, "user", message, readyAttachments.length ? readyAttachments : undefined);
  saveStore();
  renderSessionList();

  if (eligibleForAutoTitle) {
    requestAutoTitle(sid, message).catch(() => {});
  }

  const tryStream = streamingSupported() && !isStreamCircuitOpen() && !isImageCommand(message);

  try {
    if (tryStream) {
      const outcome = await runStreamingSend(sid, message, readyAttachments);
      if (outcome === "fallback") {
        // runStreamingSend's finally re-enabled the button so the stop
        // button stayed clickable during the streaming attempt; we're
        // about to fire a second HTTP request, so re-disable to prevent
        // the user double-clicking through fallback (which would either
        // duplicate the optimistic user echo or trip server-side
        // concurrent_request).
        sendBtn.disabled = true;
        showTyping();
        await runNonStreamingSend(sid, message, readyAttachments);
      }
    } else {
      showTyping();
      await runNonStreamingSend(sid, message, readyAttachments);
    }
  } finally {
    hideTyping();
    sendBtn.disabled = false;
    setSendMode("send");
    updateSendButtonState();
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
  // For "post": optional image attachments sent in the same body.
  attachments?: AttachmentRef[];
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
  // Internal layout: a Text node for streamed content + a real <span> for
  // the caret. Reasons:
  //   - Text node + nodeValue writes are cheaper than `textContent` (no
  //     subtree rebuild per frame). Important on Edge / older Chromium
  //     where every textContent write creates and destroys text nodes.
  //   - Caret as a real element, NOT ::after, so text-node updates don't
  //     reposition / repaint the caret. ::after pseudo-elements share
  //     layout with their host's contents and can stutter visibly on
  //     fast text growth.
  hideTyping();
  const bubble = document.createElement("div") as HTMLDivElement;
  bubble.className = "msg bot md streaming";
  const streamTextNode = document.createTextNode("");
  const caretSpan = document.createElement("span");
  caretSpan.className = "stream-caret";
  caretSpan.setAttribute("aria-hidden", "true");
  bubble.appendChild(streamTextNode);
  bubble.appendChild(caretSpan);
  // Wrap in a .msg-row from the start so the hover-action chrome is
  // already in place when streaming finishes. CSS hides .msg-actions
  // while the bubble carries the .streaming class — see styles.css.
  const streamRow = document.createElement("div");
  streamRow.className = "msg-row bot-row";
  streamRow.appendChild(bubble);
  streamRow.appendChild(buildMessageActions("bot", bubble));
  msgs.appendChild(streamRow);
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
    streamTextNode.data = pending;
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
  // Throttle scrollToEnd to ~12fps. Reading scrollHeight forces layout
  // and writing scrollTop forces paint; doing both per frame eats into
  // the budget on slower devices. Text grows fast enough that a 4-frame
  // gap (~67ms) between scroll updates is imperceptible.
  let renderFrameCounter = 0;
  const renderTo = (n: number): void => {
    streamTextNode.data = pending.slice(0, n);
    if (++renderFrameCounter % 5 === 0) scrollToEnd();
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
    decorateCodeblocks(bubble);
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
    updateClearButtonState();
  };
  const clearPending = (): void => {
    if (store.pendingStreams[sid]) {
      delete store.pendingStreams[sid];
      savePendingStreams();
      updateClearButtonState();
    }
  };

  const onStreamId = (id: string): void => {
    streamId = id;
    // Expose the id to the stop button so a click can fire
    // POST /chat/stream/{id}/cancel and stop the server-side LLM,
    // not just our local SSE reader. Only set for the POST path —
    // resume/peer attach is not the user's outgoing request.
    if (kind === "post") sync.streamAbortId = id;
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
    // Suppression handoff from applyEvent message_added(assistant). If
    // long-poll won the race and already rendered the server's full
    // text into a sibling bubble + pushed it to history, drop our
    // streaming bubble and skip the push — the history is already
    // correct and pushing a second entry would duplicate. Independent
    // of the tail-history race-guard below (whose ts-window check the
    // backgrounded-tab path can outrun); the explicit flag avoids
    // relying on Date.now() vs server ts comparisons.
    if (sync.streamFinalizeSuppressed[sid]) {
      delete sync.streamFinalizeSuppressed[sid];
      removeBubbleWithRow(bubble);
      return;
    }
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
        removeBubbleWithRow(bubble);
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
    // Clear any pending finalize-suppression flag so a future stream
    // for this session doesn't pick up stale state. finalizeBubble's
    // suppression path consumes-and-deletes, but discardBubble is its
    // sibling exit and was missing the cleanup.
    delete sync.streamFinalizeSuppressed[sid];
    removeBubbleWithRow(bubble);
    clearPending();
  };

  const settlePartial = (kind2: "interrupted" | "error" | "incomplete", incomplete: boolean): void => {
    finalizeBubble({ pending, bubble, incomplete }, kind2);
    clearPending();
  };

  try {
    let info: StreamDoneInfo;
    if (kind === "post") {
      info = await streamChat(sid, opts.message ?? "", opts.attachments ?? [], onChunk, ac.signal, onStreamId);
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

    if (kind === "peer") {
      // Cross-device live attach is an opportunistic enhancement. If it
      // fails, the authoritative message_added events will still populate
      // the conversation, so showing a red "internal_error" bubble only
      // makes a harmless fallback look like a failed chat.
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
      // User cancelled before any text arrived: drop the empty bubble.
      // For "post" mark the user's message as stopped so it stays
      // visible with a "已停止 · 重试 · 编辑" footer instead of being
      // wiped on next refresh by ingestConversationDetail.
      discardBubble();
      if (kind === "post") {
        markLastUserAsFailed(opts.message ?? "", opts.attachments, "stopped");
      }
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
      // Resume attach: surface the error inline so the user knows
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
      // No content arrived → user message effectively didn't go through.
      // Mark it failed so the bubble keeps the retry/edit affordance
      // instead of dropping a separate red error bubble the user can't
      // act on. Resume-kind keeps the old behavior because resume doesn't
      // own a user echo to attach the failure to.
      if (kind === "post") {
        markLastUserAsFailed(opts.message ?? "", opts.attachments, "send_failed");
      } else {
        addMessageBubble(
          isSoft ? "notice" : "error",
          streamErrorCopy(sce.code),
        );
      }
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
      sync.streamAbortId = null;
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
async function runStreamingSend(sid: string, message: string, attachments: AttachmentRef[]): Promise<"ok" | "fallback"> {
  return await attachStreamingBubble({ sid, kind: "post", message, attachments });
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
  // Drop stale PendingStream entries before attempting resume. Server-
  // side buffer grace TTL is 30s after stream close; LLM per-chunk
  // timeout (default 60s) bounds in-flight duration. After ~5 minutes
  // the buffer is definitely gone, so resuming would just hit
  // stream_not_found (best case) or surface a stale closed_failed
  // entry as "请求失败: internal_error" (worst case). Either way the
  // PendingStream is dead — clear it without a doomed network round-
  // trip + scary error toast.
  const STALE_AFTER_MS = 5 * 60 * 1000;
  const startedAt = typeof pending.started_at === "number" ? pending.started_at : 0;
  if (!startedAt || Date.now() - startedAt > STALE_AFTER_MS) {
    delete store.pendingStreams[sid];
    savePendingStreams();
    updateClearButtonState();
    return;
  }
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

async function runNonStreamingSend(sid: string, message: string, attachments: AttachmentRef[]): Promise<void> {
  try {
    const body: Record<string, unknown> = { session_id: sid, username, message };
    if (attachments.length) body.attachments = attachments.map((a) => ({ file_id: a.file_id }));
    const resp = await fetchWithTimeout(
      CHAT_URL,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...bearer() },
        body: JSON.stringify(body),
      },
      FETCH_TIMEOUT_CHAT_MS,
    );
    let payload: Record<string, unknown> = {};
    try { payload = await resp.json(); } catch {}

    if (resp.ok) {
      setBadge(payload.remaining as number, payload.daily_quota as number);
      const reply = (payload.reply as string) || "(空回复)";
      // Image-gen path: /image / /img / /draw returns the same JSON
      // envelope plus an `attachments` array carrying the generated
      // file_id(s). Forward those onto the assistant bubble + history
      // so the image renders inline (same renderer as user-side image
      // attachments). text/streaming responses never set this field,
      // so the legacy path stays unaffected.
      const rawAttachments = (payload as { attachments?: unknown }).attachments;
      const replyAttachments: AttachmentRef[] = Array.isArray(rawAttachments)
        ? rawAttachments
            .filter((a): a is { file_id: string; mime: string } =>
              !!a && typeof a === "object"
              && typeof (a as { file_id?: unknown }).file_id === "string"
              && typeof (a as { mime?: unknown }).mime === "string"
            )
            .map((a) => ({ file_id: a.file_id, mime: a.mime }))
        : [];
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
        addMessageBubble("bot", reply, replyAttachments.length ? replyAttachments : undefined);
        if (sessNow) {
          const item: HistoryItem = { role: "bot", text: reply, ts: Date.now() };
          if (replyAttachments.length) item.attachments = replyAttachments;
          sessNow.history.push(item);
          sessNow.lastActiveAt = Date.now();
          saveStore();
          renderSessionList();
        }
        recordOptimistic(sid, "assistant", reply, replyAttachments.length ? replyAttachments : undefined);
      } else {
        hideTyping();
      }
      return;
    }

    const err = (payload.error as string) || `http_${resp.status}`;
    const s = resp.status;
    // Non-retryable cases: surface the specific notice so the user
    // knows what to fix (login, quota, message length, IP block, etc.)
    // — these don't get a per-message "重试" because retry of the same
    // text won't work without a different action first.
    if (s === 401) {
      addMessageBubble("error", "Token 无效或已撤销，请重新登录。");
      setTimeout(() => { handle401(); }, 1500);
      return;
    }
    if (s === 429 && err === "quota_exceeded") {
      setBadge(0, payload.daily_quota as number);
      addMessageBubble("notice", "今日额度已用完，明日 0 点重置。");
      return;
    }
    if (s === 429 && err === "concurrent_request") {
      // The previous turn is still being processed — the user's message
      // wasn't lost, just queued. Surface a notice and leave the bubble
      // intact (no failure marker — retry would just 429 again).
      addMessageBubble("notice", "上一条还在处理中，稍候。");
      return;
    }
    if (s === 429 && err === "ip_blocked") {
      const retry = resp.headers.get("Retry-After") || payload.retry_after || "?";
      addMessageBubble("error", `请求过于频繁，已暂时封禁，${retry} 秒后重试。`);
      return;
    }
    if (s === 400 && err === "message_too_long") {
      addMessageBubble("error", `消息过长 (上限 ${payload.max_length})。`);
      return;
    }
    if (s === 403 && err === "forbidden_origin") {
      addMessageBubble("error", "页面来源未在 allowed_origins 中。");
      return;
    }
    // Image-gen specific errors. Treat as terminal notices (no retry
    // button) because the user typically needs to either turn the
    // feature on, fix the API key, shorten the prompt, or wait —
    // pressing 重试 with the same text won't help.
    const imageDetail = typeof payload.detail === "string"
      ? String(payload.detail).slice(0, 200)
      : "";
    if (err === "image_disabled") {
      addMessageBubble("notice", "管理员尚未启用生图功能。");
      return;
    }
    if (err === "image_prompt_empty") {
      addMessageBubble("notice", "请在 /image 后填写图片描述。");
      return;
    }
    if (err === "image_timeout") {
      addMessageBubble(
        "error",
        imageDetail
          ? `生图超时：${imageDetail}`
          : "生图超时，请稍后再试，或在设置里增大「请求总超时」。",
      );
      return;
    }
    if (err === "image_call_failed") {
      // Bridge surfaces the upstream error (auth, model not found,
      // unknown parameter, etc.) into `detail`. Show it so the
      // operator can fix the actual config issue instead of a
      // generic "请检查管理员的生图配置".
      addMessageBubble(
        "error",
        imageDetail
          ? `生图失败：${imageDetail}`
          : "生图失败，请检查管理员的生图配置。",
      );
      return;
    }
    if (err === "empty_image_reply") {
      addMessageBubble(
        "error",
        imageDetail
          ? `生图返回为空：${imageDetail}`
          : "生图服务返回为空，请稍后再试或更换 prompt。",
      );
      return;
    }
    // Retryable failures (5xx, llm_timeout/empty_reply with no fallback
    // path remaining, etc.). Mark the user bubble failed instead of
    // dropping a generic red bubble — the user gets a 重试 button right
    // where they sent, matching ChatGPT/iMessage patterns.
    markLastUserAsFailed(message, attachments, "send_failed");
  } catch (error) {
    // Network error, fetch timeout, etc. Same treatment as retryable
    // server failures above.
    markLastUserAsFailed(message, attachments, "send_failed");
  }
}

$<HTMLButtonElement>("clearHistory").onclick = () => { void clearActiveHistory(); };
$<HTMLButtonElement>("newSessionBtn").onclick = newSession;

// Disable the "clear history" button while a stream is in flight for
// the active session. The backend already rejects clear_history with
// 429 concurrent_request if a /chat/stream is mid-flight (it shares
// the same PerTokenConcurrency lock); without the frontend gate the
// user would just see a 429 toast and have to retry manually.
//
// We intentionally do NOT auto-retry on 429. Clear is a destructive
// operation: if we wait 5s and retry, the assistant might have just
// produced a long reply that gets wiped — confusing UX. Disabling
// the button shows the user "wait for the message to finish" up
// front, which is the safer pattern for irreversible actions.
function updateClearButtonState(): void {
  const btn = $<HTMLButtonElement>("clearHistory");
  const sid = store.activeId;
  const streaming = !!(sid && store.pendingStreams[sid]);
  btn.disabled = streaming;
  if (streaming) {
    btn.title = "当前正在回复，完成后再清空";
  } else {
    btn.removeAttribute("title");
  }
}
updateClearButtonState();
$<HTMLButtonElement>("logout").onclick = () => {
  if (!confirm("登出会清除本机保存的 token 与对话历史。继续？")) return;
  sync.stopped = true;
  abortInflightLongPoll();
  if (sync.streamAbort) { sync.streamAbort.abort(); sync.streamAbort = null; }
  clearTimer("shortPollTimer");
  clearTimer("probeTimer");
  clearTimer("retryTimer");
  // Server-clear the wcg_file cookie + record server-side logout. The
  // cookie is HttpOnly so JS can't touch it directly; the response's
  // Set-Cookie header is what the browser commits. We POST under the
  // cookie's Path scope (`/api/webchat/files`) so sendBeacon — which
  // can't set custom headers — still carries the cookie, letting the
  // server identify the token and add it to the invalidation tracker.
  // Without that, logout would only clear the browser cookie but the
  // server would still honour HMAC-valid cookies until natural expiry.
  // `navigator.sendBeacon` is documented to survive navigation; we
  // fall back to keepalive-tagged fetch where it's unavailable.
  const logoutUrl = `${API}/files/logout`;
  let beaconQueued = false;
  try {
    if (typeof navigator.sendBeacon === "function") {
      beaconQueued = navigator.sendBeacon(logoutUrl);
    }
  } catch { /* fall through */ }
  if (!beaconQueued) {
    try {
      void fetch(logoutUrl, {
        method: "POST",
        credentials: "same-origin",
        keepalive: true,        // survive navigation
      }).catch(() => {});
    } catch { /* fall through */ }
  }
  for (const k of [LS_TOKEN, LS_USERNAME, LS_STORE, LS_LAST_PTS, LS_PENDING_STREAMS, LS_PENDING_LOCALS]) localStorage.removeItem(k);
  location.replace("/");
};

sidebarToggleBtn.addEventListener("click", () => {
  if (sidebarEl.classList.contains("open")) closeMobileSidebar();
  else openMobileSidebar();
});
sidebarBackdrop.addEventListener("click", closeMobileSidebar);
// Escape-to-close is owned by the sidebar's focus trap (installed in
// openMobileSidebar). No global listener here — it would race with the
// trap's onEscape and double-fire closeMobileSidebar.

inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    void send();
  }
});

// Paperclip → file picker. Reset .value after each open so the change
// handler fires even when the user re-selects the same file (the browser
// suppresses change events on identical selections otherwise).
attachBtn.addEventListener("click", () => {
  if (attachBtn.disabled) return;
  fileInputEl.value = "";
  fileInputEl.click();
});
// 生成图片 button. Prepends "/image " to the textarea if the prefix
// isn't already there; clicking again with the prefix present
// removes it (toggle behaviour). Mirrors how operators can also just
// type the trigger themselves — the button is an affordance, not a
// modal mode.
function refreshImageBtnState(): void {
  if (isImageCommand(inputEl.value)) {
    imageBtn.classList.add("active");
    imageBtn.setAttribute("aria-pressed", "true");
  } else {
    imageBtn.classList.remove("active");
    imageBtn.setAttribute("aria-pressed", "false");
  }
}
imageBtn.addEventListener("click", () => {
  if (imageBtn.disabled) return;
  if (isImageCommand(inputEl.value)) {
    // Toggle off — strip the prefix + leading whitespace.
    inputEl.value = inputEl.value.replace(IMAGE_CMD_RE, "").replace(/^\s+/, "");
  } else {
    inputEl.value = `/image ${inputEl.value.trimStart()}`;
  }
  autosizeInput();
  refreshImageBtnState();
  inputEl.focus();
  // Move caret to end so the prompt typing continues naturally.
  const len = inputEl.value.length;
  try { inputEl.setSelectionRange(len, len); } catch {}
});
inputEl.addEventListener("input", refreshImageBtnState);
refreshImageBtnState();
fileInputEl.addEventListener("change", () => {
  if (fileInputEl.files && fileInputEl.files.length) {
    addAttachmentFiles(fileInputEl.files);
  }
  fileInputEl.value = "";
});

// Drag-and-drop on the composer footer. Track enter/leave depth so child
// transitions don't flicker the overlay off. We only show the overlay if
// the drag contains files (matches `Files` in dataTransfer.types).
let dragDepth = 0;
function dragHasFiles(e: DragEvent): boolean {
  const dt = e.dataTransfer;
  if (!dt) return false;
  for (const t of dt.types) if (t === "Files") return true;
  return false;
}
footerEl.addEventListener("dragenter", (e) => {
  if (!dragHasFiles(e)) return;
  e.preventDefault();
  dragDepth += 1;
  dropOverlayEl.hidden = false;
});
footerEl.addEventListener("dragover", (e) => {
  if (!dragHasFiles(e)) return;
  e.preventDefault();
  if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
});
footerEl.addEventListener("dragleave", (e) => {
  if (!dragHasFiles(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) dropOverlayEl.hidden = true;
});
footerEl.addEventListener("drop", (e) => {
  if (!dragHasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  dropOverlayEl.hidden = true;
  const files = e.dataTransfer?.files;
  if (files && files.length) addAttachmentFiles(files);
});

// Paste handler. Two concerns:
//
// 1) Image paste (clipboardData.files is populated for raw image paste on
//    every modern browser; we filter to images defensively). Routed into the
//    attachment pipeline.
//
// 2) Text paste with stray line breaks. Users pasting from another chat,
//    a Word doc, or a hard-wrapped terminal/log line get \n at every visual
//    column-width line break of the source — and the bubble preserves
//    them verbatim (`white-space: pre-wrap`), so a single sentence shows
//    up as 4-5 staircase lines the user didn't intend. We normalize:
//      * CRLF / CR / U+2028 → \n; U+2029 → \n\n (Unicode line/paragraph
//        separators land in clipboards from some IMEs and rich editors).
//      * Within a paragraph (no blank line gap), single \n → space —
//        treats source-hardwraps as soft wraps, matching how the same
//        text would have flowed if pasted into any other chat input.
//      * Paragraph breaks (\n\n+) collapse to a single \n\n so excess
//        blank lines from copy/paste don't expand the bubble vertically.
//    Shift+Enter still works for intentional line breaks — that path
//    never goes through `paste`.
function normalizePastedText(s: string): string {
  let t = s
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .replace(/\u2028/g, "\n")
    .replace(/\u2029/g, "\n\n");
  // Split on blank-line boundaries, collapse intra-paragraph wraps to
  // a single space, drop empty paragraphs from runs of >2 newlines.
  return t
    .split(/\n{2,}/)
    .map((p) => p.replace(/[ \t]*\n[ \t]*/g, " ").replace(/[ \t]{2,}/g, " ").trim())
    .filter((p) => p.length > 0)
    .join("\n\n");
}
inputEl.addEventListener("paste", (e) => {
  const cd = e.clipboardData;
  if (!cd) return;
  // Image branch first — keep existing behavior intact.
  if (cd.files && cd.files.length) {
    const images: File[] = [];
    for (const f of cd.files) {
      if (f.type && f.type.startsWith("image/")) images.push(f);
    }
    if (images.length) {
      e.preventDefault();
      addAttachmentFiles(images);
      return;
    }
  }
  // Text branch — only intercept when normalization would actually
  // change anything. Skipping the preventDefault on a no-op paste keeps
  // the native browser path (and its undo entry) intact.
  const raw = cd.getData("text/plain");
  if (!raw) return;
  const normalized = normalizePastedText(raw);
  if (normalized === raw) return;
  e.preventDefault();
  // execCommand("insertText") preserves the textarea's native undo
  // stack — replacing the value directly wipes undo history, which the
  // user notices the moment they hit Ctrl+Z after a paste. Fall back to
  // a manual splice only if the browser refused the command (Firefox
  // ESR with certain hardening configs, mostly).
  if (!document.execCommand("insertText", false, normalized)) {
    const start = inputEl.selectionStart ?? inputEl.value.length;
    const end = inputEl.selectionEnd ?? inputEl.value.length;
    inputEl.value = inputEl.value.slice(0, start) + normalized + inputEl.value.slice(end);
    const caret = start + normalized.length;
    inputEl.selectionStart = inputEl.selectionEnd = caret;
  }
  autosizeInput();
  updateSendButtonState();
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
inputEl.addEventListener("input", () => {
  autosizeInput();
  updateSendButtonState();
});
// Reset to one line on initial render and any external value clear.
autosizeInput();
updateSendButtonState();
sendBtn.onclick = (): void => {
  // Same button doubles as stop while a stream is in flight. Click during
  // stream cancels the AbortController; the streaming path catches the
  // resulting AbortError and either keeps the partial bubble or drops it.
  if (sync.streamAbort) {
    // If we already know the server-side stream_id, ask the server to
    // stop the LLM iteration too. Without this the server keeps
    // generating after we drop the SSE connection (by design — see the
    // "Client-disconnect semantics" comment in handlers/chat.py) and
    // the full reply lands later via long-poll. Fire-and-forget: the
    // SSE reader's AbortError handles the local teardown either way,
    // and a 404 from a stream that already finished naturally is fine.
    const cancelId = sync.streamAbortId;
    if (cancelId) {
      const url = `${CHAT_STREAM_URL}/${encodeURIComponent(cancelId)}/cancel`;
      fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: bearer(),
        keepalive: true,
      }).catch(() => {});
    }
    sync.streamAbort.abort();
    return;
  }
  void send();
};

document.addEventListener("visibilitychange", onVisibilityChange);

// Cold boot: paint cache, then refetch authoritative state, then start sync.
// probeQuota() is fired EARLY (sync-kicked, before replayActive) so the
// wcg_file cookie lands as close as possible to the first <img src>
// requests. attachImgErrorRetry handles the remaining race where the
// /me response hasn't returned by the time the browser starts fetching
// images, OR a long-lived session straddles a server restart.
void probeQuota();
saveStore();
renderSessionList();
replayActive();
loadChatSite();
setupThemeToggle();

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
