import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    // Node is enough: the only unit tests are for the hand-rolled SSE parser.
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
  server: {
    proxy: {
      // Local dev: Vite on :5173 talks to the FastAPI backend on :8000.
      '/api': 'http://localhost:8000',
    },
  },
})
