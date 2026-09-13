import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The API binds 127.0.0.1 only and refuses non-loopback callers, so the dev
// server proxies to it rather than the browser calling it cross-origin.
// That also keeps the operator bearer token out of any CORS preflight path.
const API = 'http://127.0.0.1:8080';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/v1': { target: API, changeOrigin: false },
      '/healthz': { target: API, changeOrigin: false },
    },
  },
});
