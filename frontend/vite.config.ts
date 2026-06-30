/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// The SPA is served BY the FastAPI app from src/plex_manager/web/static (ADR-0009),
// so the build emits there (outDir is resolved relative to this config's dir). In
// dev, Vite proxies the API to the running backend.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../src/plex_manager/web/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
})
