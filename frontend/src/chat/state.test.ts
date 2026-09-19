import type { SessionUpdate } from '@agentclientprotocol/sdk'
import { describe, expect, it } from 'vitest'

import { compareConversations, conversationReducer, initialConversation, replyRows, savedReplyRows, type Conversation, type ConversationAction } from './state'

const conversation: Conversation = {
  id: '01995a3c-0000-7000-8000-000000000001', title: 'Read the gate', source: 'web', userId: null, userName: '', createdAt: '2026-09-18T10:00:00Z',
  messageCount: 1, activeMessageId: '2',
  isPinned: false, lastMessageAt: '2026-09-18T10:00:00Z',
  messages: [{ id: '2', role: 'user', body: 'Read the gate', state: 'delivered', replyTo: null, toolCalls: [], isInternal: false, file: null, createdAt: '2026-09-18T10:00:00Z', deliveredAt: '2026-09-18T10:00:01Z' }],
}

function event(sequence: number, update: SessionUpdate, epoch = 'one'): ConversationAction {
  return { type: 'event', sequence, epoch, messageId: '2', notification: { sessionId: 'agent', update } }
}

describe('Chat events', () => {
  it('orders conversations by their latest message with a stable id tie-breaker', () => {
    const later = { ...conversation, id: '01995a3c-0000-7000-8000-000000000002' }
    const latest = { ...conversation, id: '01995a3c-0000-7000-8000-000000000003', lastMessageAt: '2026-09-19T10:00:00Z' }
    const pinned = { ...conversation, isPinned: true }
    expect([pinned, latest, later].sort(compareConversations).map((item) => item.id)).toEqual([latest.id, later.id, pinned.id])
  })

  it('keeps text and tool calls in order and merges partial tool updates', () => {
    let state = conversationReducer(initialConversation, { type: 'snapshot', ...conversation })
    state = conversationReducer(state, event(1, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'I will read it.' } }))
    state = conversationReducer(state, event(2, { sessionUpdate: 'tool_call', toolCallId: 'gate', title: 'Read the gate', rawInput: { run: 'run-one' }, status: 'pending' }))
    state = conversationReducer(state, event(3, { sessionUpdate: 'tool_call_update', toolCallId: 'gate', status: 'completed', rawOutput: { gate: 'approval' } }))
    state = conversationReducer(state, event(4, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'It needs ' } }))
    state = conversationReducer(state, event(5, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'approval.' } }))
    expect(state.turns['2']?.rows).toEqual([
      { type: 'text', text: 'I will read it.' },
      { type: 'tool', call: expect.objectContaining({ title: 'Read the gate', rawInput: { run: 'run-one' }, status: 'completed', rawOutput: { gate: 'approval' } }) },
      { type: 'text', text: 'It needs approval.' },
    ])
  })

  it('replaces the complete plan and keeps null tool titles unchanged', () => {
    let rows = replyRows([], { sessionUpdate: 'plan', entries: [{ content: 'Read', priority: 'high', status: 'pending' }] })
    rows = replyRows(rows, { sessionUpdate: 'plan', entries: [{ content: 'Report', priority: 'low', status: 'completed' }] })
    expect(rows).toEqual([{ type: 'plan', entries: [{ content: 'Report', priority: 'low', status: 'completed' }] }])
    rows = replyRows(rows, { sessionUpdate: 'tool_call', toolCallId: 'one', title: 'Read', rawInput: {} })
    rows = replyRows(rows, { sessionUpdate: 'tool_call_update', toolCallId: 'one', title: null, rawOutput: null })
    expect(rows.at(-1)).toEqual({ type: 'tool', call: expect.objectContaining({ title: 'Read', rawInput: {}, rawOutput: null }) })
    rows = replyRows(rows, { sessionUpdate: 'plan', entries: [] })
    expect(rows[0]).toEqual({ type: 'plan', entries: [] })
  })

  it('does not duplicate replayed chunks and resets the cursor for a new sandbox', () => {
    const chunk = event(1, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'Hello' } })
    let state = conversationReducer(initialConversation, { type: 'snapshot', ...conversation })
    state = conversationReducer(state, chunk)
    state = conversationReducer(state, { type: 'snapshot', ...conversation })
    expect(conversationReducer(state, chunk)).toBe(state)
    state = conversationReducer(state, event(1, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'New turn' } }, 'two'))
    expect(state.turns['2']?.rows).toEqual([{ type: 'text', text: 'New turn' }])
  })

  it.each(['replied', 'cancelled'] as const)('uses stored %s replies, ignores old events, and clears an error on the next snapshot', (messageState) => {
    const completed: Conversation = { ...conversation, activeMessageId: null, messages: conversation.messages.map((message) => ({ ...message, state: messageState })) }
    const state = conversationReducer(initialConversation, { type: 'snapshot', ...completed })
    expect(conversationReducer(state, event(1, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'duplicate' } }))).toBe(state)
    const failed = conversationReducer(state, { type: 'error', detail: 'Unavailable' })
    expect(conversationReducer(failed, { type: 'snapshot', ...completed }).error).toBeNull()
  })

  it('restores tool positions after non-BMP text and multiple tools at the same position', () => {
    expect(savedReplyRows({
      id: '4', role: 'assistant', state: null, replyTo: '2', createdAt: conversation.createdAt, body: '🌱 Read.Done.', deliveredAt: null,
      isInternal: false, file: null,
      toolCalls: [
        { toolCallId: 'one', title: 'Read', textOffset: 7 },
        { toolCallId: 'two', title: 'Check', textOffset: 7 },
      ],
    })).toEqual([
      { type: 'text', text: '🌱 Read.' },
      { type: 'tool', call: { toolCallId: 'one', title: 'Read', textOffset: 7 } },
      { type: 'tool', call: { toolCallId: 'two', title: 'Check', textOffset: 7 } },
      { type: 'text', text: 'Done.' },
    ])
  })
})
