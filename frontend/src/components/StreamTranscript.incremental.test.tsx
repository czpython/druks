import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { StreamTranscript } from './StreamTranscript'

const eventLine = (text: string) =>
  JSON.stringify({ type: 'assistant', message: { content: [{ type: 'text', text }] } })

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('StreamTranscript incremental parsing', () => {
  it('parses only newly appended JSONL lines', () => {
    const first = eventLine('first row')
    const second = eventLine('second row')
    const parseSpy = vi.spyOn(JSON, 'parse')
    const { rerender } = render(<StreamTranscript text={`${first}\n`} />)

    expect(screen.getByText('first row')).toBeTruthy()

    rerender(<StreamTranscript text={`${first}\n${second}\n`} />)

    expect(screen.getByText('second row')).toBeTruthy()
    expect(screen.getAllByText(/^(first|second) row$/).map((node) => node.textContent)).toEqual([
      'first row',
      'second row',
    ])
    expect(parseSpy.mock.calls.filter(([input]) => input === first)).toHaveLength(1)
    expect(parseSpy.mock.calls.filter(([input]) => input === second)).toHaveLength(1)
  })

  it('buffers split lines and flushes an unterminated final line once', () => {
    const split = eventLine('split row')
    const final = eventLine('final row')
    const splitAt = Math.floor(split.length / 2)
    const { rerender } = render(<StreamTranscript text={split.slice(0, splitAt)} />)

    expect(screen.queryByText('split row')).toBeNull()

    rerender(<StreamTranscript text={`${split}\n`} />)
    expect(screen.getAllByText('split row')).toHaveLength(1)

    rerender(<StreamTranscript text={`${split}\n${final}`} />)
    expect(screen.queryByText('final row')).toBeNull()

    rerender(<StreamTranscript text={`${split}\n${final}`} complete />)
    expect(screen.getAllByText('split row')).toHaveLength(1)
    expect(screen.getAllByText('final row')).toHaveLength(1)

    rerender(<StreamTranscript text={`${split}\n${final}`} complete />)
    expect(screen.getAllByText('split row')).toHaveLength(1)
    expect(screen.getAllByText('final row')).toHaveLength(1)
  })
})
