import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const BACKEND = 'http://127.0.0.1:8080'

// `base: './'` so the built bundle works when the Python server mounts
// ui/dist at an arbitrary path.
export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      // SSE flows through here; http-proxy streams it without buffering.
      '/api': { target: BACKEND, changeOrigin: true, ws: true },
      '/v1': { target: BACKEND, changeOrigin: true },
    },
  },
})
