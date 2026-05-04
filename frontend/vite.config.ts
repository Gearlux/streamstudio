/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During `npm run dev`, the frontend runs on :5173 and proxies /api + /ws to
// the FastAPI server on :8765. In production the frontend is built to dist/
// and served directly by FastAPI under '/'.
//
// Vitest config (the `test` key) lives here too — Vitest reads the same
// vite.config.ts so plugins and resolve rules stay in sync between the dev
// server, the production build, and the test runner.
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
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
