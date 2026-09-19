import { getJSON, patchJSON, postJSON, postNoContent } from '../api/client'
import type { Conversation, ConversationSummary, Message } from './state'

const conversations = '/api/chat/conversations'

export const chatApi = {
  list: () => getJSON<ConversationSummary[]>(conversations),
  get: (id: string) => getJSON<Conversation>(`${conversations}/${id}`),
  pin: (id: string, pinned: boolean) => patchJSON<ConversationSummary>(`${conversations}/${id}`, { pinned }),
  create: (body: string) => postJSON<Conversation>(conversations, { body }),
  send: (id: string, body: string) => postJSON<Message>(`${conversations}/${id}/messages`, { body }),
  stop: (id: string, messageId: string) => postNoContent(`${conversations}/${id}/cancel`, { messageId }),
}
