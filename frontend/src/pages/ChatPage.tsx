import { useEffect, useReducer, useRef, useState } from 'react'
import type { ToolCall } from '@agentclientprotocol/sdk'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Check, ChevronRight, Pin, Plus, Search, X } from 'lucide-react'
import { useStickToBottom } from 'use-stick-to-bottom'
import { Link, useLocation } from 'wouter'

import { api, ApiError, UnauthorizedError } from '../api/client'
import { appLabel } from '../apps/registry'
import { chatApi } from '../chat/api'
import {
  compareConversations, conversationReducer, conversationTitle, initialConversation, savedReplyRows,
  type Conversation, type ConversationAction, type ConversationSummary, type ReplyRow,
} from '../chat/state'
import { Markdown } from '../components/Markdown'
import { Page } from '../components/Page'
import { useFormatters } from '../lib/preferences'
import { zonedParts } from '../lib/format'
import '../chat.css'

const conversationListKey = ['chat', 'conversations']
const conversationIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function ChatPage({ id }: { id?: string }) {
  const [, navigate] = useLocation()
  const queryClient = useQueryClient()
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [search, setSearch] = useState('')
  const [now, setNow] = useState(() => new Date())
  const list = useQuery({ queryKey: conversationListKey, queryFn: chatApi.list, refetchInterval: 10_000 })
  const pin = useMutation({
    mutationFn: ({ id, isPinned }: { id: string; isPinned: boolean }) => chatApi.setPinned(id, isPinned),
    onSuccess: (summary) => {
      queryClient.setQueryData<ConversationSummary[]>(conversationListKey, (current = []) =>
        current.map((conversation) => conversation.id === summary.id ? summary : conversation).sort(compareConversations),
      )
      void queryClient.invalidateQueries({ queryKey: conversationListKey })
    },
  })
  const format = useFormatters()
  const draftKey = id ?? 'new'
  const conversationId = id && id !== 'new' ? id : undefined
  const today = zonedParts(now, format.timezone)
  const todayDate = Date.UTC(today.year, today.month - 1, today.day)
  const groups = new Map<string, ConversationSummary[]>([
    ['Pinned', []], ['Today', []], ['Yesterday', []], ['Earlier', []],
  ])
  const matches = [...list.data ?? []]
    .filter((conversation) => conversationTitle(conversation).toLowerCase().includes(search.trim().toLowerCase()))
    .sort(compareConversations)
  for (const conversation of matches) {
    const day = zonedParts(new Date(conversation.lastMessageAt), format.timezone)
    const daysAgo = (todayDate - Date.UTC(day.year, day.month - 1, day.day)) / 86_400_000
    let group: string
    if (conversation.isPinned) group = 'Pinned'
    else if (daysAgo === 0) group = 'Today'
    else if (daysAgo === 1) group = 'Yesterday'
    else group = 'Earlier'
    groups.get(group)!.push(conversation)
  }

  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 30_000)
    return () => clearInterval(timer)
  }, [])

  function togglePinned(conversation: ConversationSummary) {
    pin.mutate({ id: conversation.id, isPinned: !conversation.isPinned })
  }

  if (conversationId !== undefined && !conversationIdPattern.test(conversationId)) {
    return <Page inset><p role="alert">Conversation not found.</p><Link href="/chat">Back to conversations</Link></Page>
  }

  return (
    <Page className={`page-chat${id ? ' has-conversation' : ''}`} scroll="internal">
      {pin.isError && <div className="chat-pin-error" role="alert">
        <span>Could not save the pin.</span>
        <button className="chat-button" onClick={() => pin.mutate(pin.variables)}>Try again</button>
        <button className="chat-icon-button" aria-label="Dismiss pin error" onClick={() => pin.reset()}><X size={16} /></button>
      </div>}
      <aside className="chat-list" aria-label="Conversations">
        <header className="chat-list-head">
          <label className="chat-search">
            <Search size={15} aria-hidden="true" />
            <input type="search" aria-label="Search conversations" placeholder="Search conversations" value={search} onChange={(event) => setSearch(event.target.value)} />
          </label>
          <Link href="/chat/new" className="chat-icon-button chat-new" aria-label="New conversation" title="New conversation">
            <Plus size={19} aria-hidden="true" />
          </Link>
        </header>
        <div className="chat-list-body">
          {list.isPending && <p className="chat-notice" role="status">Loading conversations…</p>}
          {list.isError && <div className="chat-notice" role="alert">
            <p>Could not load conversations.</p>
            <button className="chat-button" onClick={() => void list.refetch()}>Try again</button>
          </div>}
          {list.isSuccess && list.data.length === 0 && <p className="chat-notice">Your conversations will appear here.</p>}
          {list.isSuccess && list.data.length > 0 && matches.length === 0 && <div className="chat-notice" role="status">
            <p>No matching conversations.</p>
            <button className="chat-button" onClick={() => setSearch('')}>Clear search</button>
          </div>}
          {Array.from(groups, ([day, conversations]) => conversations.length > 0 && <section key={day} aria-label={day}>
            <h2 className="chat-day">{day}</h2>
            {conversations.map((conversation) => <div className="chat-list-row" key={conversation.id} data-selected={conversation.id === conversationId}>
              <Link href={`/chat/${conversation.id}`} className="chat-list-item" aria-current={conversation.id === conversationId ? 'page' : undefined}>
                <span className="chat-list-title" title={conversationTitle(conversation)}>{conversationTitle(conversation)}</span>
                <time dateTime={conversation.lastMessageAt} title={format.absTime(conversation.lastMessageAt)}>
                  {format.absTimeCompact(conversation.lastMessageAt)}
                </time>
                {conversation.activeMessageId
                  ? <span className="chat-list-status"><span className="chat-activity-dot" />Agent is replying</span>
                  : <span className="chat-list-status">{conversation.messageCount} {conversation.messageCount === 1 ? 'message' : 'messages'}</span>}
              </Link>
              <PinButton conversation={conversation} onPin={togglePinned} disabled={pin.isPending} />
            </div>)}
          </section>)}
        </div>
      </aside>
      <ConversationThread
        key={draftKey}
        id={conversationId}
        summary={list.data?.find((conversation) => conversation.id === conversationId)}
        onPin={togglePinned}
        pinPending={pin.isPending}
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

function PinButton({ conversation, onPin, disabled }: {
  conversation: ConversationSummary
  onPin: (conversation: ConversationSummary) => void
  disabled: boolean
}) {
  const label = `Pin ${conversationTitle(conversation)}`
  return <button className="chat-icon-button chat-pin" aria-label={label} title={label} aria-pressed={conversation.isPinned} disabled={disabled} onClick={() => onPin(conversation)}>
    <Pin size={16} aria-hidden="true" />
  </button>
}

function ConversationThread({ id, summary, onPin, pinPending, draft, onDraft, onCreated }: {
  id?: string
  summary?: ConversationSummary
  onPin: (conversation: ConversationSummary) => void
  pinPending: boolean
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
  const apps = useQuery({ queryKey: ['apps'], queryFn: api.listApps, enabled: !id, staleTime: 60_000 })
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
          id: action.id, title: action.title, source: action.source, userId: action.userId,
          userName: action.userName, createdAt: action.createdAt,
          isPinned: action.isPinned, lastMessageAt: action.lastMessageAt,
          messageCount: action.messageCount, activeMessageId: action.activeMessageId,
        }
        queryClient.setQueryData<ConversationSummary[]>(conversationListKey, (current = []) =>
          [...current.filter((item) => item.id !== summary.id), summary].sort(compareConversations),
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
  const userMessages = messages.filter((message) => message.role === 'user')
  const lastReply = messages.findLast((message) => message.role === 'assistant')
  // A WhatsApp turn can answer several messages, and its newest delivered message names it.
  const unansweredMessage = userMessages.findLast((message) => message.state === 'delivered')
    ?? userMessages.find((message) => message.state === 'pending')

  return <section className={`chat-thread${id ? '' : ' is-new'}`} aria-label="Conversation">
    <header className="chat-thread-head">
      <Link href="/chat" className="chat-back chat-button" aria-label="Back to conversations"><ArrowLeft size={18} /></Link>
      <div className="chat-thread-title">
        {id && <h1>{conversation ? conversationTitle(conversation) : 'Conversation'}</h1>}
        {conversation && <p>{conversation.messageCount} {conversation.messageCount === 1 ? 'message' : 'messages'}
          {lastReply && <> <span aria-hidden="true">/</span> last reply <time dateTime={lastReply.createdAt} title={format.absTime(lastReply.createdAt)}>{format.absTimeCompact(lastReply.createdAt)}</time></>}
        </p>}
      </div>
      {id && connection === 'reconnecting' && <span className="chat-connection" role="status">Reconnecting…</span>}
      {conversation && <PinButton conversation={summary ?? conversation} onPin={onPin} disabled={pinPending} />}
    </header>
    {!id && <div className="chat-intro">
      <h1>What should Druks do?</h1>
      <p>Ask about activity, usage, or schedules. Tell Druks what to do next.</p>
    </div>}
    <div className="chat-messages" ref={scrollRef} role="log" aria-label="Messages" aria-live="polite" hidden={!id}>
      <div className="chat-messages-content" ref={contentRef}>
        {id && !conversation && connection === 'opening' && <p className="chat-notice" role="status">Loading conversation…</p>}
        {userMessages.map((message) => {
          const reply = message.state === 'replied' || (message.state === 'cancelled' && message.deliveredAt)
            ? replies.get(message.id) : undefined
          const rows = reply ? savedReplyRows(reply) : state.turns[message.id]?.rows ?? []
          const isQueued = message.state === 'pending' && unansweredMessage?.id !== message.id
          const isWaiting = unansweredMessage?.id === message.id && rows.length === 0 && !state.error
          return <article className="chat-turn" key={message.id} aria-label="Message and reply">
            <div className={message.isInternal ? 'chat-user is-internal' : 'chat-user'}>
              <div className="chat-message-meta">{message.isInternal ? 'Druks' : 'You'} <time dateTime={message.createdAt} title={format.absTime(message.createdAt)}>{format.absTimeCompact(message.createdAt)}</time></div>
              {message.body && <div className="chat-user-body">{message.body}</div>}
              {message.file && <a href={message.file.url} target="_blank" rel="noreferrer">{message.file.name}</a>}
              {isQueued && <span className="chat-queued">Queued</span>}
            </div>
            <div className="chat-reply">
              {(rows.length > 0 || isWaiting) && <div className="chat-message-meta chat-agent-meta">Agent
                {reply && <time dateTime={reply.createdAt} title={format.absTime(reply.createdAt)}>{format.absTimeCompact(reply.createdAt)}</time>}
              </div>}
              <Reply rows={rows} />
              {isWaiting && <p className="chat-waiting" role="status"><span className="chat-activity-dot" />{message.state === 'delivered' ? 'Agent is replying…' : 'Connecting…'}</p>}
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
          placeholder={id ? 'Reply, or ask Druks to do something…' : 'Message Druks…'}
          rows={1}
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
          {unansweredMessage && !state.error && <button type="button" className="chat-button" onClick={() => void stop(unansweredMessage.id)} disabled={stopping}>{stopping ? 'Stopping…' : 'Stop'}</button>}
          <button type="submit" className="chat-icon-button chat-send" aria-label={sending ? 'Sending…' : 'Send'} title="Send" disabled={!draft.trim() || sending || (Boolean(id) && !conversation)}><ArrowUp size={18} aria-hidden="true" /></button>
        </div>
      </div>
      <p id="chat-send-help">Enter to send · Shift + Enter for a new line</p>
    </form>
    {!id && <div className="chat-starters">
      {['What needs my attention?', 'What failed in the last 24 hours?', 'Compare usage this week with last week.'].map((prompt) => <button key={prompt} onClick={() => { onDraft(prompt); composer.current?.focus() }}>
        <span>{prompt}</span><ArrowRight size={16} aria-hidden="true" />
      </button>)}
      {apps.isSuccess && <p className="chat-apps">{apps.data.length} installed {apps.data.length === 1 ? 'app' : 'apps'}{apps.data.length > 0 && <> · {apps.data.map((app) => appLabel(app.name)).join(', ')}</>}</p>}
    </div>}
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
  const status = call.status ?? 'pending'
  const labels = { pending: 'Pending', in_progress: 'Running', completed: 'Complete', failed: 'Failed' }
  const output = 'rawOutput' in call ? call.rawOutput : call.content
  return <details className="chat-tool">
    <summary>
      <ChevronRight size={16} className="chat-tool-chevron" aria-hidden="true" />
      <span className="chat-tool-label">{call.title}</span>
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
