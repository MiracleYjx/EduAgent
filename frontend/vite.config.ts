import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const backendOrigin = "http://127.0.0.1:8000";

function sameOriginProxy() {
  return {
    target: backendOrigin,
    changeOrigin: true,
    configure: (proxy: unknown) => {
      // The Vite server is a trusted loopback development proxy. Rewrite the
      // browser Origin to the exact backend origin instead of widening the
      // backend's Host/Origin allow-list or enabling wildcard CORS.
      const server = proxy as {
        on: (
          event: "proxyReq",
          listener: (request: {
            setHeader: (name: string, value: string) => void;
          }) => void,
        ) => void;
      };
      server.on("proxyReq", (request) => {
        request.setHeader("origin", backendOrigin);
      });
    },
  };
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    allowedHosts: ["127.0.0.1", "localhost"],
    cors: false,
    proxy: {
      "/api": sameOriginProxy(),
      "/health": sameOriginProxy(),
      "/setup-ready": sameOriginProxy(),
      "/ready": sameOriginProxy(),
      "/openapi.json": sameOriginProxy(),
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  test: {
    // Keep DOM-heavy AntD files from competing for the same test timeout budget.
    maxWorkers: 1,
    fileParallelism: false,
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: true,
    clearMocks: true,
    restoreMocks: true,
  },
});
