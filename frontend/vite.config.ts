import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // Local dev: Vite on :5173 talks to the FastAPI backend on :8000.
      '/api': 'http://localhost:8000',
    },
  },
})
