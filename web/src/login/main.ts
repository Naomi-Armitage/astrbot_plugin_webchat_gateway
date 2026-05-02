interface SiteConfig {
  site_name?: string;
  welcome_message?: string;
  show_github_link?: boolean;
  privacy_url?: string;
  theme_family?: string;
}

const LS_TOKEN = "wcg.token";
const LS_USERNAME = "wcg.username";
const LS_FAMILY = "wcg.theme.family";
const LS_MODE = "wcg.theme.mode";

const $ = <T extends Element = HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing #${id}`);
  return el as unknown as T;
};

// Refuse anything that's not http(s) or root-relative — operator config
// is trusted but `javascript:` / `data:` URLs would still execute on click.
const HREF_OK = /^(https?:\/\/|\/)/i;

function resolveTheme(family: string, mode: "light" | "dark"): string {
  if (family === "classic") return mode === "dark" ? "classic-dark" : "classic-light";
  return mode === "dark" ? "midnight" : "paper";
}

// Hydrate token / username from any prior visit (returning user).
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

// Pull operator-set branding. Failure is non-fatal: defaults render.
async function loadSite(): Promise<void> {
  try {
    const resp = await fetch("/api/webchat/site", { credentials: "same-origin" });
    if (!resp.ok) return;
    const data = (await resp.json()) as SiteConfig;
    const name = (data.site_name || "").trim() || "WebChat Gateway";
    document.title = name;
    $("brandName").textContent = name;
    $("heroTitle").textContent = name;
    $("footerName").textContent = name;
    $("welcomeMessage").textContent = (data.welcome_message || "").trim();
    if (data.show_github_link) {
      $("githubLink").hidden = false;
    }
    const privacy = (data.privacy_url || "").trim();
    if (privacy && HREF_OK.test(privacy)) {
      const a = $<HTMLAnchorElement>("privacyLink");
      a.href = privacy;
      a.hidden = false;
      if (/^https?:\/\//i.test(privacy)) {
        a.target = "_blank";
        a.rel = "noopener";
      }
    }
    const family = data.theme_family === "classic" ? "classic" : "notebook";
    const stored = localStorage.getItem(LS_FAMILY);
    if (stored !== family) {
      try { localStorage.setItem(LS_FAMILY, family); } catch {}
      const cur = document.documentElement.getAttribute("data-theme");
      const isDark = cur === "midnight" || cur === "classic-dark";
      const resolved = resolveTheme(family, isDark ? "dark" : "light");
      if (resolved !== cur) {
        document.documentElement.setAttribute("data-theme", resolved);
      }
    }
  } catch {
    /* offline / network error — keep defaults */
  }
}
loadSite();

// Theme toggle. Init script in <head> sets data-theme on first paint;
// here we just wire the sun/moon button and persist mode flips.
(function setupThemeToggle() {
  const btn = $<HTMLButtonElement>("themeToggle");
  const icon = $<SVGElement>("themeIcon");
  const sunPath = '<path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/><circle cx="12" cy="12" r="4"/>';
  const moonPath = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';
  function applyMode(mode: "light" | "dark", persist: boolean): void {
    const family = localStorage.getItem(LS_FAMILY) || "notebook";
    document.documentElement.setAttribute("data-theme", resolveTheme(family, mode));
    icon.innerHTML = mode === "dark" ? moonPath : sunPath;
    btn.setAttribute("aria-pressed", String(mode === "dark"));
    if (persist) { try { localStorage.setItem(LS_MODE, mode); } catch {} }
  }
  const cur = document.documentElement.getAttribute("data-theme");
  const initialMode: "light" | "dark" = (cur === "midnight" || cur === "classic-dark") ? "dark" : "light";
  applyMode(initialMode, false);
  btn.addEventListener("click", () => {
    const isDark = btn.getAttribute("aria-pressed") === "true";
    applyMode(isDark ? "light" : "dark", true);
  });
})();

export {};
