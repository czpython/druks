import { expect, test as setup } from '@playwright/test'

import { API_KEY as ANTHROPIC_KEY, TOKEN } from './anthropic'
import { API_KEY as OPENAI_KEY } from './openai'

// Druks checks a setup token's prefix only, so the fake token needs no network call.
setup('the test account signs in to Anthropic', async ({ request }) => {
  const response = await request.post('/api/providers/anthropic/sign-in/token', { data: { token: TOKEN } })
  expect(response.status(), await response.text()).toBe(200)
})

// Codex runs on the OpenAI key, and OpenCode on the Anthropic key. Druks stores a key without
// a check. Its request for the provider's models fails, because the mock does not run yet.
setup('the installation has an API key for Anthropic and for OpenAI', async ({ request }) => {
  for (const [provider, key] of [['anthropic', ANTHROPIC_KEY], ['openai', OPENAI_KEY]]) {
    const response = await request.post(`/api/providers/${provider}/key`, { data: { key } })
    expect(response.status(), await response.text()).toBe(200)
  }
})
