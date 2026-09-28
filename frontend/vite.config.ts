import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// `base: "./"` makes every asset URL in the built index.html RELATIVE, so Flask can serve dist/ at `/`
// (streamstudio editor) or a consuming app can mount the page under its own prefix (`/editor/`) without
// rebuilding. The page reaches the API the same way — `api/...` relative to the document, never `/api`.
// In dev, vite serves the sources on its own port and proxies `api/` to a running `streamstudio editor`
// on its default port (8190, `streamstudio.cli.EDITOR_PORT`).
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8190", changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
  test: { environment: "node" },
});
