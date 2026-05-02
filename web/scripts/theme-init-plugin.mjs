import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const PLACEHOLDER = "<!-- THEME_INIT -->";

export function themeInitPlugin() {
  const src = readFileSync(resolve(__dirname, "../src/shared/theme-init.js"), "utf8");
  const tag = `<script>\n${src}\n</script>`;
  return {
    name: "wcg-theme-init",
    transformIndexHtml(html) {
      if (!html.includes(PLACEHOLDER)) {
        throw new Error(`themeInitPlugin: missing ${PLACEHOLDER} placeholder in HTML`);
      }
      return html.replace(PLACEHOLDER, tag);
    },
  };
}
