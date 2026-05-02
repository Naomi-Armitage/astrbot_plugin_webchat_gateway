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
form.addEventListener("submit", (e) => {
  e.preventDefault();
  errEl.textContent = "";
  const token = tokenInput.value.trim();
  const username = usernameInput.value.trim() || "Friend";
  if (!token) {
    errEl.textContent = "请先填写访问 token。";
    tokenInput.focus();
    return;
  }
  try { localStorage.setItem(LS_TOKEN, token); } catch {}
  try { localStorage.setItem(LS_USERNAME, username); } catch {}
  submitBtn.disabled = true;
  location.href = "/chat";
});

loadSite();
setupThemeToggle();
