import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 3000,
    proxy: {
      // Все запросы /api/* — проксируем на FastAPI
      '/api/': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
