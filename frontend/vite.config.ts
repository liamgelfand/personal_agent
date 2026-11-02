import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In dev, the UI runs on :5173 and proxies API + WebSocket calls to the Python server on :8000.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', ws: true },
    },
  },
})
