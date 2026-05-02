// Sync theme init: runs before <style> applies, so the correct
// :root[data-theme="..."] block wins from first paint (no flash).
// Family default is classic (matches operator-side default in
// _conf_schema.json); mode follows OS unless the user toggled.
(function(){
  try {
    var family = localStorage.getItem("wcg.theme.family") || "classic";
    var mode = localStorage.getItem("wcg.theme.mode")
      || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    var t = family === "notebook"
      ? (mode === "dark" ? "midnight" : "paper")
      : (mode === "dark" ? "classic-dark" : "classic-light");
    document.documentElement.setAttribute("data-theme", t);
  } catch (e) {
    document.documentElement.setAttribute("data-theme", "classic-light");
  }
})();
