// Builds each page in PAGES into examples/<page>/index.html, single self-
// contained file (CSS/JS inlined) so the Python backend's _file_handler can
// keep serving them verbatim.

import { build } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

const PAGES = ["landing"];

async function buildPage(page) {
  const root = resolve(__dirname, "../src", page);
  const outDir = resolve(__dirname, "../../examples", page);
  await build({
    root,
    plugins: [viteSingleFile()],
    logLevel: "info",
    build: {
      outDir,
      emptyOutDir: true,
      cssCodeSplit: false,
      assetsInlineLimit: 100_000_000,
      rollupOptions: {
        output: {
          inlineDynamicImports: true,
        },
      },
    },
  });
  console.log(`[build] ${page} -> ${outDir}/index.html`);
}

let failed = false;
for (const page of PAGES) {
  try {
    await buildPage(page);
  } catch (err) {
    failed = true;
    console.error(`[build] ${page} failed:`, err);
  }
}
process.exit(failed ? 1 : 0);
