import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// In development the UI calls the API same-origin (/api, /ws) and Vite proxies
// to the backend -- the same shape as the production nginx container.
const backend = process.env.BACKEND_URL || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
});
