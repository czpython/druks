import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RunState, SubjectStatus } from '../../api/types'
import type { PRResolution, WorkItemRow } from './api'
import { WorkItemsPage } from './WorkItemsPage'

const sse = vi.hoisted(() => ({
  handlers: null as null | Record<string, (data: unknown) => void>,
}))

vi.mock('../../api/sse', () => ({
  useSSE: (
    _url: string,
    options: { handlers: Record<string, (data: unknown) => void> },
  ) => {
    sse.handlers = options.handlers
  },
}))

function row(id: string, state: RunState, resolution: PRResolution | null): WorkItemRow {
  const status: SubjectStatus = {
    state,
    kind: 'ship.build',
    agent: null,
    gate: state === 'parked' ? 'review_work' : null,
    failure: state === 'failed' ? 'tests failed' : null,
    reason: null,
    triggeredAt: '2026-07-27T12:01:00Z',
    accountUsername: 'system',
  }
  return {
    summary: {
      id,
      source: 'linear',
      repo: 'czpython/druks',
      projectName: 'druks',
      title: `${state}-${id}`,
      ticketKey: `ENG-${id}`,
      ticketUrl: `https://linear.app/fellaworks/issue/ENG-${id}`,
      prNumber: Number(id),
      branch: `agent/eng-${id}`,
      resolution,
      createdAt: '2026-07-27T12:00:00Z',
      updatedAt: `2026-07-27T12:0${id}:00Z`,
      links: {
        repo: 'https://github.com/czpython/druks',
        pr: `https://github.com/czpython/druks/pull/${id}`,
        ticket: `https://linear.app/fellaworks/issue/ENG-${id}`,
      },
    },
    status,
  }
}

afterEach(() => {
  cleanup()
  sse.handlers = null
})

describe('WorkItemsPage', () => {
  it('filters resolution first and renders every active bucket in its count', () => {
    const { container } = render(<WorkItemsPage />)

    act(() => {
      sse.handlers?.snapshot({
        rows: [
          row('1', 'parked', null),
          row('2', 'failed', null),
          row('3', 'finished', null),
          row('4', 'scheduled', null),
          row('5', 'running', null),
          row('6', 'running', 'merged'),
        ],
      })
    })

    expect(screen.getByText('finished-3')).toBeTruthy()
    expect(screen.queryByText('running-6')).toBeNull()
    expect(container.querySelector('.dash-h1-count')?.textContent).toBe('(5)')
    expect(
      [...container.querySelectorAll('.wi-group-count')].map((node) => node.textContent),
    ).toEqual(['(3)', '(2)'])
    expect(screen.getByText('3 needs you')).toBeTruthy()
    expect(screen.getByText('2 in flight')).toBeTruthy()
  })
})
