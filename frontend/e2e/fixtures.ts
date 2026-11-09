import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

import { test as base, expect, type APIRequestContext, type Page } from '@playwright/test'

import type { Conversation } from '../src/chat/state'
import { startAnthropic, textOf, TOKEN, USER_MARKER, type Anthropic } from './anthropic'

export { expect }
export { Gate, TITLE } from './anthropic'

/** The compose project that scripts/install.sh made, with the test CA in its e2e folder. */
export const installDir = process.env.DRUKS_INSTALL_DIR ?? join(homedir(), 'druks')

declare global {
  interface Window {
    reportRenderViolation: (detail: string) => void
  }
}

// U<n> marks a turn's message and A<n> starts its reply, so a marker gives its place on the page.
const MARKER = /\b[UA]\d+-[a-z0-9]+\b/g

export const test = base.extend<{ violations: Set<string> }, { anthropic: Anthropic }>({
  // Playwright reads the fixtures that a fixture needs from this pattern, also when it is empty.
  // eslint-disable-next-line no-empty-pattern
  anthropic: [async ({}, provide) => {
    const certificates = join(installDir, 'e2e')
    const anthropic = await startAnthropic({
      cert: readFileSync(join(certificates, 'api.anthropic.com.pem'), 'utf8'),
      key: readFileSync(join(certificates, 'api.anthropic.com.key'), 'utf8'),
    })
    await provide(anthropic)
    await anthropic.close()
  }, { scope: 'worker' }],
  violations: async ({ page }, provide) => {
    const violations = new Set<string>()
    await page.exposeFunction('reportRenderViolation', (detail: string) => violations.add(detail))
    await page.addInitScript(watchMessages, MARKER.source)
    await provide(violations)
  },
})

/** The markers of a test's turns. Each test owns its nonce, so tests share no state. */
export function markTurns(count: number) {
  const nonce = Math.random().toString(36).slice(2, 8)
  return Array.from({ length: count }, (_, index) => ({ user: `U${index + 1}-${nonce}`, reply: `A${index + 1}-${nonce}` }))
}

export async function send(page: Page, body: string) {
  await page.getByRole('textbox', { name: 'Message', exact: true }).fill(body)
  await page.getByRole('button', { name: 'Send', exact: true }).click()
}

/** Open a new conversation with its first message, and return its id. */
export async function startConversation(page: Page, body: string): Promise<string> {
  await page.goto('/chat/new')
  await send(page, body)
  await page.waitForURL(/\/chat\/[0-9a-f-]{36}$/)
  return new URL(page.url()).pathname.split('/').at(-1)!
}

export async function getConversation(request: APIRequestContext, id: string): Promise<Conversation> {
  const response = await request.get(`/api/chat/conversations/${id}`)
  expect(response.status()).toBe(200)
  return response.json()
}

export function compose(...args: string[]) {
  execFileSync('docker', ['compose', ...args], { cwd: installDir, stdio: 'inherit' })
}

/** The running boxes that drukbox's docker provider made. */
export function listBoxes(): string[] {
  const output = execFileSync('docker', ['ps', '--quiet', '--filter', 'label=managed-by=drukbox'], { encoding: 'utf8' })
  return output.split('\n').filter(Boolean)
}

/** Runs in the page. On every DOM change, it reports a marker that shows twice or out of order. */
function watchMessages(pattern: string) {
  const rank = (marker: string) => Number(marker.slice(1, marker.indexOf('-'))) * 2 + Number(marker[0] === 'A')
  new MutationObserver(() => {
    const log = document.querySelector<HTMLElement>('[role="log"][aria-label="Messages"]')
    const markers: string[] = log?.innerText.match(new RegExp(pattern, 'g')) ?? []
    markers.forEach((marker, index) => {
      if (markers.indexOf(marker) !== index) window.reportRenderViolation(`${marker} shows twice`)
      else if (index > 0 && rank(markers[index - 1]) > rank(marker)) window.reportRenderViolation(`${marker} shows below ${markers[index - 1]}`)
    })
  }).observe(document, { childList: true, subtree: true, characterData: true })
}

const normalize = (text: string) => text.replace(/\s+/g, ' ').trim()

/** The page shows each saved message once and in order. Each reply is what the mock streamed for
 * its turn. The second turn's request carries the first turn. Every request carries the real token. */
export async function expectChatFacts(
  { page, request, anthropic, violations }: { page: Page, request: APIRequestContext, anthropic: Anthropic, violations: Set<string> },
  id: string,
) {
  const log = page.getByRole('log', { name: 'Messages' })
  await expect.poll(async () => {
    const { messages } = await getConversation(request, id)
    const text = normalize(await log.innerText())
    let position = -1
    return messages.flatMap((message) => {
      const body = normalize(message.body)
      const count = text.split(body).length - 1
      if (count !== 1) return [`${message.role} "${body}" shows ${count} times`]
      if (text.indexOf(body) < position) return [`${message.role} "${body}" shows above an older message`]
      position = text.indexOf(body)
      return []
    })
  }, { message: 'each saved message shows once, in order' }).toEqual([])
  expect([...violations], 'no marker showed twice or out of order').toEqual([])

  const conversation = await getConversation(request, id)
  const turns = conversation.messages.filter((message) => message.role === 'user').map((message) => {
    const marker = message.body.match(USER_MARKER)![0]
    const requests = anthropic.completions.filter((completion) => completion.marker === marker)
    expect(requests, `one model request for ${marker}`).toHaveLength(1)
    const reply = conversation.messages.find((saved) => saved.replyTo === message.id)
    expect(reply?.body, `the reply to ${marker} is what the mock streamed`).toBe(requests[0].streamed.join(''))
    return { message, reply: reply!, request: requests[0] }
  })

  const [first, second] = turns
  const history = second.request.messages.slice(0, -1).map(textOf).join('\n')
  expect(history, 'the second turn carries the first message').toContain(first.message.body)
  expect(history, 'the second turn carries the first reply').toContain(first.reply.body)

  const authorizations = new Set(anthropic.completions.map((completion) => completion.authorization))
  expect([...authorizations], 'every model request carries the real token').toEqual([`Bearer ${TOKEN}`])
}
