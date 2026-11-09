import type { IncomingMessage, ServerResponse } from 'node:http'
import { createServer } from 'node:https'

/** The setup token the test account signs in with. The box holds only its placeholder. */
export const TOKEN = 'sk-ant-oat01-druks-e2e'
/** The fixed answer to the request that names a new conversation. */
export const TITLE = 'Druks end to end'

/** Holds a scripted stream at its place until the test opens it. */
export class Gate {
  open = () => {}
  readonly opened = new Promise<void>((resolve) => { this.open = resolve })
}

/** The text deltas of one reply, with gates between them. */
export type Script = (string | Gate)[]

interface Message {
  role: 'user' | 'assistant'
  content: string | { type: string, text?: string }[]
}

/** One Messages API request and the text deltas the mock streamed back. */
export interface Completion {
  /** The marker of the turn, or empty for a side request. */
  marker: string
  authorization: string
  messages: Message[]
  streamed: string[]
}

export interface Anthropic {
  completions: Completion[]
  /** Answer the turn whose message starts with the marker. */
  script: (marker: string, script: Script) => void
  close: () => Promise<void>
}

/** A test's message starts with U<turn>-<nonce>. */
export const USER_MARKER = /^U\d+-[a-z0-9]+\b/
// The start of NAMING_PROMPT in backend/druks/chat/constants.py.
const NAMING_PROMPT = 'Name this conversation'

function textsOf(message: Message): string[] {
  if (typeof message.content === 'string') return [message.content]
  return message.content.map((block) => block.text ?? '')
}

export function textOf(message: Message): string {
  return textsOf(message).join('\n')
}

/** The marker of the turn that a request with this last user message runs. History repeats
 * older markers, so only the last user message counts. A side request, such as the title of a
 * session, quotes the message inside its own text, so the marker must start a text block. */
function markerOf(prompt: Message): string {
  return textsOf(prompt).map((text) => text.match(USER_MARKER)?.[0]).find(Boolean) ?? ''
}

/** An HTTPS Messages API for api.anthropic.com. It logs every request and answers an unknown
 * path with 404. */
export async function startAnthropic({ cert, key }: { cert: string, key: string }): Promise<Anthropic> {
  const completions: Completion[] = []
  const scripts = new Map<string, Script>()

  async function answer(request: IncomingMessage, response: ServerResponse) {
    console.log(`anthropic: ${request.method} ${request.url}`)
    const path = new URL(request.url ?? '/', 'https://api.anthropic.com').pathname
    if (request.method !== 'POST' || path !== '/v1/messages') {
      return sendError(response, 404, 'not_found_error', `The mock does not serve ${request.method} ${path}.`)
    }
    const chunks: Buffer[] = []
    for await (const chunk of request) chunks.push(chunk)
    const body = JSON.parse(Buffer.concat(chunks).toString())
    if (!body.stream) return sendError(response, 400, 'invalid_request_error', 'The mock answers streaming requests only.')
    const messages: Message[] = body.messages
    const prompt = messages.findLast((message) => message.role === 'user')!
    const marker = markerOf(prompt)
    const script = marker ? scripts.get(marker) : [textOf(prompt).includes(NAMING_PROMPT) ? TITLE : 'OK']
    if (!script) return sendError(response, 400, 'invalid_request_error', `No script for ${marker}.`)
    const completion: Completion = { marker, authorization: request.headers.authorization ?? '', messages, streamed: [] }
    completions.push(completion)
    await stream(response, body.model, script, completion)
  }

  const server = createServer({ cert, key }, (request, response) => {
    answer(request, response).catch((error) => {
      console.error('anthropic:', error)
      response.destroy()
    })
  })
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    // Every interface: a container reaches this process at the Docker bridge address on Linux,
    // and through the host address of Docker Desktop on macOS.
    server.listen(443, '0.0.0.0', resolve)
  })
  return {
    completions,
    script: (marker, script) => { scripts.set(marker, script) },
    close: () => new Promise((resolve) => {
      server.closeAllConnections()
      server.close(() => resolve())
    }),
  }
}

function sendError(response: ServerResponse, status: number, type: string, message: string) {
  response.writeHead(status, { 'content-type': 'application/json' })
  response.end(JSON.stringify({ type: 'error', error: { type, message } }))
}

async function stream(response: ServerResponse, model: string, script: Script, completion: Completion) {
  const closed = new Promise<void>((resolve) => response.once('close', resolve))
  // A Stop hangs up mid-stream, and the rest of the script goes nowhere.
  function send(type: string, data: object) {
    if (response.destroyed) return false
    response.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`)
    return true
  }
  response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
  send('message_start', {
    message: {
      id: `msg_e2e_${Math.random().toString(36).slice(2)}`,
      type: 'message',
      role: 'assistant',
      model,
      content: [],
      stop_reason: null,
      stop_sequence: null,
      usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    },
  })
  send('content_block_start', { index: 0, content_block: { type: 'text', text: '' } })
  for (const chunk of script) {
    if (chunk instanceof Gate) await Promise.race([chunk.opened, closed])
    else if (send('content_block_delta', { index: 0, delta: { type: 'text_delta', text: chunk } })) completion.streamed.push(chunk)
  }
  send('content_block_stop', { index: 0 })
  send('message_delta', { delta: { stop_reason: 'end_turn', stop_sequence: null }, usage: { output_tokens: completion.streamed.length } })
  send('message_stop', {})
  response.end()
}
