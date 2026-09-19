import { StrictMode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Route, Router } from 'wouter'

import { chatApi } from '../chat/api'
import type { Conversation, ConversationAction, Message } from '../chat/state'
import { ChatPage } from './ChatPage'

vi.mock('../chat/api', () => ({ chatApi: { list: vi.fn(), get: vi.fn(), create: vi.fn(), send: vi.fn(), stop: vi.fn() } }))

class Socket {
  static instances: Socket[] = []
  onopen: (() => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  close = vi.fn()
  url: string
  constructor(url: string) { this.url = url; Socket.instances.push(this) }
  receive(action: ConversationAction) { act(() => this.onmessage?.({ data: JSON.stringify(action) })) }
}

const message: Message = { id: '10', role: 'user', body: 'Check the active runs', state: 'delivered', replyTo: null, toolCalls: [], isInternal: false, file: null, createdAt: '2026-09-18T10:24:00Z', deliveredAt: '2026-09-18T10:24:01Z' }
const conversation: Conversation = {
  id: '01995a3c-0000-7000-8000-000000000001', title: message.body, source: 'web', userId: null, userName: '', createdAt: message.createdAt,
  messageCount: 1, activeMessageId: '10', messages: [message],
}

function mount(path = `/chat/${conversation.id}`) {
  window.history.replaceState(null, '', path)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<StrictMode><QueryClientProvider client={client}><Router>
    <Route path="/chat/:id?">{(params) => <ChatPage id={params.id} />}</Route>
  </Router></QueryClientProvider></StrictMode>)
  return client
}

async function connected() {
  await waitFor(() => expect(Socket.instances.length).toBeGreaterThan(0))
  const socket = Socket.instances.at(-1)!
  act(() => socket.onopen?.())
  return socket
}

beforeEach(() => {
  Socket.instances = []
  vi.stubGlobal('WebSocket', Socket)
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.mocked(chatApi.list).mockResolvedValue([conversation])
  vi.mocked(chatApi.get).mockResolvedValue(conversation)
  vi.mocked(chatApi.send).mockResolvedValue({ ...message, id: '11', body: 'Then check failures', state: 'pending' })
  vi.mocked(chatApi.create).mockResolvedValue(conversation)
  vi.mocked(chatApi.stop).mockResolvedValue(undefined)
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('Chat page', () => {
  it('keeps conversations in date order after a live snapshot', async () => {
    vi.mocked(chatApi.list).mockResolvedValue([
      conversation,
      { ...conversation, id: '01995a3c-0000-7000-8000-000000000002', title: 'Previous day', createdAt: '2026-09-17T16:00:00Z' },
    ])
    mount()
    const socket = await connected()
    socket.receive({ type: 'snapshot', ...conversation })
    const links = within(screen.getByRole('complementary', { name: 'Conversations' })).getAllByRole('link')
    expect(links.map((link) => link.getAttribute('href'))).toEqual(['/chat/new', `/chat/${conversation.id}`, '/chat/01995a3c-0000-7000-8000-000000000002', '/apps/chat/settings/channels'])
  })

  it('shows an unnamed conversation as New conversation', async () => {
    const unnamed = { ...conversation, title: null }
    vi.mocked(chatApi.list).mockResolvedValue([unnamed])
    vi.mocked(chatApi.get).mockResolvedValue(unnamed)
    mount()
    await connected()
    const list = within(screen.getByRole('complementary', { name: 'Conversations' }))
    expect(list.getAllByRole('link').find((link) => link.getAttribute('href') === `/chat/${conversation.id}`)?.textContent).toContain('New conversation')
    expect(screen.getByRole('heading', { name: 'New conversation' })).toBeTruthy()
  })

  it('creates a conversation only from its first message', async () => {
    vi.mocked(chatApi.list).mockResolvedValue([])
    mount('/chat/new')
    expect(chatApi.create).not.toHaveBeenCalled()
    expect(screen.queryByText('No messages yet')).toBeNull()
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), { target: { value: message.body } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(chatApi.create).toHaveBeenCalledExactlyOnceWith(message.body))
    await connected()
    expect(window.location.pathname).toBe(`/chat/${conversation.id}`)
    expect(screen.getByRole('heading', { name: message.body })).toBeTruthy()
    expect(screen.getByText('Only you can see these conversations.')).toBeTruthy()
    expect(screen.queryByText('Private conversation')).toBeNull()
  })

  it('saves another message while the agent replies and keeps Send beside Stop', async () => {
    mount()
    await connected()
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Then check failures' } })
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
    expect(await screen.findByText('Queued')).toBeTruthy()
    expect(chatApi.send).toHaveBeenCalledExactlyOnceWith(conversation.id, 'Then check failures')
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(chatApi.stop).toHaveBeenCalledExactlyOnceWith(conversation.id, message.id))
    expect(screen.getByText('Agent is replying')).toBeTruthy()
  })

  it('names a batched WhatsApp turn by its newest delivered message', async () => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, source: 'whatsapp', messages: [
      message,
      { ...message, id: '11', body: 'And the failures' },
      { ...message, id: '12', body: 'Later question', state: 'pending', deliveredAt: null },
    ] })
    mount()
    await connected()
    const turns = screen.getAllByRole('article')
    expect(within(turns[0]!).queryByRole('status')).toBeNull()
    expect(within(turns[1]!).getByRole('status').textContent).toBe('Agent is replying…')
    expect(within(turns[2]!).getByText('Queued')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(chatApi.stop).toHaveBeenCalledExactlyOnceWith(conversation.id, '11'))
  })

  it('shows connection progress at the next reply without infrastructure copy', async () => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, activeMessageId: null, messages: [{ ...message, state: 'pending' }] })
    mount()
    await connected()
    const status = screen.getByText('Connecting…')
    expect(status.closest('.chat-reply')).toBeTruthy()
    expect(screen.queryByText(/get the sandbox|start the agent|No messages yet/i)).toBeNull()
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(chatApi.stop).toHaveBeenCalledExactlyOnceWith(conversation.id, message.id))
  })

  it('streams plain agent text and expandable tool input and output without row times', async () => {
    mount()
    const socket = await connected()
    const emit = (sequence: number, update: Extract<ConversationAction, { type: 'event' }>['notification']['update']) => socket.receive({ type: 'event', messageId: '10', epoch: 'sandbox', sequence, notification: { sessionId: 'agent', update } })
    emit(1, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'I will check the gates.' } })
    emit(2, { sessionUpdate: 'tool_call', toolCallId: 'gate', title: 'Read the approval gate', rawInput: { run: 'example' }, status: 'in_progress' })
    emit(3, { sessionUpdate: 'tool_call_update', toolCallId: 'gate', status: 'completed', rawOutput: { gate: 'approval' } })
    emit(4, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'One run needs approval.' } })
    const first = screen.getByText('I will check the gates.').closest('.markdown-content')
    const last = screen.getByText('One run needs approval.').closest('.markdown-content')
    expect(first?.className).toBe(last?.className)
    const tool = screen.getByText('Read the approval gate').closest('details')!
    fireEvent.click(tool.querySelector('summary')!)
    expect(tool.hasAttribute('open')).toBe(true)
    expect(within(tool).getByText(/"run": "example"/)).toBeTruthy()
    expect(within(tool).getByText(/"gate": "approval"/)).toBeTruthy()
    expect(tool.querySelector('time')).toBeNull()
  })

  it.each(['interrupted', 'cancelled'] as const)('resends a %s turn only after Send again and preserves the current draft', async (state) => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, activeMessageId: null, messages: [{ ...message, state, deliveredAt: null }] })
    mount()
    await connected()
    expect(screen.getByText(state === 'cancelled' ? 'Cancelled' : 'Interrupted')).toBeTruthy()
    expect(chatApi.send).not.toHaveBeenCalled()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Keep this draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send again' }))
    await waitFor(() => expect(chatApi.send).toHaveBeenCalledExactlyOnceWith(conversation.id, message.body))
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Keep this draft')
  })

  it('reconnects the stream without resending a saved message or duplicating text', async () => {
    mount()
    const first = await connected()
    const chunk: ConversationAction = { type: 'event', messageId: '10', epoch: 'sandbox', sequence: 1, notification: { sessionId: 'agent', update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'One reply.' } } } }
    first.receive(chunk)
    vi.useFakeTimers()
    act(() => first.onclose?.({ code: 1006 }))
    expect(screen.getByText('Reconnecting…')).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    const second = Socket.instances.at(-1)!
    expect(second).not.toBe(first)
    act(() => second.onopen?.())
    second.receive(chunk)
    expect(screen.getAllByText('One reply.')).toHaveLength(1)
    expect(chatApi.send).not.toHaveBeenCalled()
  })

  it('pairs each reply with its user message by replyTo when replies arrive out of order', async () => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, activeMessageId: null, messages: [
      { ...message, state: 'replied' },
      { ...message, id: '11', body: 'Next request', state: 'replied' },
      { ...message, id: '13', role: 'assistant', body: 'Second answer', state: null, replyTo: '11', deliveredAt: null },
      { ...message, id: '12', role: 'assistant', body: 'First answer', state: null, replyTo: '10', deliveredAt: null },
    ] })
    mount()
    await connected()
    const turns = screen.getAllByRole('article')
    expect(turns[0]?.textContent).toContain('First answer')
    expect(turns[0]?.textContent).not.toContain('Second answer')
    expect(turns[1]?.textContent).toContain('Next request')
    expect(turns[1]?.textContent).toContain('Second answer')
  })

  it('keeps the draft after a failed send and ignores Enter during IME composition', async () => {
    mount()
    await connected()
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: 'Keep my message' } })
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(chatApi.send).not.toHaveBeenCalled()
    vi.mocked(chatApi.send).mockRejectedValueOnce(new Error('Message was not saved.'))
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect((input as HTMLTextAreaElement).value).toBe('Keep my message')
  })

  it('keeps a cancelled partial reply with its turn and skips cancelled pending messages', async () => {
    vi.mocked(chatApi.get).mockResolvedValue({ ...conversation, activeMessageId: null, messages: [
      { ...message, state: 'cancelled' },
      { ...message, id: '11', body: 'Do not send this', state: 'cancelled', deliveredAt: null },
      { ...message, id: '12', body: 'Continue', state: 'replied' },
      { ...message, id: '13', role: 'assistant', body: 'Partial answer', state: null, replyTo: '10', deliveredAt: null },
      { ...message, id: '14', role: 'assistant', body: 'Next answer', state: null, replyTo: '12', deliveredAt: null },
    ] })
    mount()
    await connected()
    const turns = screen.getAllByRole('article')
    expect(turns[0]?.textContent).toContain('Partial answer')
    expect(turns[1]?.textContent).not.toContain('answer')
    expect(turns[2]?.textContent).toContain('Next answer')
    expect(screen.getAllByRole('button', { name: 'Send again' })).toHaveLength(2)
    expect(chatApi.send).not.toHaveBeenCalled()
  })
})
