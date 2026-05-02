import { LS_TOKEN, $, loadSite, setupThemeToggle } from "../shared/site";

// Returning user with a saved token: skip /login, go straight to /chat.
const cta = $<HTMLAnchorElement>("primaryCta");
const ctaText = $("primaryCtaText");
const ctaHint = $("ctaHint");
const savedToken = (localStorage.getItem(LS_TOKEN) || "").trim();
if (savedToken) {
  cta.href = "/chat";
  ctaText.textContent = "继续聊天";
  ctaHint.textContent = "用上次保存的 token";
}

loadSite();
setupThemeToggle();
