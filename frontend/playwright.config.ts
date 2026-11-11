import { defineConfig } from '@playwright/test'

// The suite drives a stack that scripts/install.sh made, with only the model faked. See
// docs/development.md.
export default defineConfig({
  testDir: 'e2e',
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 180_000,
  // A first turn boots the account's box.
  expect: { timeout: 60_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    // The endpoint of a local install: the chat socket accepts this origin only.
    baseURL: 'http://127.0.0.1:8001',
    // The edge identity that druks.toml names in identity.header.
    extraHTTPHeaders: { 'X-Forwarded-Email': 'e2e@druks.test' },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'sign-in', testMatch: /\.setup\.ts$/ },
    { name: 'chat', dependencies: ['sign-in'] },
  ],
})
