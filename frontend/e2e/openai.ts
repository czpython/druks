import type { IncomingMessage, ServerResponse } from 'node:http'

import { Gate, textsOf, type Completion, type Message, type Model, type Script } from './model'

/** The API key that Codex runs on. The box holds only its placeholder. */
export const API_KEY = 'sk-proj-druks-e2e'

/** The Responses API of api.openai.com. It answers an unknown path with 404. */
export async function answerOpenAi(model: Model, request: IncomingMessage, response: ServerResponse) {
  // Codex asks for a WebSocket first. A 426 makes it use HTTPS at once, where any other answer
  // costs it five retries.
  if (request.headers.upgrade) return sendError(response, 426, 'The mock answers over HTTPS only.')
  const path = new URL(request.url ?? '/', 'https://api.openai.com').pathname
  if (request.method !== 'POST' || path !== '/v1/responses') {
    return sendError(response, 404, `The mock does not serve ${request.method} ${path}.`)
  }
  const chunks: Buffer[] = []
  for await (const chunk of request) chunks.push(chunk)
  const body = JSON.parse(Buffer.concat(chunks).toString())
  if (!body.stream) return sendError(response, 400, 'The mock answers streaming requests only.')
  const messages: Message[] = body.input.filter((item: { type: string }) => item.type === 'message')
  const { marker, script } = model.pick(messages, body.tools)
  if (!script) return sendError(response, 400, `No script for ${marker}.`)
  const completion: Completion = {
    marker,
    credential: String(request.headers.authorization),
    messages: messages.map((message) => textsOf(message).join('\n')),
    streamed: [],
  }
  model.completions.push(completion)
  await stream(response, body.model, script, completion)
}

function sendError(response: ServerResponse, status: number, message: string) {
  response.writeHead(status, { 'content-type': 'application/json' })
  response.end(JSON.stringify({ error: { type: 'invalid_request_error', message, code: null } }))
}

async function stream(response: ServerResponse, model: string, script: Script, completion: Completion) {
  const closed = new Promise<void>((resolve) => response.once('close', resolve))
  // A Stop hangs up mid-stream, and the rest of the script goes nowhere.
  function send(type: string, data: object) {
    if (response.destroyed) return false
    response.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`)
    return true
  }
  const id = Math.random().toString(36).slice(2)
  const message = { type: 'message', id: `msg_e2e_${id}`, role: 'assistant', status: 'in_progress', content: [] }
  response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
  send('response.created', { response: { id: `resp_e2e_${id}`, model, status: 'in_progress' } })
  send('response.output_item.added', { output_index: 0, item: message })
  for (const chunk of script) {
    if (chunk instanceof Gate) await Promise.race([chunk.opened, closed])
    else if (send('response.output_text.delta', { item_id: message.id, output_index: 0, content_index: 0, delta: chunk })) completion.streamed.push(chunk)
  }
  const text = completion.streamed.join('')
  send('response.output_item.done', {
    output_index: 0,
    item: { ...message, status: 'completed', content: [{ type: 'output_text', text, annotations: [] }] },
  })
  send('response.completed', {
    response: {
      id: `resp_e2e_${id}`,
      model,
      status: 'completed',
      usage: { input_tokens: 1, output_tokens: completion.streamed.length, total_tokens: 1 + completion.streamed.length },
    },
  })
  response.end()
}
