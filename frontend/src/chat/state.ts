import type { PlanEntry, SessionNotification, SessionUpdate, ToolCall } from '@agentclientprotocol/sdk'

import type { FileSummary } from '../api/types'

export interface ConversationSummary {
  id: string
  title: string | null
  isPinned: boolean
  source: 'web' | 'whatsapp' | 'slack' | 'github'
  userId: string | null
  userName: string
  createdAt: string
  lastMessageAt: string
  messageCount: number
  activeMessageId: string | null
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  body: string
  state: 'pending' | 'delivered' | 'replied' | 'interrupted' | 'cancelled' | null
  replyTo: string | null
  createdAt: string
  deliveredAt: string | null
  toolCalls: Array<ToolCall & { textOffset: number }>
  /** Druks wrote this message for the agent. The person on WhatsApp never sees it. */
  isInternal: boolean
  file: FileSummary | null
  /** What the person said in the message's voice note. */
  transcript: string
}

export interface Conversation extends ConversationSummary {
  messages: Message[]
}

export type ReplyRow =
  | { type: 'text'; text: string }
  | { type: 'tool'; call: ToolCall }
  | { type: 'plan'; entries: PlanEntry[] }

interface LiveTurn {
  epoch: string
  sequence: number
  rows: ReplyRow[]
}

export interface ConversationState {
  conversation: Conversation | null
  turns: Record<string, LiveTurn>
  error: { detail: string } | null
}

export type ConversationAction =
  | ({ type: 'snapshot' } & Conversation)
  | { type: 'event'; messageId: string; sequence: number; epoch: string; notification: SessionNotification }
  | { type: 'error'; detail: string }
  | { type: 'saved'; message: Message }

export const initialConversation: ConversationState = { conversation: null, turns: {}, error: null }

// A WhatsApp conversation has no title, so the person's name stands in.
export function conversationTitle(conversation: ConversationSummary): string {
  return conversation.title ?? (conversation.userName || conversation.userId || 'New conversation')
}

export function compareConversations(left: ConversationSummary, right: ConversationSummary): number {
  return Date.parse(right.lastMessageAt) - Date.parse(left.lastMessageAt) || right.id.localeCompare(left.id)
}

export function replyRows(rows: ReplyRow[], update: SessionUpdate): ReplyRow[] {
  switch (update.sessionUpdate) {
    case 'agent_message_chunk': {
      if (update.content.type !== 'text') return rows
      const last = rows.at(-1)
      if (last?.type === 'text') {
        return [...rows.slice(0, -1), { type: 'text', text: last.text + update.content.text }]
      }
      return [...rows, { type: 'text', text: update.content.text }]
    }
    case 'tool_call':
      return [...rows, { type: 'tool', call: update }]
    case 'tool_call_update': {
      const changes = Object.fromEntries(Object.entries(update).filter(([key, value]) =>
        value != null || key === 'rawInput' || key === 'rawOutput',
      ))
      return rows.map((row) => row.type === 'tool' && row.call.toolCallId === update.toolCallId
        ? { ...row, call: { ...row.call, ...changes } }
        : row)
    }
    case 'plan': {
      const plan: ReplyRow = { type: 'plan', entries: update.entries }
      return rows.some((row) => row.type === 'plan')
        ? rows.map((row) => row.type === 'plan' ? plan : row)
        : [...rows, plan]
    }
    default:
      return rows
  }
}

export function savedReplyRows(message: Message): ReplyRow[] {
  const rows: ReplyRow[] = []
  const characters = Array.from(message.body)
  let position = 0
  for (const call of message.toolCalls) {
    if (call.textOffset > position) {
      rows.push({ type: 'text', text: characters.slice(position, call.textOffset).join('') })
    }
    rows.push({ type: 'tool', call })
    position = call.textOffset
  }
  if (position < characters.length) rows.push({ type: 'text', text: characters.slice(position).join('') })
  return rows
}

export function conversationReducer(state: ConversationState, action: ConversationAction): ConversationState {
  switch (action.type) {
    case 'snapshot': {
      const turns = Object.fromEntries(Object.entries(state.turns).filter(([id]) =>
        action.messages.some((message) => message.id === id && message.state !== 'replied' && message.state !== 'cancelled'),
      ))
      return { conversation: action, turns, error: null }
    }
    case 'saved': {
      const conversation = state.conversation
      if (!conversation || conversation.messages.some((message) => message.id === action.message.id)) return state
      return { ...state, conversation: {
        ...conversation,
        messages: [...conversation.messages, action.message].sort((left, right) =>
          Date.parse(left.createdAt) - Date.parse(right.createdAt) || left.id.localeCompare(right.id),
        ),
        messageCount: conversation.messageCount + 1,
      } }
    }
    case 'error':
      return { ...state, error: { detail: action.detail } }
    case 'event': {
      if (state.conversation?.messages.some((message) => message.id === action.messageId && (message.state === 'replied' || message.state === 'cancelled'))) return state
      const previous = state.turns[action.messageId]
      const sameEpoch = previous?.epoch === action.epoch
      if (sameEpoch && previous.sequence >= action.sequence) return state
      const rows = replyRows(sameEpoch ? previous.rows : [], action.notification.update)
      return { ...state, error: null, turns: { ...state.turns, [action.messageId]: {
        epoch: action.epoch, sequence: action.sequence, rows,
      } } }
    }
  }
}
