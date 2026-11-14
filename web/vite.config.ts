import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The browser always talks to "/api"; Vite forwards it to the FastAPI server with the
// prefix stripped, so GET /api/fields reaches GET /fields. Same rule for `dev` and `preview`.
const apiPort = process.env.API_PORT ?? "8010";
const proxy = {
  "/api": {
    target: `http://127.0.0.1:${apiPort}`,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ""),
  },
};

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  preview: { proxy },
});
