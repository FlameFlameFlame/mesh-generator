import { defineConfig } from 'vite';

const backendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:8000';

export default defineConfig({
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api/v2': {
        target: backendUrl,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
