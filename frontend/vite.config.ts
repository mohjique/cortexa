import { defineConfig, searchForWorkspaceRoot } from "vite";
import react from "@vitejs/plugin-react";

// In dev, /api is proxied to the local FastAPI server so no CORS setup is needed.
// In production (Vercel), set VITE_API_URL to the deployed backend's URL.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The tour's example MRI is imported from the committed demo scans
    // (tutorial/tourCase.ts), outside this folder. Only that folder is added.
    fs: { allow: [searchForWorkspaceRoot(process.cwd()), "../classifier/demo_scans"] },
    proxy: {
      "/api": {
        target: process.env.CORTEXA_API_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
