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
      // Local dev: Vite on :5173 talks to the FastAPI backend on $PORT.
      //
      // PORT is the backend's port, not this server's — `make dev-api` and the
      // Docker image both bind it, and Vite itself does not read PORT, so the
      // dev server stays on 5173. Without this, `PORT=8899 make dev-api` left
      // the proxy pointing at a port with nothing behind it.
      '/api': `http://localhost:${process.env.PORT ?? 8000}`,
    },
  },
})
