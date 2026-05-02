import { LS_TOKEN, $, loadSite, setupThemeToggle } from "../shared/site";

const LS_USERNAME = "wcg.username";

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
    location.href = "/chat";
  } catch {
    errEl.textContent = "网络错误，请检查连接后重试。";
  } finally {
    submitBtn.disabled = false;
  }
});

loadSite();
setupThemeToggle();
