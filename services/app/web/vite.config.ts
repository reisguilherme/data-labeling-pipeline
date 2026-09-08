import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwind from "@tailwindcss/vite";

const API = process.env.MST_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwind()],
  build: {
    // O backend serve daqui; `npm run build` já deixa tudo no lugar.
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API, changeOrigin: true },
    },
  },
});
