import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const backend = 'http://127.0.0.1:8821';

export default defineConfig({
  base: '/preview/',
  plugins: [react()],
  server: {
    host: '127.0.0.1', port: 8830, strictPort: true,
    proxy: {
      // The backend only accepts the configured Origin on writes; the dev
      // server runs on a different port, so forward the backend's own origin.
      // changeOrigin also rewrites Host, which the backend needs to select the
      // reeldock_dev_local_8821 session cookie name.
      '/api': { target: backend, changeOrigin: true, headers: { Origin: backend } },
      '/oauth': { target: backend, changeOrigin: true, headers: { Origin: backend } },
      '/static': { target: backend, changeOrigin: true, headers: { Origin: backend } },
      '/licenses': { target: backend, changeOrigin: true, headers: { Origin: backend } },
      '/artwork': { target: backend, changeOrigin: true, headers: { Origin: backend } },
    },
  },
});
