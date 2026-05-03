export interface SiteConfig {
  site_name?: string;
  welcome_message?: string;
  show_github_link?: boolean;
  privacy_url?: string;
  theme_family?: string;
}

export const LS_TOKEN = "wcg.token";
export const LS_FAMILY = "wcg.theme.family";
export const LS_MODE = "wcg.theme.mode";

const CHROME_COLORS: Record<string, string> = {
  paper: "#fafaf9",
  midnight: "#000000",
  "classic-light": "#ffffff",
  "classic-dark": "#0d1117",
};

export const $ = <T extends Element = HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing #${id}`);
  return el as unknown as T;
};

// Refuse anything that's not http(s) or root-relative. Operator config is
// trusted, but `javascript:` / `data:` URLs would still execute on click.
export const HREF_OK = /^(https?:\/\/|\/)/i;

export function resolveTheme(family: string, mode: "light" | "dark"): string {
  if (family === "classic") return mode === "dark" ? "classic-dark" : "classic-light";
  return mode === "dark" ? "midnight" : "paper";
}

export function modeFromTheme(theme: string | null): "light" | "dark" {
  return theme === "midnight" || theme === "classic-dark" ? "dark" : "light";
}

function configuredFamily(): string {
  const fromMarkup = document.documentElement.getAttribute("data-default-theme-family");
  if (fromMarkup === "notebook" || fromMarkup === "classic") return fromMarkup;
  const stored = localStorage.getItem(LS_FAMILY);
  return stored === "notebook" ? "notebook" : "classic";
}

export function paintBrowserChrome(theme: string): void {
  const color = CHROME_COLORS[theme] || CHROME_COLORS["classic-light"]!;
  let meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.appendChild(meta);
  }
  meta.content = color;
  const mode = modeFromTheme(theme);
  document.documentElement.style.backgroundColor = color;
  document.documentElement.style.colorScheme = mode;
  document.body.style.backgroundColor = color;
}

export function isIOSWebKit(): boolean {
  const ua = navigator.userAgent || "";
  const platform = navigator.platform || "";
  return /iP(?:hone|ad|od)/.test(platform)
    || /iP(?:hone|ad|od)/.test(ua)
    || (platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

export function reloadIOSChromeOnce(key: string, value: string): boolean {
  if (!isIOSWebKit()) return false;
  try {
    if (sessionStorage.getItem(key) === value) return false;
    sessionStorage.setItem(key, value);
  } catch {
    // Without a session guard this path could reload forever.
    return false;
  }
  location.reload();
  return true;
}

// Pull operator-set branding. Failure is non-fatal: defaults render.
export async function loadSite(): Promise<void> {
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
      const mode = modeFromTheme(cur);
      const resolved = resolveTheme(family, mode);
      if (resolved !== cur) {
        document.documentElement.setAttribute("data-theme", resolved);
        paintBrowserChrome(resolved);
      }
      reloadIOSChromeOnce("wcg.theme.family.reload", `${family}:${mode}`);
    }
  } catch {
    /* offline / network error: keep defaults */
  }
}

// Theme toggle. Init script in <head> sets data-theme on first paint;
// here we wire the sun/moon button and persist mode flips.
export function setupThemeToggle(): void {
  const btn = $<HTMLButtonElement>("themeToggle");
  const icon = $<SVGElement>("themeIcon");
  const sunPath = '<path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/><circle cx="12" cy="12" r="4"/>';
  const moonPath = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';
  function applyMode(mode: "light" | "dark", persist: boolean): void {
    const family = configuredFamily();
    const theme = resolveTheme(family, mode);
    document.documentElement.setAttribute("data-theme", theme);
    paintBrowserChrome(theme);
    icon.innerHTML = mode === "dark" ? moonPath : sunPath;
    btn.setAttribute("aria-pressed", String(mode === "dark"));
    if (persist) {
      try { localStorage.setItem(LS_MODE, mode); } catch {}
      if (isIOSWebKit()) location.reload();
    }
  }
  const cur = document.documentElement.getAttribute("data-theme");
  const initialMode = modeFromTheme(cur);
  applyMode(initialMode, false);
  btn.addEventListener("click", () => {
    const isDark = btn.getAttribute("aria-pressed") === "true";
    applyMode(isDark ? "light" : "dark", true);
  });
}
