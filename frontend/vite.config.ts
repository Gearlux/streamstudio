import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During `npm run dev`, the frontend runs on :5173 and proxies /api + /ws to
// the FastAPI server on :8765. In production the frontend is built to dist/
// and served directly by FastAPI under '/'.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: true, ws: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
