import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    // `import events from '@mock/events.json'` reads the repo's mock/ folder (used in mock mode).
    alias: { '@mock': fileURLToPath(new URL('../mock', import.meta.url)) },
  },
  server: {
    // The backend's CORS allows exactly one origin, http://localhost:3000 (SEALED_DASHBOARD_ORIGIN).
    host: 'localhost',
    port: 3000,
    strictPort: true, // fail instead of silently moving to 3001, which the backend would refuse
    fs: { allow: ['..'] }, // let the dev server read ../mock
  },
})
