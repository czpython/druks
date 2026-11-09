import { expect, test as setup } from '@playwright/test'

import { TOKEN } from './anthropic'

// Druks checks a setup token's prefix only, so the fake token needs no network call.
setup('the test account signs in to Anthropic', async ({ request }) => {
  const response = await request.post('/api/providers/anthropic/sign-in/token', { data: { token: TOKEN } })
  expect(response.status(), await response.text()).toBe(200)
})
