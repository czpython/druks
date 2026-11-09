import {
  compose, expect, expectChatFacts, Gate, getConversation, listBoxes, markTurns, send, startConversation, test, TITLE,
} from './fixtures'

test('two turns share one box, and a reload shows each message once', async ({ page, request, anthropic, violations }) => {
  const [first, second] = markTurns(2)
  anthropic.script(first.user, [`${first.reply} Hello`, ' from the first turn.'])
  anthropic.script(second.user, [`${second.reply} Hello`, ' again.'])
  const log = page.getByRole('log', { name: 'Messages' })

  const id = await startConversation(page, `${first.user} Say hello.`)
  await expect(log).toContainText(`${first.reply} Hello from the first turn.`)
  await expect(page.getByRole('heading', { name: TITLE })).toBeVisible()
  const boxes = listBoxes()
  expect(boxes).toHaveLength(1)

  await send(page, `${second.user} Say hello again.`)
  await expect(log).toContainText(`${second.reply} Hello again.`)
  expect(listBoxes()).toEqual(boxes)

  await page.reload()
  await expectChatFacts({ page, request, anthropic, violations }, id)
})

test('Stop keeps the streamed text, and the next turn runs', async ({ page, request, anthropic, violations }) => {
  const [first, second] = markTurns(2)
  anthropic.script(first.user, [`${first.reply} Counting`, new Gate()])
  anthropic.script(second.user, [`${second.reply} Still here.`])
  const log = page.getByRole('log', { name: 'Messages' })

  const id = await startConversation(page, `${first.user} Count to a thousand.`)
  await expect(log).toContainText(`${first.reply} Counting`)
  await page.getByRole('button', { name: 'Stop' }).click()
  await expect.poll(async () => {
    const { messages } = await getConversation(request, id)
    return messages.find((message) => message.body.startsWith(first.user))?.state
  }).toBe('cancelled')

  await send(page, `${second.user} Are you there?`)
  await expect(log).toContainText(`${second.reply} Still here.`)
  await expectChatFacts({ page, request, anthropic, violations }, id)
})

test('a turn that runs through a Druks restart shows its reply once', async ({ page, request, anthropic, violations }) => {
  const [first, second] = markTurns(2)
  const restarted = new Gate()
  anthropic.script(first.user, [`${first.reply} Before the restart,`, restarted, ' and after it.'])
  anthropic.script(second.user, [`${second.reply} Back again.`])
  const log = page.getByRole('log', { name: 'Messages' })

  const id = await startConversation(page, `${first.user} Keep talking.`)
  await expect(log).toContainText(`${first.reply} Before the restart,`)
  compose('restart', 'web')
  await expect.poll(() => request.get('/health').then((response) => response.ok(), () => false)).toBe(true)
  restarted.open()
  await expect(log).toContainText(`${first.reply} Before the restart, and after it.`)

  await send(page, `${second.user} Still there?`)
  await expect(log).toContainText(`${second.reply} Back again.`)
  await expectChatFacts({ page, request, anthropic, violations }, id)
})
