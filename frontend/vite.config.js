import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/auth': { target: 'http://localhost:8000', changeOrigin: true },
      '/chat': { target: 'http://localhost:8000', changeOrigin: true },
      '/users': { target: 'http://localhost:8000', changeOrigin: true },
      '/listings': { target: 'http://localhost:8000', changeOrigin: true },
      '/map': { target: 'http://localhost:8000', changeOrigin: true },
      '/safety': { target: 'http://localhost:8000', changeOrigin: true },
      '/scorecards': { target: 'http://localhost:8000', changeOrigin: true },
      '/healthz': { target: 'http://localhost:8000', changeOrigin: true },
      '/ping': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})