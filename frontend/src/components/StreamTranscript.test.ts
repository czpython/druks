import { describe, expect, it } from 'vitest'

import { parseStream } from './StreamTranscript'

const line = (o: unknown) => JSON.stringify(o)

describe('parseStream noise suppression', () => {
  it('drops infra system subtypes and rate-limit events', () => {
    const text = [
      line({ type: 'system', subtype: 'thinking_tokens', estimated_tokens: 50 }),
      line({ type: 'system', subtype: 'hook_started' }),
      line({ type: 'system', subtype: 'task_started', task_id: 'x' }),
      line({ type: 'system', subtype: 'task_notification' }),
      line({ type: 'rate_limit_event' }),
    ].join('\n')

    expect(parseStream(text, true)).toEqual([])
  })

  it('still renders the assistant text and tool calls between the noise', () => {
    const text = [
      line({ type: 'system', subtype: 'thinking_tokens', estimated_tokens: 50 }),
      line({ type: 'assistant', message: { content: [{ type: 'text', text: 'Running checks.' }] } }),
      line({
        type: 'assistant',
        message: { content: [{ type: 'tool_use', name: 'Bash', input: { command: 'make test' } }] },
      }),
      line({ type: 'system', subtype: 'thinking_tokens', estimated_tokens: 200 }),
    ].join('\n')

    const rows = parseStream(text, true)

    expect(rows.map((r) => r.kind)).toEqual(['text', 'tool_use'])
    expect(rows[0]).toMatchObject({ kind: 'text', text: 'Running checks.' })
    expect(rows[1]).toMatchObject({ kind: 'tool_use', name: 'Bash' })
  })

  it('hands codex agent_message JSON to the app and keeps reasoning and tools', () => {
    const text = [
      line({
        type: 'item.completed',
        item: {
          type: 'agent_message',
          text: '{"acceptance_criteria":[],"plan_markdown":"I will read the repo first.","questions":[]}',
        },
      }),
      line({ type: 'item.completed', item: { type: 'reasoning', text: 'Examining the tests.' } }),
      line({
        type: 'item.completed',
        item: { type: 'command_execution', command: 'make test', status: 'completed', exit_code: 0, aggregated_output: 'ok' },
      }),
    ].join('\n')

    const rows = parseStream(text, true)

    expect(rows.map((r) => r.kind)).toEqual(['harness_result', 'thinking', 'tool_use', 'tool_result'])
    expect(rows[1]).toMatchObject({ kind: 'thinking', text: 'Examining the tests.' })
  })

  it('renders prose codex agent_message as text, like claude narration', () => {
    // With the schema as a prompt epilogue (not --output-schema), codex
    // narrates in plain agent_message prose mid-run; only the final
    // JSON-object message is the structured result.
    const text = [
      line({
        type: 'item.completed',
        item: { type: 'agent_message', text: 'I will read the diff and the changed files.' },
      }),
      line({
        type: 'item.completed',
        item: { type: 'agent_message', text: '{"plan_markdown":"# plan","questions":[]}' },
      }),
    ].join('\n')

    const rows = parseStream(text, true)

    expect(rows.map((r) => r.kind)).toEqual(['text', 'harness_result'])
    expect(rows[0]).toMatchObject({
      kind: 'text',
      text: 'I will read the diff and the changed files.',
    })
  })

  it('hands every codex final JSON payload to the app as a harness result', () => {
    const text = [
      line({
        type: 'item.completed',
        item: {
          type: 'agent_message',
          text: '{"verdict":"request_changes","body":"Two findings.","findings":[{},{}]}',
        },
      }),
      line({
        type: 'item.completed',
        item: { type: 'agent_message', text: '{"plan_markdown":"# plan","questions":[]}' },
      }),
    ].join('\n')

    const rows = parseStream(text, true)

    expect(rows).toEqual([
      {
        kind: 'harness_result',
        result: { verdict: 'request_changes', body: 'Two findings.', findings: [{}, {}] },
      },
      { kind: 'harness_result', result: { plan_markdown: '# plan', questions: [] } },
    ])
  })

  it('renders codex mcp_tool_call as a named tool call with its result text', () => {
    // Real shape from a prod generate_plan transcript.
    const text = [
      line({ type: 'item.started', item: { type: 'mcp_tool_call', server: 'linear', tool: 'get_issue', status: 'in_progress' } }),
      line({
        type: 'item.completed',
        item: {
          type: 'mcp_tool_call',
          server: 'linear',
          tool: 'get_issue',
          arguments: { id: 'ACME-398', includeRelations: true },
          result: { content: [{ type: 'text', text: '{"id":"ACME-398","title":"Run linting"}' }], structured_content: null },
          error: null,
          status: 'completed',
        },
      }),
    ].join('\n')

    const rows = parseStream(text, true)

    expect(rows).toEqual([
      { kind: 'tool_use', name: 'linear.get_issue', arg: '{"id":"ACME-398","includeRelations":true}' },
      { kind: 'tool_result', text: '{"id":"ACME-398","title":"Run linting"}', isError: false },
    ])
  })

  it('marks a failed mcp_tool_call as an error result', () => {
    const text = line({
      type: 'item.completed',
      item: {
        type: 'mcp_tool_call',
        server: 'linear',
        tool: 'get_issue',
        arguments: { id: 'ACME-0' },
        result: null,
        error: 'issue not found',
        status: 'failed',
      },
    })

    expect(parseStream(text, true)).toEqual([
      { kind: 'tool_use', name: 'linear.get_issue', arg: '{"id":"ACME-0"}' },
      { kind: 'tool_result', text: 'issue not found', isError: true },
    ])
  })

  it('omits the tool count on codex session rows (no inventory in thread.started)', () => {
    const rows = parseStream(line({ type: 'thread.started', thread_id: '0199e12113ec5ec27' }), true)
    expect(rows).toEqual([{ kind: 'session', model: 'codex · 12113ec5ec27', tools: null }])
  })

  it('drops empty thinking blocks (redacted content)', () => {
    const text = line({
      type: 'assistant',
      message: { content: [{ type: 'thinking', thinking: '', signature: 'abc' }] },
    })

    expect(parseStream(text, true)).toEqual([])
  })
})
