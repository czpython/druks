import { getJSON, patchJSON, postJSON, postNoContent } from '../api/client'
import type { ActiveRun, Conversation, ConversationSummary, Message } from './state'

const conversations = '/api/chat/conversations'

export const chatApi = {
  list: () => getJSON<ConversationSummary[]>(conversations),
  get: (id: string) => getJSON<Conversation>(`${conversations}/${id}`),
  listActiveRuns: (id: string) => getJSON<ActiveRun[]>(`${conversations}/${id}/active-runs`),
  setPinned: (id: string, isPinned: boolean) => patchJSON<ConversationSummary>(`${conversations}/${id}`, { is_pinned: isPinned }),
  create: (body: string, agentId?: string) => postJSON<Conversation>(conversations, { body, agentId }),
  send: (id: string, body: string) => postJSON<Message>(`${conversations}/${id}/messages`, { body }),
  retry: (id: string, messageId: string) => postJSON<Message>(`${conversations}/${id}/messages/${messageId}/retry`, undefined),
  stop: (id: string, messageId: string) => postNoContent(`${conversations}/${id}/cancel`, { messageId }),
}
