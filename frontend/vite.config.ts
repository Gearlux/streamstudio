import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// `base: "./"` makes every asset URL in the built index.html RELATIVE, so Flask can serve the build at `/`
// (bare `streamstudio`, the canvas editor) or a consuming app can mount the page under its own prefix (`/editor/`)
// without rebuilding. The page reaches the API the same way — `api/...` relative to the document, never `/api`.
// The build lands INSIDE the package, `streamstudio/canvas` (`streamstudio.page.OUT_DIR` says the same), and is
// COMMITTED there with its record `.built-from`, so every install serves the page without Node. vite empties the
// folder first (`emptyOutDir` — for a folder outside this one vite would only warn and leave the old files), and
// writes no source map: the committed .js would name a map no install carries. `npm run build` (package.json) is
// `build:page` — this build — between two streamstudio steps that note the source before and record it after; a
// server's start runs `build:page` alone.
// In dev, vite serves the sources on its own port and proxies `api/` to a running `streamstudio` canvas editor
// on its default port (8190, `streamstudio.cli.EDITOR_PORT`).
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8190", changeOrigin: true },
    },
  },
  build: { outDir: "../streamstudio/canvas", emptyOutDir: true, sourcemap: false },
  test: { environment: "node" },
});
