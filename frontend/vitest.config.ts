import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

import { shellAlias } from './vite.config'

export default defineConfig({
  plugins: [react()],
  resolve: { alias: shellAlias },
  test: {
    environment: 'jsdom',
    globals: false,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
