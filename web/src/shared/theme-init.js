// Sync theme init: runs before <style> applies, so the correct
// :root[data-theme="..."] block wins from first paint (no flash).
// The backend may inject data-default-theme-family on <html>; prefer it
// because theme family is operator config, not a per-browser preference.
(function(){
  var COLORS = {
    "paper": "#fafaf9",
    "midnight": "#000000",
    "classic-light": "#ffffff",
    "classic-dark": "#0d1117"
  };

  function familyOrDefault(value) {
    return value === "notebook" ? "notebook" : "classic";
  }

  function modeOrSystem(value) {
    if (value === "light" || value === "dark") return value;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function resolveTheme(family, mode) {
    return family === "notebook"
      ? (mode === "dark" ? "midnight" : "paper")
      : (mode === "dark" ? "classic-dark" : "classic-light");
  }

  function paintChrome(theme, mode) {
    var color = COLORS[theme] || COLORS["classic-light"];
    var meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      meta = document.createElement("meta");
      meta.setAttribute("name", "theme-color");
      document.head.appendChild(meta);
    }
    meta.setAttribute("content", color);
    document.documentElement.style.backgroundColor = color;
    document.documentElement.style.colorScheme = mode;
  }

  try {
    var configuredFamily = document.documentElement.getAttribute("data-default-theme-family");
    var family = configuredFamily
      ? familyOrDefault(configuredFamily)
      : familyOrDefault(localStorage.getItem("wcg.theme.family"));
    var mode = modeOrSystem(localStorage.getItem("wcg.theme.mode"));
    var theme = resolveTheme(family, mode);
    if (configuredFamily) {
      try { localStorage.setItem("wcg.theme.family", family); } catch (e) {}
    }
    document.documentElement.setAttribute("data-theme", theme);
    paintChrome(theme, mode);
  } catch (e) {
    document.documentElement.setAttribute("data-theme", "classic-light");
    paintChrome("classic-light", "light");
  }
})();
