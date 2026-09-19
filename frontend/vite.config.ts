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
    // Pin the zone. The backend stores naive UTC and the app re-appends the `Z`
    // to render the viewer's local day, so a test that asserts an instant or a
    // date is only reproducible against a known offset — `2026-09-16T12:00:00`
    // is already the 17th in UTC+13, and an elapsed counter asserted in the
    // machine's own zone passes or fails depending on where the laptop is.
    env: { TZ: 'UTC' },
  },
  server: {
    proxy: {
      // Local dev: Vite on :5173 talks to the FastAPI backend on $PORT.
      //
      // PORT is the backend's port, not this server's — `make dev-api` binds
      // it, and Vite itself does not read PORT, so the dev server stays on
      // 5173. Without this, `PORT=8899 make dev-api` left the proxy pointing at
      // a port with nothing behind it.
      '/api': `http://localhost:${process.env.PORT ?? 8000}`,
    },
  },
})
