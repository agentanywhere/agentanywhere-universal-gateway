import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  base: process.env.VITE_BASE_PATH || '/',
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    // 127.0.0.1 so the URLs in the docs work: on macOS "localhost" makes Vite
    // listen on ::1 only, and http://127.0.0.1:3001 would not connect.
    host: '127.0.0.1',
    port: 3001,
    proxy: {
      '^/admin(/|$)': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '^/api/': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
