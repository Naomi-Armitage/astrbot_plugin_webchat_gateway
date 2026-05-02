import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

// Used by `vite dev`; production builds re-apply these via scripts/build.mjs.
export default defineConfig({
  plugins: [viteSingleFile()],
  build: {
    cssCodeSplit: false,
    assetsInlineLimit: 100_000_000,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
