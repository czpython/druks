import { useEffect, useReducer, useRef, useState } from 'react'
import type { ToolCall } from '@agentclientprotocol/sdk'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowLeft, Check, ChevronRight, Plus } from 'lucide-react'
import { useStickToBottom } from 'use-stick-to-bottom'
import { Link, useLocation } from 'wouter'

import { ApiError, UnauthorizedError } from '../api/client'
import { chatApi } from '../chat/api'
import {
  conversationReducer, initialConversation, savedReplyRows,
  type Conversation, type ConversationAction, type ConversationSummary, type ReplyRow,
} from '../chat/state'
import { Markdown } from '../components/Markdown'
import { Page } from '../components/Page'
import { useFormatters } from '../lib/preferences'
import '../chat.css'

const conversationListKey = ['chat', 'conversations']
const conversationIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function ChatPage({ id }: { id?: string }) {
  const [, navigate] = useLocation()
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const list = useQuery({ queryKey: conversationListKey, queryFn: chatApi.list, refetchInterval: 10_000 })
  const format = useFormatters()
  const draftKey = id ?? 'new'
  const conversationId = id && id !== 'new' ? id : undefined
  const groups = new Map<string, ConversationSummary[]>()
  for (const conversation of list.data ?? []) {
    const day = format.absDay(conversation.createdAt)
    groups.set(day, [...groups.get(day) ?? [], conversation])
  }

  if (conversationId !== undefined && !conversationIdPattern.test(conversationId)) {
    return <Page inset><p role="alert">Conversation not found.</p><Link href="/chat">Back to conversations</Link></Page>
  }

  return (
    <Page className={`page-chat${id ? ' has-conversation' : ''}`} scroll="internal">
      <aside className="chat-list" aria-label="Conversations">
        <header className="chat-list-head">
          <h1>Chat</h1>
          <Link href="/chat/new" className="chat-button">
            <Plus size={16} aria-hidden="true" /> New conversation
          </Link>
        </header>
        <div className="chat-list-body">
          {list.isPending && <p className="chat-notice" role="status">Loading conversations…</p>}
          {list.isError && <div className="chat-notice" role="alert">
            <p>Could not load conversations.</p>
            <button className="chat-button" onClick={() => void list.refetch()}>Try again</button>
          </div>}
          {Array.from(groups, ([day, conversations]) => <section key={day} aria-label={day}>
            <h2 className="chat-day">{day}</h2>
            {conversations.map((conversation) => <Link
              key={conversation.id}
              href={`/chat/${conversation.id}`}
              className="chat-list-item"
              aria-current={conversation.id === conversationId ? 'page' : undefined}
            >
              <span className="chat-list-title">{conversation.title ?? 'New conversation'}</span>
              <time dateTime={conversation.createdAt} title={format.absTime(conversation.createdAt)}>
                {format.clockTime(conversation.createdAt)}
              </time>
              {conversation.activeMessageId
                ? <span className="chat-list-status"><span className="chat-activity-dot" />Agent is replying</span>
                : <span className="chat-list-status">{conversation.messageCount} {conversation.messageCount === 1 ? 'message' : 'messages'}</span>}
            </Link>)}
          </section>)}
        </div>
        <p className="chat-privacy">Only you can see these conversations.</p>
      </aside>
      <ConversationThread
        key={draftKey}
        id={conversationId}
        draft={drafts[draftKey] ?? ''}
        onDraft={(draft) => setDrafts((current) => ({ ...current, [draftKey]: draft }))}
        onCreated={(conversation, submitted) => {
          setDrafts((current) => ({ ...current,
            [conversation.id]: current[draftKey] === submitted ? '' : current[draftKey] ?? '',
            [draftKey]: '',
          }))
          navigate(`/chat/${conversation.id}`)
        }}
      />
    </Page>
  )
}

function ConversationThread({ id, draft, onDraft, onCreated }: {
  id?: string
  draft: string
  onDraft: (draft: string) => void
  onCreated: (conversation: Conversation, submitted: string) => void
}) {
  const queryClient = useQueryClient()
  const [state, dispatch] = useReducer(conversationReducer, initialConversation)
  const [connection, setConnection] = useState<'opening' | 'connected' | 'reconnecting' | 'denied'>('opening')
  const [reconnect, setReconnect] = useState(0)
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const [stopping, setStopping] = useState(false)
  const mounted = useRef(true)
  const draftRef = useRef(draft)
  const composer = useRef<HTMLTextAreaElement>(null)
  const format = useFormatters()
  const { scrollRef, contentRef, isAtBottom, scrollToBottom } = useStickToBottom({ initial: 'instant', resize: 'instant' })

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    draftRef.current = draft
  }, [draft])

  useEffect(() => {
    if (!id) return
    let active = true
    let socket: WebSocket | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    let attempts = 0

    function receive(action: ConversationAction) {
      if (!active) return
      dispatch(action)
      if (action.type === 'snapshot') {
        const summary: ConversationSummary = {
          id: action.id, title: action.title, source: action.source, createdAt: action.createdAt,
          messageCount: action.messageCount, activeMessageId: action.activeMessageId,
        }
        queryClient.setQueryData<ConversationSummary[]>(conversationListKey, (current = []) =>
          [...current.filter((item) => item.id !== summary.id), summary].sort((left, right) =>
            Date.parse(right.createdAt) - Date.parse(left.createdAt) || right.id.localeCompare(left.id),
          ),
        )
      }
    }

    function retry() {
      if (active) {
        setConnection('reconnecting')
        timer = setTimeout(() => void connect(), Math.min(1000 * 2 ** attempts++, 10_000))
      }
    }

    async function connect() {
      try {
        const conversation = await chatApi.get(id!)
        if (!active) return
        receive({ type: 'snapshot', ...conversation })
        const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
        socket = new WebSocket(`${scheme}://${window.location.host}/api/chat/conversations/${id}/ws`)
        socket.onopen = () => {
          if (active) {
            attempts = 0
            setConnection('connected')
            setError('')
          }
        }
        socket.onmessage = (event) => receive(JSON.parse(event.data) as ConversationAction)
        socket.onclose = (event) => {
          if (!active) return
          if (event.code === 1008) {
            setConnection('denied')
            setError('Chat access was denied. Reload the page to check your account.')
          } else retry()
        }
      } catch (caught) {
        if (!active) return
        if (caught instanceof UnauthorizedError || (caught instanceof ApiError && (caught.status === 404 || caught.status === 403))) {
          setConnection('denied')
          setError(caught.message)
        } else retry()
      }
    }

    void connect()
    return () => {
      active = false
      clearTimeout(timer)
      socket?.close()
    }
  }, [id, queryClient, reconnect])

  async function send(body = draft, resend = false) {
    if (!body.trim() || sending) return
    setSending(true)
    setError('')
    try {
      if (id) {
        const message = await chatApi.send(id, body)
        if (!mounted.current) return
        dispatch({ type: 'saved', message })
        if (!resend && draftRef.current === body) onDraft('')
        composer.current?.focus()
        void scrollToBottom()
      } else {
        const conversation = await chatApi.create(body)
        void queryClient.invalidateQueries({ queryKey: conversationListKey })
        if (mounted.current) onCreated(conversation, body)
      }
    } catch (caught) {
      if (mounted.current) setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      if (mounted.current) setSending(false)
    }
  }

  async function stop(messageId: string) {
    setStopping(true)
    setError('')
    try {
      await chatApi.stop(id!, messageId)
    } catch (caught) {
      if (mounted.current) setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      if (mounted.current) setStopping(false)
    }
  }

  const conversation = state.conversation
  const messages = conversation?.messages ?? []
  const replies = new Map(messages.filter((message) => message.role === 'assistant').map((message) => [message.replyTo, message]))
  const firstPending = messages.find((message) => message.role === 'user' && (message.state === 'pending' || message.state === 'delivered'))

  return <section className="chat-thread" aria-label="Conversation">
    <header className="chat-thread-head">
      <Link href="/chat" className="chat-back chat-button" aria-label="Back to conversations"><ArrowLeft size={18} /></Link>
      <h2>{conversation?.title ?? (id && !conversation ? 'Conversation' : 'New conversation')}</h2>
      {id && connection === 'reconnecting' && <span className="chat-connection" role="status">Reconnecting…</span>}
    </header>
    <div className="chat-messages" ref={scrollRef} role="log" aria-label="Messages" aria-live="polite">
      <div className="chat-messages-content" ref={contentRef}>
        {!id && <p className="chat-start">Send a message to start a conversation.</p>}
        {id && !conversation && connection === 'opening' && <p className="chat-notice" role="status">Loading conversation…</p>}
        {messages.filter((message) => message.role === 'user').map((message) => {
          const reply = message.state === 'replied' || (message.state === 'cancelled' && message.deliveredAt)
            ? replies.get(message.id) : undefined
          const rows = reply ? savedReplyRows(reply) : state.turns[message.id]?.rows ?? []
          const isQueued = message.state === 'pending' && firstPending?.id !== message.id
          const isWaiting = firstPending?.id === message.id && rows.length === 0 && !state.error
          return <article className="chat-turn" key={message.id} aria-label="Message and reply">
            <div className="chat-user">
              <div className="chat-message-meta">You <time dateTime={message.createdAt} title={format.absTime(message.createdAt)}>{format.clockTime(message.createdAt)}</time></div>
              <div className="chat-user-body">{message.body}</div>
              {isQueued && <span className="chat-queued">Queued</span>}
            </div>
            <div className="chat-reply">
              <Reply rows={rows} />
              {isWaiting && <p className="chat-waiting" role="status"><span className="chat-activity-dot" />{conversation?.activeMessageId === message.id ? 'Agent is replying…' : 'Connecting…'}</p>}
              {(message.state === 'interrupted' || message.state === 'cancelled') && <div className="chat-interrupted" role="status">
                <strong>{message.state === 'cancelled' ? 'Cancelled' : 'Interrupted'}</strong>
                <p>{message.state === 'cancelled' ? 'You stopped this turn.' : 'This turn stopped before it finished.'}</p>
                <button className="chat-button" onClick={() => void send(message.body, true)} disabled={sending}>Send again</button>
                <span>Druks never resends by itself.</span>
              </div>}
            </div>
          </article>
        })}
      </div>
    </div>
    {!isAtBottom && messages.length > 0 && <div className="chat-jump">
      <button className="chat-button" onClick={() => void scrollToBottom()}><ArrowDown size={16} aria-hidden="true" /> Latest message</button>
    </div>}
    {(error || state.error) && <div className="chat-error" role="alert">
      <p>{error || state.error?.detail}</p>
      {state.error && <button className="chat-button" onClick={() => setReconnect((attempt) => attempt + 1)}>Connect again</button>}
    </div>}
    <form className="chat-composer" onSubmit={(event) => { event.preventDefault(); void send() }}>
      <div className="chat-composer-controls">
        <textarea
          ref={composer}
          aria-label="Message"
          aria-describedby="chat-send-help"
          placeholder="Message Druks…"
          rows={2}
          value={draft}
          onChange={(event) => { draftRef.current = event.target.value; onDraft(event.target.value) }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault()
              void send()
            }
          }}
        />
        <div className="chat-composer-actions">
          {firstPending && <button type="button" className="chat-button" onClick={() => void stop(firstPending.id)} disabled={stopping}>{stopping ? 'Stopping…' : 'Stop'}</button>}
          <button type="submit" className="chat-button primary" disabled={!draft.trim() || sending || (Boolean(id) && !conversation)}>{sending ? 'Sending…' : 'Send'}</button>
        </div>
      </div>
      <p id="chat-send-help">Enter to send · Shift + Enter for a new line</p>
    </form>
  </section>
}

function Reply({ rows }: { rows: ReplyRow[] }) {
  return rows.map((row, index) => {
    if (row.type === 'text') return <Markdown key={index} source={row.text} />
    if (row.type === 'tool') return <ToolRow key={row.call.toolCallId} call={row.call} />
    return row.entries.length > 0 && <ol className="chat-plan" key="plan" aria-label="Agent plan">
      {row.entries.map((entry, entryIndex) => <li key={entryIndex} data-state={entry.status}>
        <span>{entry.content}</span><span className="chat-plan-status">{entry.status.replaceAll('_', ' ')}</span>
      </li>)}
    </ol>
  })
}

function ToolRow({ call }: { call: ToolCall }) {
  const name = call.name?.replace(/^mcp__druks__/, '')
  const status = call.status ?? 'pending'
  const labels = { pending: 'Pending', in_progress: 'Running', completed: 'Complete', failed: 'Failed' }
  const output = 'rawOutput' in call ? call.rawOutput : call.content
  return <details className="chat-tool">
    <summary>
      <ChevronRight size={16} className="chat-tool-chevron" aria-hidden="true" />
      <span className="chat-tool-label"><span>{call.title}</span>{name && name !== call.title && <code>{name}</code>}</span>
      <span className={`chat-tool-status is-${status}`}>{status === 'completed' && <Check size={15} aria-hidden="true" />}{labels[status]}</span>
    </summary>
    <div className="chat-tool-details">
      <h3>Input</h3>
      {call.rawInput === undefined ? <p>No input.</p> : <pre>{JSON.stringify(call.rawInput, null, 2)}</pre>}
      <h3>Output</h3>
      {output === undefined ? <p>{status === 'completed' || status === 'failed' ? 'No output.' : 'Waiting for output.'}</p> : <pre>{JSON.stringify(output, null, 2)}</pre>}
    </div>
  </details>
}
