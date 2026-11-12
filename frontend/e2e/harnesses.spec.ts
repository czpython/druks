import { API_KEY as ANTHROPIC_KEY } from './anthropic'
import { expect, expectChatFacts, markTurns, send, startConversation, test, TITLE } from './fixtures'
import { API_KEY as OPENAI_KEY } from './openai'

const harnesses = [
  { agent: { harness: 'codex', model: 'openai/gpt-5.5', billing: 'api_key' }, api: 'openai', credential: `Bearer ${OPENAI_KEY}` },
  { agent: { harness: 'opencode', model: 'anthropic/claude-sonnet-4-5', billing: 'api_key' }, api: 'anthropic', credential: ANTHROPIC_KEY },
] as const

for (const { agent, api, credential } of harnesses) {
  test.describe(agent.harness, () => {
    test.use({ agent })

    test('two turns, and a reload shows each message once', async ({ page, request, anthropic, openai, violations }) => {
      const model = { anthropic, openai }[api]
      const [first, second] = markTurns(2)
      model.script(first.user, [`${first.reply} Hello`, ' from the first turn.'])
      model.script(second.user, [`${second.reply} Hello`, ' again.'])
      const log = page.getByRole('log', { name: 'Messages' })

      const id = await startConversation(page, `${first.user} Say hello.`)
      await expect(log).toContainText(`${first.reply} Hello from the first turn.`)
      await expect(page.getByRole('heading', { name: TITLE })).toBeVisible()

      await send(page, `${second.user} Say hello again.`)
      await expect(log).toContainText(`${second.reply} Hello again.`)

      await page.reload()
      await expectChatFacts({ page, request, violations }, { id, model, credential })
    })
  })
}
