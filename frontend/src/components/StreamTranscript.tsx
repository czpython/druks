import { useEffect, useRef, useState } from 'react'

/**
 * Render the harness transcript (Claude or Codex) as readable rows.
 *
 * Two on-disk shapes show up depending on which adapter ran the turn:
 *
 * **Claude** (``--output-format stream-json``)::
 *
 *   {"type":"system","subtype":"init","model":"...","tools":[...]}
 *   {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
 *   {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"..."}}]}}
 *   {"type":"assistant","message":{"content":[{"type":"thinking","thinking":"..."}]}}
 *   {"type":"user","message":{"content":[{"type":"tool_result","content":[{"type":"text","text":"..."}],"is_error":false}]}}
 *   {"type":"result","duration_ms":...,"total_cost_usd":...,"usage":{"input_tokens":...,"output_tokens":...}}
 *
 * **Codex** (``codex exec`` JSONL)::
 *
 *   {"type":"thread.started","thread_id":"..."}
 *   {"type":"turn.started"}
 *   {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
 *   {"type":"item.started","item":{"type":"command_execution","command":"...","status":"in_progress","exit_code":null,"aggregated_output":""}}
 *   {"type":"item.completed","item":{"type":"command_execution","command":"...","status":"completed|failed","exit_code":0|N,"aggregated_output":"..."}}
 *   {"type":"item.completed","item":{"type":"reasoning","text":"..."}}
 *   {"type":"turn.completed","usage":{"input_tokens":...,"output_tokens":...,"cached_input_tokens":...,"reasoning_output_tokens":...}}
 *
 * Codex doesn't carry per-turn duration or cost in the envelope — those
 * come from accounting elsewhere. The result row hides empty fields so a
 * Codex turn doesn't show a misleading ``$0.00 · 0ms``.
 *
 * Lines that don't parse as JSON pass through as plain text — partial
 * stderr leakage stays legible.
 */
export function StreamTranscript({ text, complete = false }: { text: string; complete?: boolean }) {
  const [parseState, setParseState] = useState(() =>
    appendStreamText(emptyParseState, text, complete),
  )
  const rows = parseState.rows

  useEffect(() => {
    setParseState((previous) => appendStreamText(previous, text, complete))
  }, [text, complete])

  // Stick-to-bottom: when the transcript renders inside its own scroll box
  // (the detail page caps `.ins-xscript .stream-transcript`), follow new rows
  // automatically — but release the pin the moment the operator scrolls up
  // to read, and re-pin when they return to the bottom. On pages where the
  // container doesn't scroll (full-run view), scrollTop writes are no-ops.
  const boxRef = useRef<HTMLDivElement>(null)
  const pinnedRef = useRef(true)
  useEffect(() => {
    const el = boxRef.current
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight
  }, [rows])

  if (rows.length === 0) {
    return <pre className="run-pre mono dim">waiting for output…</pre>
  }
  return (
    <div
      className="stream-transcript"
      ref={boxRef}
      onScroll={() => {
        const el = boxRef.current
        if (!el) return
        pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
      }}
    >
      {rows.map((row, i) => (
        <StreamRow key={i} row={row} />
      ))}
    </div>
  )
}

type Row =
  | { kind: 'session'; model: string; tools: number | null }
  | { kind: 'text'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool_use'; name: string; arg: string }
  | { kind: 'tool_result'; text: string; isError: boolean }
  | { kind: 'result'; durationMs: number | null; costUsd: number | null; tokens: number }
  // ``harness_result`` is the structured payload a harness emits as its
  // final output — evaluator verdicts, plan-review decisions, code-review
  // findings. These land in the transcript as bare JSON lines (no
  // stream-envelope ``type`` field) and used to render as truncated
  // "▸ event" unknown rows; now we surface the verdict + body + counts
  // so the operator can read what actually happened without leaving the
  // transcript view.
  | {
      kind: 'harness_result'
      verdict: string
      body: string
      findingsCount: number | null
      checksCount: number | null
      acCount: number | null
      isError: boolean
    }
  | { kind: 'unknown'; label: string; detail: string }
  | { kind: 'raw'; line: string }

interface IncrementalParseState {
  rows: Row[]
  receivedLength: number
  partialLine: string
  tailFlushed: boolean
}

const emptyParseState: IncrementalParseState = {
  rows: [],
  receivedLength: 0,
  partialLine: '',
  tailFlushed: false,
}

function appendStreamText(
  previous: IncrementalParseState,
  text: string,
  complete: boolean,
): IncrementalParseState {
  const suffix = text.slice(previous.receivedLength)
  if (suffix === '' && complete === previous.tailFlushed) return previous

  const pending = previous.partialLine + suffix
  const lastNewline = pending.lastIndexOf('\n')
  const terminated = lastNewline === -1 ? [] : pending.slice(0, lastNewline).split('\n')
  let partialLine = lastNewline === -1 ? pending : pending.slice(lastNewline + 1)
  const newRows = rowsForLines(terminated)

  if (complete && partialLine !== '') {
    newRows.push(...rowsForLines([partialLine]))
    partialLine = ''
  }

  return {
    rows: newRows.length === 0 ? previous.rows : [...previous.rows, ...newRows],
    receivedLength: text.length,
    partialLine,
    tailFlushed: complete,
  }
}

// Exported for unit tests; not a component (HMR fast-refresh warning is moot).
// eslint-disable-next-line react-refresh/only-export-components
export function parseStream(text: string, complete: boolean): Row[] {
  // Live streams can end with a partial JSON line. Keep that hidden until
  // the next chunk arrives; completed/static transcripts should render it.
  const lines = text.split('\n')
  const completeLines = lines.slice(0, lines.length - 1)
  if (complete && lines[lines.length - 1] !== '') {
    completeLines.push(lines[lines.length - 1] ?? '')
  }
  if (lines[lines.length - 1] === '') {
    // Trailing newline means the last "incomplete" slot was empty; the
    // complete list is already correct.
  }

  return rowsForLines(completeLines)
}

function rowsForLines(lines: string[]): Row[] {
  const rows: Row[] = []
  for (const line of lines) {
    if (!line.trim()) continue
    const event = tryParse(line)
    if (event === null) {
      rows.push({ kind: 'raw', line })
      continue
    }
    rows.push(...rowsForEvent(event, line))
  }
  return rows
}

function tryParse(line: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(line)
    return typeof parsed === 'object' && parsed !== null
      ? (parsed as Record<string, unknown>)
      : null
  } catch {
    return null
  }
}

function rowsForEvent(event: Record<string, unknown>, raw: string): Row[] {
  const eventType = event.type
  if (eventType === 'system') {
    if (event.subtype === 'init') {
      return [
        {
          kind: 'session',
          model: stringOr(event.model, '?'),
          tools: Array.isArray(event.tools) ? event.tools.length : 0,
        },
      ]
    }
    // Every other system subtype — thinking_tokens (a token-count progress
    // ticker), hook_started / hook_response, task_started / task_notification —
    // is infra with no content. Drop it so the assistant text and tool calls
    // aren't buried under raw JSON noise.
    return []
  }
  if (eventType === 'rate_limit_event') {
    return []
  }
  if (eventType === 'assistant') {
    const blocks = extractContent(event.message)
    return blocks.flatMap(blockRows)
  }
  if (eventType === 'user') {
    const blocks = extractContent(event.message)
    return blocks.map(userBlockRow)
  }
  if (eventType === 'result') {
    return [
      {
        kind: 'result',
        durationMs: numberOr(event.duration_ms, 0),
        costUsd: numberOr(event.total_cost_usd, 0),
        tokens: totalTokens(event.usage),
      },
    ]
  }
  // --- Codex exec stream ----------------------------------------------------
  // Each item flows ``item.started`` → ``item.completed``. The completed
  // event carries the full payload (final output, exit code, etc.) so we
  // ignore ``item.started`` to avoid double rows. ``turn.started`` is
  // pure noise — Codex emits one per turn.
  if (eventType === 'thread.started') {
    const threadId = stringOr(event.thread_id, '?')
    // Show a short suffix so the operator can correlate with logs.
    const short = threadId.length > 12 ? threadId.slice(-12) : threadId
    // Codex's thread.started carries no tool inventory — omit the count
    // rather than claiming "0 tools" while MCP servers are connected.
    return [{ kind: 'session', model: `codex · ${short}`, tools: null }]
  }
  if (eventType === 'turn.started') {
    return []
  }
  if (eventType === 'item.started') {
    return []
  }
  if (eventType === 'item.completed') {
    return codexItemRows(event.item)
  }
  if (eventType === 'turn.completed') {
    return [
      {
        kind: 'result',
        // Codex doesn't surface duration / cost in the envelope — leave
        // both null so the renderer drops the fields entirely.
        durationMs: null,
        costUsd: null,
        tokens: totalTokens(event.usage),
      },
    ]
  }
  if (eventType === 'error') {
    return [
      {
        kind: 'tool_result',
        text: stringOr(event.message ?? event.error, '(error)'),
        isError: true,
      },
    ]
  }
  // --- Codex session rollout (codex-session.jsonl) --------------------------
  // This is a third shape (alongside claude's stream-json and codex's
  // ``codex exec`` stream): the file codex writes under
  // ``$CODEX_HOME/sessions/`` and which druks now serves as the agent
  // transcript (see AgentCall.artifact_layout). The schema wraps each
  // event with ``{timestamp, type, payload}`` where ``type`` is one of
  // session_meta / event_msg / response_item / turn_context.
  if (eventType === 'session_meta') {
    const payload = (event.payload as Record<string, unknown>) || {}
    return [
      {
        kind: 'session',
        model: stringOr(payload.originator, 'codex'),
        tools: null,
      },
    ]
  }
  if (eventType === 'turn_context') {
    // Workspace + sandbox-policy metadata — pure noise.
    return []
  }
  if (eventType === 'event_msg') {
    return sessionEventMsgRows(event.payload)
  }
  if (eventType === 'response_item') {
    return sessionResponseItemRows(event.payload)
  }
  // Harness result payload (no stream envelope) — e.g. the evaluator's
  // {verdict, body, findings, checks, acceptance_results} or the plan
  // reviewer's {decision, body}. Detect by the presence of ``verdict``
  // or ``decision`` and the absence of a ``type`` field. Render as a
  // structured row so the operator can read the body inline instead of
  // staring at a truncated JSON ellipsis.
  if (!eventType) {
    const harness = harnessResultRow(event)
    if (harness !== null) return [harness]
  }
  return [
    {
      kind: 'unknown',
      label: stringOr(eventType, 'event'),
      detail: raw.length > 80 ? raw.slice(0, 77) + '…' : raw,
    },
  ]
}


function sessionEventMsgRows(payload: unknown): Row[] {
  if (typeof payload !== 'object' || payload === null) return []
  const obj = payload as Record<string, unknown>
  const innerType = obj.type
  if (innerType === 'token_count') {
    const info = obj.info as Record<string, unknown> | undefined
    const totals = info?.total_token_usage as Record<string, unknown> | undefined
    return [
      {
        kind: 'result',
        durationMs: null,
        costUsd: null,
        tokens: numberOr(totals?.total_tokens, 0),
      },
    ]
  }
  // task_started / task_complete / user_message / agent_message: the
  // actual content streams through ``response_item`` as well, so
  // rendering these too would just duplicate every turn.
  return []
}

function sessionResponseItemRows(payload: unknown): Row[] {
  if (typeof payload !== 'object' || payload === null) return []
  const obj = payload as Record<string, unknown>
  const innerType = obj.type
  if (innerType === 'message') {
    const role = stringOr(obj.role, '')
    // Only the assistant role is "what the agent did" - we want the
    // operator looking at the rollout to see activity, not echoed
    // input. ``developer`` is codex's auto-injected permissions /
    // sandbox preamble; ``user`` is the prompt druks rendered and
    // sent. Both are inputs, not activity, so skip.
    if (role !== 'assistant') return []
    const content = obj.content
    if (!Array.isArray(content)) return []
    const text = content
      .filter((c): c is Record<string, unknown> => typeof c === 'object' && c !== null)
      .map((c) => stringOr(c.text, ''))
      .filter((t) => t.length > 0)
      .join('\n')
      .trim()
    return text ? [{ kind: 'text', text }] : []
  }
  if (innerType === 'reasoning') {
    // Codex emits reasoning with an ``encrypted_content`` blob and an
    // optional ``summary`` array. When summary has text we surface it;
    // otherwise the row is pure noise and we drop it.
    const summary = obj.summary
    if (Array.isArray(summary)) {
      const text = summary
        .filter((s): s is Record<string, unknown> => typeof s === 'object' && s !== null)
        .map((s) => stringOr(s.text, ''))
        .filter((t) => t.length > 0)
        .join('\n')
        .trim()
      if (text) return [{ kind: 'thinking', text }]
    }
    return []
  }
  return []
}


function harnessResultRow(event: Record<string, unknown>): Row | null {
  // verdict/decision (review, triage) with status as the implement-result
  // fallback — its schema has status+summary, no verdict field.
  const verdict = stringOr(event.verdict ?? event.decision ?? event.status, '').trim()
  const body = stringOr(event.body ?? event.summary, '').trim()
  if (!verdict && !body) return null
  const findingsCount = Array.isArray(event.findings) ? event.findings.length : null
  const checksCount = Array.isArray(event.checks) ? event.checks.length : null
  const acCount = Array.isArray(event.acceptance_results)
    ? event.acceptance_results.length
    : null
  // Treat fail / blocked / request_changes verdicts as error-coloured —
  // the operator should see those land prominently when they scan the
  // transcript.
  const lower = verdict.toLowerCase()
  const isError =
    lower === 'fail' ||
    lower === 'failed' ||
    lower === 'blocked' ||
    lower === 'request_changes' ||
    lower === 'file_followup'
  return {
    kind: 'harness_result',
    verdict,
    body,
    findingsCount,
    checksCount,
    acCount,
    isError,
  }
}

/** Translate a Codex ``item.completed`` payload into renderable rows. */
function codexItemRows(item: unknown): Row[] {
  if (typeof item !== 'object' || item === null) return []
  const obj = item as Record<string, unknown>
  const itemType = obj.type
  if (itemType === 'agent_message') {
    // With --output-schema, every codex agent_message is a JSON object —
    // the final structured result (interim ones are prompt-suppressed
    // noise; legacy transcripts still carry them). Verdict-shaped results
    // render as the same highlighted row claude's results get; the rest
    // (plan markdown, scope briefs) are structural payloads shown in their
    // own views, so they drop. Prose renders as text — today's codex can't
    // emit it, but it's the upgrade path if the CLI ever enforces
    // final-only.
    const text = stringOr(obj.text, '').trim()
    if (!text) return []
    if (text.startsWith('{')) {
      try {
        const parsed: unknown = JSON.parse(text)
        if (typeof parsed === 'object' && parsed !== null) {
          const row = harnessResultRow(parsed as Record<string, unknown>)
          return row ? [row] : []
        }
      } catch {
        // not JSON after all — fall through to prose
      }
    }
    return [{ kind: 'text', text }]
  }
  if (itemType === 'reasoning') {
    const text = stringOr(obj.text ?? obj.summary, '').trim()
    return text ? [{ kind: 'thinking', text }] : []
  }
  if (itemType === 'mcp_tool_call') {
    // {"server":"linear","tool":"get_issue","arguments":{...},
    //  "result":{"content":[{"type":"text","text":"..."}]},"error":null,
    //  "status":"completed"|"failed"}
    const name =
      [stringOr(obj.server, ''), stringOr(obj.tool, '')].filter(Boolean).join('.') || 'mcp'
    const args = obj.arguments
    const errorText = stringOr(obj.error, '').trim()
    const isError = stringOr(obj.status, '') === 'failed' || Boolean(errorText)
    return [
      {
        kind: 'tool_use',
        name,
        arg: args == null ? '' : JSON.stringify(args),
      },
      {
        kind: 'tool_result',
        text: errorText || mcpResultText(obj.result) || '(no output)',
        isError,
      },
    ]
  }
  if (itemType === 'command_execution') {
    const command = stringOr(obj.command, '').trim()
    const exitCode = obj.exit_code
    const status = stringOr(obj.status, '')
    const output = stringOr(obj.aggregated_output, '').trim()
    const isError =
      status === 'failed' || (typeof exitCode === 'number' && exitCode !== 0)
    const rows: Row[] = []
    if (command) {
      rows.push({ kind: 'tool_use', name: 'bash', arg: command })
    }
    // Always emit a result row when the command finished — even an empty
    // body tells the operator the call returned. The exit-code suffix
    // makes failures jump out without expanding the title tooltip.
    const suffix =
      typeof exitCode === 'number' && exitCode !== 0
        ? `[exit ${exitCode}] ${output || '(no output)'}`
        : output || '(no output)'
    rows.push({ kind: 'tool_result', text: suffix, isError })
    return rows
  }
  if (itemType === 'file_change' || itemType === 'patch') {
    // Best-effort placeholder — surface the type so the operator knows
    // something happened even if the envelope shape is new.
    return [
      {
        kind: 'tool_use',
        name: stringOr(itemType, 'item'),
        arg: stringOr(obj.path ?? obj.summary, ''),
      },
    ]
  }
  // Unknown item types render as labelled rows so a future Codex schema
  // bump shows up without re-deploying the renderer.
  return [
    {
      kind: 'unknown',
      label: stringOr(itemType, 'item'),
      detail: '',
    },
  ]
}

function extractContent(message: unknown): Array<Record<string, unknown>> {
  if (typeof message !== 'object' || message === null) return []
  const content = (message as Record<string, unknown>).content
  if (!Array.isArray(content)) return []
  return content.filter(
    (b): b is Record<string, unknown> => typeof b === 'object' && b !== null,
  )
}

function blockRows(block: Record<string, unknown>): Row[] {
  const type = block.type
  if (type === 'text') {
    const text = stringOr(block.text, '').trim()
    return text ? [{ kind: 'text', text }] : []
  }
  if (type === 'thinking') {
    const text = stringOr(block.thinking ?? block.text, '').trim()
    return text ? [{ kind: 'thinking', text }] : []
  }
  if (type === 'tool_use') {
    const name = stringOr(block.name, 'tool')
    const arg = summarizeToolArg(name, block.input)
    return [{ kind: 'tool_use', name, arg }]
  }
  return []
}

function userBlockRow(block: Record<string, unknown>): Row {
  if (block.type !== 'tool_result') {
    return { kind: 'unknown', label: stringOr(block.type, 'block'), detail: '' }
  }
  const isError = block.is_error === true
  const text = extractToolResultText(block.content)
  return { kind: 'tool_result', text, isError }
}

function extractToolResultText(content: unknown): string {
  if (typeof content === 'string') return content.trim()
  if (!Array.isArray(content)) return ''
  return content
    .map((part) => {
      if (typeof part === 'object' && part !== null && 'text' in part) {
        return stringOr((part as Record<string, unknown>).text, '')
      }
      return ''
    })
    .join('')
    .trim()
}

// Pick the most distinctive arg per tool so the row shows what's
// actually happening, not a generic label. Falls back to the first
// non-empty input value when the tool is unknown.
function summarizeToolArg(name: string, input: unknown): string {
  if (typeof input !== 'object' || input === null) return ''
  const obj = input as Record<string, unknown>
  const pick = (key: string): string => stringOr(obj[key], '')
  switch (name) {
    case 'Read':
    case 'Write':
    case 'Edit':
    case 'MultiEdit':
    case 'NotebookEdit':
      return pick('file_path')
    case 'Bash':
    case 'BashOutput':
    case 'KillShell':
      return pick('command') || pick('shell_id')
    case 'Grep':
    case 'Glob':
      return pick('pattern')
    case 'WebFetch':
      return pick('url')
    case 'WebSearch':
      return pick('query')
    case 'TodoWrite':
      return summarizeTodos(obj.todos)
    default:
      // Generic: take the first scalar value.
      for (const v of Object.values(obj)) {
        if (typeof v === 'string') return v
        if (typeof v === 'number') return String(v)
      }
      return ''
  }
}

function summarizeTodos(todos: unknown): string {
  if (!Array.isArray(todos)) return ''
  const inProgress = todos.find(
    (t) => typeof t === 'object' && t !== null && (t as Record<string, unknown>).status === 'in_progress',
  )
  if (inProgress) {
    return stringOr((inProgress as Record<string, unknown>).content, `${todos.length} todos`)
  }
  return `${todos.length} todos`
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback
}

/** Flatten an MCP result envelope ({content: [{type: "text", text}, …]})
 * into displayable text. Non-text content items are skipped. */
function mcpResultText(result: unknown): string {
  if (typeof result !== 'object' || result === null) return ''
  const content = (result as Record<string, unknown>).content
  if (!Array.isArray(content)) return ''
  return content
    .filter(
      (c): c is Record<string, unknown> => typeof c === 'object' && c !== null,
    )
    .map((c) => stringOr(c.text, ''))
    .filter(Boolean)
    .join('\n')
    .trim()
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' ? value : fallback
}

function totalTokens(usage: unknown): number {
  if (typeof usage !== 'object' || usage === null) return 0
  const u = usage as Record<string, unknown>
  return (
    numberOr(u.input_tokens, 0) +
    numberOr(u.output_tokens, 0) +
    numberOr(u.cache_creation_input_tokens, 0) +
    numberOr(u.cache_read_input_tokens, 0)
  )
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const r = s % 60
  return r > 0 ? `${m}m ${r}s` : `${m}m`
}

function formatTokens(n: number): string {
  if (n < 1000) return `${n}`
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`
  return `${(n / 1_000_000).toFixed(2)}M`
}

function StreamRow({ row }: { row: Row }) {
  switch (row.kind) {
    case 'session':
      return (
        <div className="stream-row stream-row-session mono dim">
          ◇ session · {row.model}
          {row.tools !== null && (
            <>
              {' '}· {row.tools} tool{row.tools === 1 ? '' : 's'}
            </>
          )}
        </div>
      )
    case 'text':
      return <div className="stream-row stream-row-text">{row.text}</div>
    case 'thinking':
      return (
        <div className="stream-row stream-row-thinking">
          <span className="stream-glyph">✻</span> {row.text}
        </div>
      )
    case 'tool_use':
      return (
        <div className="stream-row stream-row-tool">
          <span className="stream-glyph">→</span>{' '}
          <span className="stream-tool-name mono">{row.name}</span>
          {row.arg && <span className="stream-tool-arg mono dim"> {row.arg}</span>}
        </div>
      )
    case 'tool_result': {
      // ``split('\n')[0]`` is statically ``string | undefined`` under
      // ``noUncheckedIndexedAccess``; coalesce so the slice below is sound.
      const single = row.text.split('\n')[0] ?? ''
      const truncated = single.length > 200 ? single.slice(0, 197) + '…' : single
      return (
        <div
          className={`stream-row stream-row-result mono dim${
            row.isError ? ' stream-row-result-error' : ''
          }`}
          title={row.text}
        >
          <span className="stream-glyph">←</span> {truncated || '(empty)'}
        </div>
      )
    }
    case 'result': {
      // Codex turns don't carry duration / cost in the envelope, so we
      // drop those segments when they're null. Result lines should
      // never read ``$0.00 · 0ms`` just because we don't have the data.
      const parts: string[] = []
      if (row.durationMs !== null) parts.push(formatDuration(row.durationMs))
      if (row.costUsd !== null) parts.push(`$${row.costUsd.toFixed(2)}`)
      parts.push(`${formatTokens(row.tokens)} tokens`)
      return (
        <div className="stream-row stream-row-done mono">
          <span className="stream-glyph">✓</span> done · {parts.join(' · ')}
        </div>
      )
    }
    case 'harness_result': {
      // Counts row — only show segments that exist on this payload
      // shape so a plan-review row (decision + body only) doesn't
      // render misleading "0 findings · 0 checks · 0 AC".
      const counts: string[] = []
      if (row.findingsCount !== null) {
        counts.push(`${row.findingsCount} finding${row.findingsCount === 1 ? '' : 's'}`)
      }
      if (row.checksCount !== null) {
        counts.push(`${row.checksCount} check${row.checksCount === 1 ? '' : 's'}`)
      }
      if (row.acCount !== null) {
        counts.push(`${row.acCount} AC`)
      }
      return (
        <div
          className={`stream-row stream-row-harness-result mono${
            row.isError ? ' stream-row-result-error' : ''
          }`}
        >
          <div className="stream-row-harness-head">
            <span className="stream-glyph">⊕</span>{' '}
            {row.verdict && (
              <span className="stream-harness-verdict mono">{row.verdict}</span>
            )}
            {counts.length > 0 && (
              <span className="stream-harness-counts mono dim">
                {counts.join(' · ')}
              </span>
            )}
          </div>
          {row.body && <div className="stream-row-harness-body">{row.body}</div>}
        </div>
      )
    }
    case 'unknown':
      return (
        <div className="stream-row stream-row-unknown mono dim">
          ▸ {row.label} {row.detail && <span className="stream-tool-arg">{row.detail}</span>}
        </div>
      )
    case 'raw':
      // Non-JSON line — Codex transcript, stderr leakage, etc. Keep it
      // legible alongside the structured rows.
      return <div className="stream-row stream-row-raw mono">{row.line}</div>
  }
}
