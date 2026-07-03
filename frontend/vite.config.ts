import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    fs: {
      // standards/us_codes.json and examples/*.json are imported from the
      // repo root — single source of truth shared with the Python side.
      allow: [".."],
    },
    proxy: {
      // LLM calls and export jobs go to the FastAPI backend; the 3D preview
      // itself never round-trips to the server (T4.6).
      // backend serves its routes under /api too, matching Vercel's layout
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
