import { LS_TOKEN, $, loadSite, setupThemeToggle } from "../shared/site";

const LS_USERNAME = "wcg.username";
// Chat-page cache keys. We don't import these from chat_client (login
// must not pull the chat bundle), but they MUST stay in sync. The login
// page nukes them on any submit so a different token can't read the
// previous user's session list / pts cursor.
const LS_CHAT_STORE = "wcg.chat.sessions";
const LS_CHAT_LAST_PTS = "wcg.chat.lastPts";

const tokenInput = $<HTMLInputElement>("token");
const usernameInput = $<HTMLInputElement>("username");
tokenInput.value = localStorage.getItem(LS_TOKEN) || "";
usernameInput.value = localStorage.getItem(LS_USERNAME) || "";

const revealBtn = $<HTMLButtonElement>("revealBtn");
revealBtn.addEventListener("click", () => {
  const showing = tokenInput.type === "text";
  tokenInput.type = showing ? "password" : "text";
  revealBtn.textContent = showing ? "显示" : "隐藏";
  revealBtn.setAttribute("aria-pressed", String(!showing));
});

const form = $<HTMLFormElement>("entryForm");
const errEl = $("err");
const submitBtn = $<HTMLButtonElement>("submitBtn");
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errEl.textContent = "";
  const token = tokenInput.value.trim();
  const username = usernameInput.value.trim() || "Friend";
  if (!token) {
    errEl.textContent = "请先填写访问 token。";
    tokenInput.focus();
    return;
  }
  submitBtn.disabled = true;
  try {
    // Probe /me with the candidate token so a wrong token shows the error
    // here, instead of letting /chat bounce back after a redirect flash.
    const resp = await fetch("/api/webchat/me", {
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
    });
    if (resp.status === 401) {
      errEl.textContent = "Token 无效或已撤销。";
      tokenInput.focus();
      return;
    }
    if (resp.status === 429) {
      errEl.textContent = "请求过于频繁，请稍后再试。";
      return;
    }
    if (!resp.ok) {
      errEl.textContent = `服务异常 (${resp.status})，请稍后再试。`;
      return;
    }
    try { localStorage.setItem(LS_TOKEN, token); } catch {}
    try { localStorage.setItem(LS_USERNAME, username); } catch {}
    // Drop chat-page caches whenever a token is committed here. Skipping
    // this would let the *previous* token's session list and last_pts
    // cursor leak into the new login (cross-token state pollution; a
    // particularly bad one because session titles are user-visible).
    try { localStorage.removeItem(LS_CHAT_STORE); } catch {}
    try { localStorage.removeItem(LS_CHAT_LAST_PTS); } catch {}
    location.href = "/chat";
  } catch {
    errEl.textContent = "网络错误，请检查连接后重试。";
  } finally {
    submitBtn.disabled = false;
  }
});

loadSite();
setupThemeToggle();
