import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const apiTarget = process.env.VITE_API_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      // decimal.js-light's "browser" field points at a CJS UMD build, which Vite
      // picks by default (mainFields = ['browser', 'module', ...]). Rollup's CJS
      // interop then leaves `default` undefined, breaking recharts'
      // `new Decimal(...)` calls inside tick computation. Force the ESM build.
      'decimal.js-light': 'decimal.js-light/decimal.mjs',
    },
  },
  server: {
    proxy: {
      '/api/events': {
        target: apiTarget,
        headers: { Connection: '' },
      },
      '/api': apiTarget,
    },
  },
})
