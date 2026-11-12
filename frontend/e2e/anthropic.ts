import type { IncomingMessage, ServerResponse } from 'node:http'

import { Gate, textsOf, type Completion, type Message, type Model, type Script } from './model'

/** The setup token that Claude runs on. The box holds only its placeholder. */
export const TOKEN = 'sk-ant-oat01-druks-e2e'
/** The API key that OpenCode runs on. The box holds only its placeholder. */
export const API_KEY = 'sk-ant-api03-druks-e2e'

/** The Messages API of api.anthropic.com. It answers an unknown path with 404. */
export async function answerAnthropic(model: Model, request: IncomingMessage, response: ServerResponse) {
  const path = new URL(request.url ?? '/', 'https://api.anthropic.com').pathname
  if (request.method !== 'POST' || path !== '/v1/messages') {
    return sendError(response, 404, 'not_found_error', `The mock does not serve ${request.method} ${path}.`)
  }
  const chunks: Buffer[] = []
  for await (const chunk of request) chunks.push(chunk)
  const body = JSON.parse(Buffer.concat(chunks).toString())
  if (!body.stream) return sendError(response, 400, 'invalid_request_error', 'The mock answers streaming requests only.')
  const messages: Message[] = body.messages
  const { marker, script } = model.pick(messages, body.tools)
  if (!script) return sendError(response, 400, 'invalid_request_error', `No script for ${marker}.`)
  const completion: Completion = {
    marker,
    // Claude sends its token as a bearer. An API key goes in x-api-key.
    credential: String(request.headers['x-api-key'] ?? request.headers.authorization),
    messages: messages.map((message) => textsOf(message).join('\n')),
    streamed: [],
  }
  model.completions.push(completion)
  await stream(response, body.model, script, completion)
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
