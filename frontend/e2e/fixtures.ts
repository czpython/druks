import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { createServer } from 'node:https'
import { homedir } from 'node:os'
import { join } from 'node:path'

import { test as base, expect, type APIRequestContext, type Page } from '@playwright/test'

import type { Conversation } from '../src/chat/state'
import { answerAnthropic } from './anthropic'
import { Model, USER_MARKER } from './model'
import { answerOpenAi } from './openai'

export { expect }
export { Gate, TITLE } from './model'

/** The compose project that scripts/install.sh made, with the test CA in its e2e folder. */
export const installDir = process.env.DRUKS_INSTALL_DIR ?? join(homedir(), 'druks')

declare global {
  interface Window {
    reportRenderViolation: (detail: string) => void
  }
}

/** The harness, model and billing of the Chat agent. Null inherits the installation default. */
interface Agent {
  harness: string | null
  model: string | null
  billing: string | null
}

// U<n> marks a turn's message and A<n> starts its reply, so a marker gives its place on the page.
const MARKER = /\b[UA]\d+-[a-z0-9]+\b/g

export const test = base.extend<
  { agent: Agent, agentSettings: void, violations: Set<string> },
  { anthropic: Model, openai: Model, mock: void }
>({
  // Playwright reads the fixtures that a fixture needs from this pattern, also when it is empty.
  // eslint-disable-next-line no-empty-pattern
  anthropic: [async ({}, provide) => provide(new Model()), { scope: 'worker' }],
  // eslint-disable-next-line no-empty-pattern
  openai: [async ({}, provide) => provide(new Model()), { scope: 'worker' }],
  // One HTTPS server answers for both hosts: they resolve to one address, and the proxy dials
  // port 443 for each.
  mock: [async ({ anthropic, openai }, provide) => {
    const certificates = join(installDir, 'e2e')
    const server = createServer({
      cert: readFileSync(join(certificates, 'mock.pem'), 'utf8'),
      key: readFileSync(join(certificates, 'mock.key'), 'utf8'),
    }, (request, response) => {
      const host = request.headers.host
      console.log(`${host}: ${request.method} ${request.url}`)
      const answer = host === 'api.openai.com' ? answerOpenAi(openai, request, response) : answerAnthropic(anthropic, request, response)
      answer.catch((error) => {
        console.error(`${host}:`, error)
        response.destroy()
      })
    })
    await new Promise<void>((resolve, reject) => {
      server.once('error', reject)
      // Every interface: a container reaches this process at the Docker bridge address on Linux,
      // and through the host address of Docker Desktop on macOS.
      server.listen(443, '0.0.0.0', resolve)
    })
    await provide()
    server.closeAllConnections()
    await new Promise((resolve) => server.close(resolve))
  }, { scope: 'worker', auto: true }],
  agent: [{ harness: null, model: null, billing: null }, { option: true }],
  // Each test sets the Chat agent, so no test runs on the harness of the test before it.
  agentSettings: [async ({ request, agent }, provide) => {
    const response = await request.patch('/api/settings/apps', {
      data: {
        agentHarnesses: { 'chat.bot': agent.harness },
        agentModels: { 'chat.bot': agent.model },
        agentBillings: { 'chat.bot': agent.billing },
      },
    })
    expect(response.status(), await response.text()).toBe(200)
    await provide()
  }, { auto: true }],
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

/** The page shows each saved message once and in order. Each reply is what the model streamed for
 * its turn. The second turn's request carries the first turn. Each request that quotes a turn
 * carries the real credential. */
export async function expectChatFacts(
  { page, request, violations }: { page: Page, request: APIRequestContext, violations: Set<string> },
  { id, model, credential }: { id: string, model: Model, credential: string },
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
    const requests = model.completions.filter((completion) => completion.marker === marker)
    expect(requests, `one model request for ${marker}`).toHaveLength(1)
    const reply = conversation.messages.find((saved) => saved.replyTo === message.id)
    expect(reply?.body, `the reply to ${marker} is what the mock streamed`).toBe(requests[0].streamed.join(''))
    return { marker, message, reply: reply!, request: requests[0] }
  })

  const [first, second] = turns
  const history = second.request.messages.slice(0, -1).join('\n')
  expect(history, 'the second turn carries the first message').toContain(first.message.body)
  expect(history, 'the second turn carries the first reply').toContain(first.reply.body)

  // The mock serves every test of the worker. A side request, such as the name of the
  // conversation, quotes a turn's message, so the markers find this test's requests.
  const requests = model.completions.filter((completion) => turns.some(({ marker }) => completion.messages.some((text) => text.includes(marker))))
  const credentials = new Set(requests.map((completion) => completion.credential))
  expect([...credentials], 'every model request carries the real credential').toEqual([credential])
}
