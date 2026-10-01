import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { Router } from 'wouter'
import { memoryLocation } from 'wouter/memory-location'

import { useSSE } from '../api/sse'
import type { Link, SubjectRow } from '../api/types'
import { hrefForLink } from '../druksui/pages'
import { AppHomePage } from './AppHomePage'

vi.mock('../api/sse', () => ({ useSSE: vi.fn() }))

afterEach(cleanup)

const PULL_REQUEST: SubjectRow = {
  summary: { id: 'acme/web#8105', key: 'acme/web#8105' },
  status: {
    state: null,
    run: null,
    kind: null,
    agent: null,
    gate: null,
    failure: null,
    reason: null,
    triggeredAt: null,
    accountUsername: null,
  },
}

it('opens a subject whose id holds a slash and a hash', () => {
  const { hook, history } = memoryLocation({ path: '/reviews', record: true })
  let snapshot: ((data: unknown) => void) | undefined
  vi.mocked(useSSE).mockImplementation((_url, { handlers }) => {
    snapshot = handlers.snapshot
  })
  render(
    <Router hook={hook}>
      <AppHomePage app="reviews" description="" subjectTypes={['pull_request']} />
    </Router>,
  )
  act(() => snapshot!({ rows: [PULL_REQUEST] }))
  fireEvent.click(screen.getByText('acme/web#8105'))
  expect(history.at(-1)).toBe('/reviews/pull_request/acme%2Fweb%238105')
})

it('encodes the subject id of a link for an app without its own subject path', () => {
  const link: Link = {
    block: 'link',
    label: 'Open',
    page: '',
    arguments: {},
    url: '',
    subject: { subjectType: 'pull_request', subjectId: 'acme/web#8105' },
  }
  expect(hrefForLink(link, 'reviews', [])).toBe('/reviews/pull_request/acme%2Fweb%238105')
})
