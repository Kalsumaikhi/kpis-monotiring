/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import type { ProxyOptions } from 'vite'

// The backend answers 401 with WWW-Authenticate: Basic, which makes browsers open their own
// credentials dialog over the custom login page. Strip it, same as nginx does in production.
const api: ProxyOptions = {
  target: 'http://localhost:8000',
  configure: (proxy) => {
    proxy.on('proxyRes', (res) => {
      delete res.headers['www-authenticate']
    })
  },
}

export default defineConfig({
  plugins: [vue()],
  server: { proxy: { '/api': api } },
  preview: { proxy: { '/api': api } },
  test: { environment: 'jsdom', globals: false },
})
