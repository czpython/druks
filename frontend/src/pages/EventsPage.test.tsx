import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Link, Route, Router, Switch } from 'wouter'
import { navigate } from 'wouter/use-browser-location'

import { api, identityApi } from '../api/client'
import type { App, FeedItem } from '../api/types'
import { registerAppUI, targetQuery } from '../apps/registry'
import { UserPreferencesProvider } from '../lib/preferences'
import { EventsPage } from './EventsPage'
import '../apps/software_factory/ui'

class FeedSource extends EventTarget {
  static instances: FeedSource[] = []
  closed = false
  url: string
  constructor(url: string) {
    super()
    this.url = url
    FeedSource.instances.push(this)
  }
  close() { this.closed = true }
  emit(type: string, data?: FeedItem | { cursor: string }) {
    this.dispatchEvent(data ? new MessageEvent(type, { data: JSON.stringify(data) }) : new Event(type))
  }
}
const result: FeedItem = {
  id: 'event:10', seq: 10, at: '2026-09-09T15:00:00Z', topic: 'gist.prepared', app: 'field_notes',
  subjectType: 'note', subjectId: '7', subjectLabel: 'Pump A',
  payload: { run: 'run-ten', artifact_id: 'saved-ten', summary: 'The pump ran hot.' },
}
const app: App = {
  name: 'field_notes', builtin: false, description: '', icon: 'box', hasFrontend: false,
  subjectTypes: ['note'], navigation: [], pages: [], operations: [],
}
const history = vi.spyOn(api, 'listEvents')
const topics = vi.spyOn(api, 'listEventTopics')
const destinations = vi.spyOn(api, 'getEventDestinations')
const artifact = vi.spyOn(api, 'artifact')
const settings = vi.spyOn(api, 'getPersonalSettings')
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><UserPreferencesProvider><Router><Switch>
    <Route path="/events"><EventsPage /></Route>
    <Route><h1>Owner page</h1><Link href="/events">Activity</Link></Route>
  </Switch></Router></UserPreferencesProvider></QueryClientProvider>)
  return client
}
function source() { return FeedSource.instances.at(-1)! }

beforeEach(() => {
  window.history.replaceState(null, '', '/events')
  FeedSource.instances = []
  vi.stubGlobal('EventSource', FeedSource)
  HTMLElement.prototype.scrollTo = vi.fn()
  history.mockResolvedValue({ items: [result], streamCursor: '10:20:10', nextCursor: null })
  topics.mockResolvedValue([{ app: 'field_notes', topic: 'gist.prepared' }, { app: 'field_notes', topic: 'older.kind' }])
  destinations.mockResolvedValue({ isSubjectAvailable: true, isRunAvailable: true, isArtifactAvailable: true })
  artifact.mockResolvedValue({ kind: 'markdown', title: 'Gist', content: '# Saved finding\nExact old result.' })
  settings.mockResolvedValue({ timezone: 'UTC', gateParkDestinationId: null })
  vi.spyOn(api, 'listApps').mockResolvedValue([app, { ...app, name: 'core', builtin: true }, { ...app, name: 'usage', builtin: true }])
  vi.spyOn(identityApi, 'me').mockResolvedValue({ authMode: 'none', account: { id: 'a', username: 'op', isDefault: true }, onboardingRequired: false })
  registerAppUI({ name: 'field_notes', routes: [], subjectPath: ({ id }, target) => `/notes/${id}${targetQuery(target)}` })
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals() })

it('shows exact saved results and restores row focus after Close and Escape', async () => {
  mount()
  const row = await screen.findByRole('button', { name: /Pump A.*Gist prepared/ })
  fireEvent.click(row)
  const details = screen.getByRole('complementary', { name: 'Activity details' })
  expect(await within(details).findByText('Exact old result.')).toBeTruthy()
  expect(artifact).toHaveBeenCalledWith('saved-ten')
  expect((await within(details).findByRole('link', { name: 'Open work' })).getAttribute('href')).toBe('/notes/7?run=run-ten')
  expect(document.activeElement).toBe(within(details).getByRole('heading', { name: 'Gist prepared' }))
  fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
  expect(screen.queryByRole('complementary')).toBeNull()
  expect(document.activeElement).toBe(row)
  fireEvent.click(row)
  screen.getByRole('heading', { name: 'Gist prepared' }).blur()
  fireEvent.keyDown(document.body, { key: 'Escape' })
  expect(screen.queryByRole('complementary')).toBeNull()
  expect(document.activeElement).toBe(row)
  fireEvent.click(row)
  fireEvent.click(screen.getByRole('button', { name: 'Close details' }))
  expect(document.activeElement).toBe(row)
})

it('keeps full type choices during search, pagination, and live updates', async () => {
  history.mockImplementation(async (params) => params?.before
    ? { items: [{ ...result, id: 'event:2', seq: 2, topic: 'older.kind' }], streamCursor: '10:20:10', nextCursor: null }
    : { items: [result], streamCursor: '10:20:10', nextCursor: '10' })
  mount()
  await screen.findByRole('button', { name: /Pump A.*Gist prepared/ })
  fireEvent.click(screen.getByRole('button', { name: 'Load older activity' }))
  await screen.findByRole('button', { name: /Pump A.*Older kind/ })
  const input = screen.getByRole('searchbox', { name: 'Search work' })
  input.focus()
  fireEvent.change(input, { target: { value: '50' } })
  fireEvent.change(input, { target: { value: '50%_pump' } })
  await waitFor(() => expect(history).toHaveBeenLastCalledWith(expect.objectContaining({ q: '50%_pump', before: undefined })))
  expect(history).not.toHaveBeenCalledWith(expect.objectContaining({ q: '50' }))
  expect(document.activeElement).toBe(input)
  fireEvent.click(screen.getByRole('button', { name: 'Filters' }))
  expect(screen.queryByRole('option', { name: 'Core' })).toBeNull()
  expect(screen.queryByRole('option', { name: 'Usage' })).toBeNull()
  expect(screen.getByRole('option', { name: 'Older kind · Field Notes' })).toBeTruthy()
  fireEvent.change(screen.getByRole('combobox', { name: 'Activity type' }), { target: { value: JSON.stringify(['field_notes', 'older.kind']) } })
  await waitFor(() => expect(history).toHaveBeenLastCalledWith(expect.objectContaining({ q: '50%_pump', topic: 'older.kind' })))
  act(() => source().emit('message', { ...result, id: 'event:30', seq: 30, topic: 'new.kind' }))
  expect(screen.queryByRole('option', { name: 'New kind' })).toBeNull()
  expect(topics).toHaveBeenCalledTimes(2)
  fireEvent.change(screen.getByRole('combobox', { name: 'App' }), { target: { value: 'field_notes' } })
  await waitFor(() => expect(history).toHaveBeenLastCalledWith(expect.objectContaining({ app: 'field_notes', topic: undefined })))
  expect(topics).toHaveBeenLastCalledWith('field_notes')
})

it.each(['field_notes', 'software_factory'])('selects the owning app for %s topics in history and the stream', async (owner) => {
  registerAppUI({ name: 'field_notes', routes: [], activityLabel: ({ topic }) => topic === 'merged' ? 'Notes combined' : undefined })
  topics.mockResolvedValue([
    { app: 'field_notes', topic: 'merged' },
    { app: 'software_factory', topic: 'merged' },
    { app: 'field_notes', topic: 'workflow.failed' },
    { app: 'software_factory', topic: 'workflow.failed' },
  ])
  mount()
  fireEvent.click(screen.getByRole('button', { name: 'Filters' }))
  await screen.findByRole('option', { name: 'Notes combined · Field Notes' })
  expect(screen.getByRole('option', { name: 'Pull request merged · Software Factory' })).toBeTruthy()
  expect(screen.getByRole('option', { name: 'Failed · Field Notes' })).toBeTruthy()
  expect(screen.getByRole('option', { name: 'Failed · Software Factory' })).toBeTruthy()
  fireEvent.change(screen.getByRole('combobox', { name: 'Activity type' }), {
    target: { value: JSON.stringify([owner, 'merged']) },
  })
  await waitFor(() => expect(history).toHaveBeenLastCalledWith(expect.objectContaining({ app: owner, topic: 'merged' })))
  await waitFor(() => expect(new URL(source().url, window.location.origin).searchParams.get('app')).toBe(owner))
  expect(new URL(source().url, window.location.origin).searchParams.get('topic')).toBe('merged')
  expect(new URLSearchParams(window.location.search).get('app')).toBe(owner)
  expect(screen.getByRole('option', { name: owner === 'field_notes' ? 'Notes combined' : 'Pull request merged' })).toBeTruthy()
})

it('buffers and deduplicates arrivals while reading, then resumes from the last complete snapshot', async () => {
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Pump A.*Gist prepared/ }))
  const connection = source()
  act(() => {
    source().emit('open')
    source().emit('message', { ...result, id: 'event:12', seq: 12, subjectLabel: 'New work' })
    source().emit('message', { ...result, id: 'event:12', seq: 12, subjectLabel: 'New work' })
    source().emit('batch-end', { cursor: '10:30:10' })
  })
  expect(screen.getByRole('button', { name: 'New activity · 1' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: /New work.*Gist prepared/ })).toBeNull()
  expect(source()).toBe(connection)
  fireEvent.click(screen.getByRole('button', { name: 'Pause updates' }))
  expect(connection.closed).toBe(true)
  expect(screen.getByText('Paused')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Resume updates' }))
  expect(new URL(source().url, window.location.origin).searchParams.get('after')).toBe('10:30:10')
  act(() => {
    source().emit('message', { ...result, id: 'event:9', seq: 9, subjectLabel: 'Late work' })
    source().emit('message', { ...result, id: 'event:9', seq: 9, subjectLabel: 'Late work' })
  })
  await act(async () => source().emit('error'))
  expect(screen.getByText('Reconnecting')).toBeTruthy()
  act(() => source().emit('open'))
  expect(screen.getByText('Live')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'New activity · 2' }))
  expect(await screen.findByRole('button', { name: /New work.*Gist prepared/ })).toBeTruthy()
  expect(screen.getAllByRole('button', { name: /Late work.*Gist prepared/ })).toHaveLength(1)
  expect(screen.getByRole('complementary')).toBeTruthy()
})

it('resumes an interrupted batch and shows each replayed row once', async () => {
  mount()
  await screen.findByRole('button', { name: /Pump A.*Gist prepared/ })
  const connection = source()
  const received = { ...result, id: 'event:12', seq: 12, subjectLabel: 'Received work' }
  const late = { ...result, id: 'event:9', seq: 9, subjectLabel: 'Late work' }
  act(() => connection.emit('message', received))
  fireEvent.click(screen.getByRole('button', { name: 'Pause updates' }))
  expect(connection.closed).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Resume updates' }))
  expect(source()).not.toBe(connection)
  expect(new URL(source().url, window.location.origin).searchParams.get('after')).toBe('10:20:10')
  const resumed = source()
  act(() => {
    resumed.emit('message', received)
    resumed.emit('message', late)
    resumed.emit('message', late)
    resumed.emit('batch-end', { cursor: '30:40:' })
  })
  expect(source()).toBe(resumed)
  expect(screen.getAllByRole('button', { name: /Received work.*Gist prepared/ })).toHaveLength(1)
  expect(screen.getAllByRole('button', { name: /Late work.*Gist prepared/ })).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Pause updates' }))
  fireEvent.click(screen.getByRole('button', { name: 'Resume updates' }))
  expect(new URL(source().url, window.location.origin).searchParams.get('after')).toBe('30:40:')
})

it('starts live updates on an empty feed from its read snapshot', async () => {
  history.mockResolvedValue({ items: [], streamCursor: '10:20:10', nextCursor: null })
  mount()
  await screen.findByText('No activity matches these filters.')
  expect(new URL(source().url, window.location.origin).searchParams.get('after')).toBe('10:20:10')
  act(() => source().emit('message', result))
  expect(await screen.findByRole('button', { name: /Pump A.*Gist prepared/ })).toBeTruthy()
})

it('retries failed reads and shows missing destinations without hiding historical facts', async () => {
  history.mockRejectedValueOnce(new Error('Offline'))
  mount()
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Could not load activity'))
  history.mockResolvedValue({ items: [result], streamCursor: '10:20:10', nextCursor: null })
  destinations.mockResolvedValue({ isSubjectAvailable: false, isRunAvailable: false, isArtifactAvailable: false })
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  fireEvent.click(await screen.findByRole('button', { name: /Pump A.*Gist prepared/ }))
  expect(await screen.findByText('This saved result is no longer available.')).toBeTruthy()
  expect(destinations).toHaveBeenCalledWith(10)
  expect(screen.getByText('Work destination unavailable.')).toBeTruthy()
  expect(screen.getByText('This run is no longer available.')).toBeTruthy()
})

it('shows the saved result when the destination check fails, then retries the check', async () => {
  destinations.mockRejectedValueOnce(new Error('Offline'))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Pump A.*Gist prepared/ }))
  expect(await screen.findByText('Exact old result.')).toBeTruthy()
  fireEvent.click(await screen.findByRole('button', { name: 'Retry destinations' }))
  expect(await screen.findByRole('link', { name: 'Open work' })).toBeTruthy()
})

it('reads a selection outside the loaded pages and leaves no gap when it closes', async () => {
  window.history.replaceState(null, '', '/events?selected=5')
  history.mockImplementation(async (params) => params?.limit === 1
    ? { items: [{ ...result, id: 'event:5', seq: 5, subjectLabel: 'Pump E' }], streamCursor: '10:20:10', nextCursor: null }
    : { items: [result], streamCursor: '10:20:10', nextCursor: '10' })
  mount()
  const details = await screen.findByRole('complementary', { name: 'Activity details' })
  expect(await within(details).findByText('Pump E')).toBeTruthy()
  fireEvent.click(within(details).getByRole('button', { name: 'Close details' }))
  expect(screen.queryByRole('button', { name: /Pump E/ })).toBeNull()
  expect(document.activeElement).toBe(screen.getByRole('list', { name: 'Activity history' }))
})

it('shows past request facts and preserves filters and selection after returning from the owner', async () => {
  window.history.replaceState(null, '', '/events?app=field_notes&q=Pump')
  history.mockResolvedValue({ items: [{ ...result, topic: 'workflow.parked',
    payload: { ...result.payload, artifact_id: null, gate: 'review',
      input_requested_at: '2026-09-09T15:00:00.123456Z', input_request: {
      presentation: 'in_app', controls: ['approve'], context: 'Recorded context',
    } } }], streamCursor: '10:20:10', nextCursor: null })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Pump A.*Input requested/ }))
  const returnUrl = window.location.pathname + window.location.search
  const owner = await screen.findByRole('link', { name: 'Open work' })
  expect(new URL(owner.getAttribute('href')!, window.location.origin).searchParams.get('parkedAt')).toBe('2026-09-09T15:00:00.123456Z')
  expect(screen.getByText('Input was requested')).toBeTruthy()
  fireEvent.click(owner)
  await screen.findByRole('heading', { name: 'Owner page' })
  act(() => navigate(returnUrl))
  await screen.findByRole('complementary')
  expect(screen.getByRole('searchbox')).toHaveProperty('value', 'Pump')
  expect(new URLSearchParams(window.location.search).get('selected')).toBe('10')
})

it('retries the exact saved artifact and replaces it on another selection', async () => {
  history.mockResolvedValue({ items: [result, { ...result, id: 'event:9', seq: 9, payload: { ...result.payload, artifact_id: 'saved-nine' }, subjectLabel: 'Pump B' }], streamCursor: '10:20:10', nextCursor: null })
  artifact.mockRejectedValueOnce(new Error('Offline'))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Pump A.*Gist prepared/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Retry result' }))
  await screen.findByText('Exact old result.')
  fireEvent.click(screen.getByRole('button', { name: /Pump B.*Gist prepared/ }))
  await waitFor(() => expect(artifact).toHaveBeenLastCalledWith('saved-nine'))
})

it('shows Factory review findings through the shared saved-result renderer', async () => {
  history.mockResolvedValue({ items: [{ ...result, app: 'software_factory', topic: 'review.completed',
    subjectType: 'work_item', subjectId: '42', subjectLabel: 'DRU-42', payload: { ...result.payload, artifact_id: 'review-ten' },
  }], streamCursor: '10:20:10', nextCursor: null })
  artifact.mockResolvedValue({ kind: 'markdown', title: 'Review', content: '## Missing validation\nRecorded evidence.\n\nSource: backend/app.py:12' })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /DRU-42.*Review completed/ }))
  const details = screen.getByRole('complementary', { name: 'Activity details' })
  expect(await within(details).findByRole('heading', { name: 'Missing validation' })).toBeTruthy()
  expect(within(details).getByText('Recorded evidence.')).toBeTruthy()
  expect(artifact).toHaveBeenCalledWith('review-ten')
  expect((await within(details).findByRole('link', { name: 'Open work' })).getAttribute('href')).toBe('/software_factory/work-items/42?run=run-ten')
})

it('sends the operator timezone day boundaries to history and the live stream', async () => {
  settings.mockResolvedValue({ timezone: 'Europe/Madrid', gateParkDestinationId: null })
  mount()
  await screen.findByRole('button', { name: /Pump A.*Gist prepared/ })
  await waitFor(() => expect(screen.getByText('17:00')).toBeTruthy())
  fireEvent.click(screen.getByRole('button', { name: 'Date range' }))
  fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-08' } })
  fireEvent.change(screen.getByLabelText('Through'), { target: { value: '2026-09-09' } })
  await waitFor(() => expect(history).toHaveBeenLastCalledWith(expect.objectContaining({
    from: '2026-09-07T22:00:00.000Z', until: '2026-09-09T22:00:00.000Z',
  })))
  await waitFor(() => expect(new URL(source().url, window.location.origin).searchParams.get('until')).toBe('2026-09-09T22:00:00.000Z'))
  expect(new URL(source().url, window.location.origin).searchParams.get('from')).toBe('2026-09-07T22:00:00.000Z')
})

it('shows a classified failure and preserves its original message in a disclosure', async () => {
  const failure = 'codex exited with 1. You hit your spend cap set by the owner of your workspace.'
  history.mockResolvedValue({ items: [{ ...result, app: 'software_factory', topic: 'workflow.failed',
    subjectType: 'work_item', subjectId: '42', subjectLabel: 'DRU-42', payload: {
      kind: 'software_factory.build', run: 'failed-attempt', title: 'Keep the recorded title', failure_code: 'spend_cap', failure,
    },
  }], streamCursor: '10:20:10', nextCursor: null })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /DRU-42.*Keep the recorded title.*Build failed/ }))
  const details = screen.getByRole('complementary', { name: 'Activity details' })
  expect(within(details).getByText('Workspace spend cap reached')).toBeTruthy()
  expect(within(details).getByText('Ask a workspace owner to increase the spend cap before continuing.')).toBeTruthy()
  const technical = within(details).getByText('Technical details').closest('details')!
  expect(technical.open).toBe(false)
  expect(within(technical).getByText(failure)).toBeTruthy()
  expect((await within(details).findByRole('link', { name: 'Open work' })).getAttribute('href'))
    .toBe('/software_factory/work-items/42?run=failed-attempt')
})

it('shows the received Factory reply above its unchanged recorded values', async () => {
  history.mockResolvedValue({ items: [{ ...result, app: 'software_factory', topic: 'workflow.running',
    subjectType: 'work_item', subjectId: '42', payload: {
      kind: 'software_factory.build', run: 'review-attempt', gate: 'review_work',
      input_requested_at: '2026-09-09T15:00:00.123456Z',
      result: { action: 'approve', note: 'The tests cover this change.', answers: { coverage: 'The rollback path is covered.' } },
    },
  }], streamCursor: '10:20:10', nextCursor: null })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Implementation review · Reply: Approve/ }))
  const details = screen.getByRole('complementary', { name: 'Activity details' })
  expect(within(details).getByText('Implementation review · Reply: Approve')).toBeTruthy()
  expect(within(details).getByText('coverage')).toBeTruthy()
  expect(within(details).getAllByText(/The rollback path is covered/).length).toBeGreaterThan(0)
  const recorded = within(details).getByText('Recorded response').closest('details')!
  expect(recorded.open).toBe(false)
  expect(recorded.textContent).toContain('"action": "approve"')
  expect(recorded.textContent).toContain('The tests cover this change.')
  const target = new URL((await within(details).findByRole('link', { name: 'Open work' })).getAttribute('href')!, window.location.origin)
  expect(target.searchParams.get('run')).toBe('review-attempt')
  expect(target.searchParams.get('parkedAt')).toBe('2026-09-09T15:00:00.123456Z')
})

it('opens keyboard controls and closes them before the selected detail', async () => {
  mount()
  const row = await screen.findByRole('button', { name: /Pump A.*Gist prepared/ })
  expect(screen.queryByRole('combobox')).toBeNull()
  expect(screen.queryByLabelText('From')).toBeNull()
  fireEvent.click(row)
  const filters = screen.getByRole('button', { name: 'Filters' })
  fireEvent.click(filters)
  expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'App' }))
  expect(filters.getAttribute('aria-expanded')).toBe('true')
  fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
  expect(screen.queryByRole('group', { name: 'Activity filters' })).toBeNull()
  expect(screen.getByRole('complementary')).toBeTruthy()
  expect(document.activeElement).toBe(filters)
  const dates = screen.getByRole('button', { name: 'Date range' })
  fireEvent.click(dates)
  expect(document.activeElement).toBe(screen.getByLabelText('From'))
  expect(screen.queryByText(/Dates in/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Close filter control' }))
  expect(document.activeElement).toBe(dates)
  fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
  expect(screen.queryByRole('complementary')).toBeNull()
  expect(document.activeElement).toBe(row)
})

it('shows active values and clears only the controls in the open panel', async () => {
  window.history.replaceState(null, '', '/events?app=field_notes&topic=gist.prepared&q=Pump&start=2026-09-08&end=2026-09-09')
  mount()
  await screen.findByRole('button', { name: /Pump A.*Gist prepared/ })
  const active = screen.getByLabelText('Active filters')
  expect(active.textContent).toContain('Search: Pump')
  expect(active.textContent).toContain('Field Notes')
  expect(active.textContent).toContain('Gist prepared')
  expect(active.textContent).toContain('From 2026-09-08')
  expect(active.textContent).toContain('Through 2026-09-09')
  fireEvent.click(screen.getByRole('button', { name: /Filters/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Clear app and type' }))
  await waitFor(() => expect(history).toHaveBeenLastCalledWith(expect.objectContaining({ app: undefined, topic: undefined, q: 'Pump', from: '2026-09-08T00:00:00.000Z' })))
  fireEvent.click(screen.getByRole('button', { name: 'Close filter control' }))
  fireEvent.click(screen.getByRole('button', { name: 'Date range' }))
  fireEvent.click(screen.getByRole('button', { name: 'Clear dates' }))
  await waitFor(() => expect(history).toHaveBeenLastCalledWith(expect.objectContaining({ from: undefined, until: undefined, q: 'Pump' })))
  fireEvent.click(screen.getByRole('button', { name: 'Close filter control' }))
  fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
  expect(window.location.search).toBe('')
  expect(screen.queryByLabelText('Active filters')).toBeNull()
})

it('groups consecutive rows by the account day across daylight saving and pagination', async () => {
  settings.mockResolvedValue({ timezone: 'Europe/Madrid', gateParkDestinationId: null })
  history.mockImplementation(async (params) => params?.before
    ? { items: [
      { ...result, id: 'event:8', seq: 8, at: '2026-10-25T00:30:00Z' },
      { ...result, id: 'event:7', seq: 7, at: '2026-10-24T21:30:00Z' },
    ], streamCursor: '10:20:10', nextCursor: null }
    : { items: [
      { ...result, at: '2026-10-25T23:30:00Z' },
      { ...result, id: 'event:9', seq: 9, at: '2026-10-25T01:30:00Z' },
    ], streamCursor: '10:20:10', nextCursor: '9' })
  mount()
  await screen.findByRole('heading', { name: '26 October 2026' })
  fireEvent.click(screen.getByRole('button', { name: 'Load older activity' }))
  await screen.findByRole('heading', { name: '24 October 2026' })
  expect(screen.getAllByRole('heading', { name: '25 October 2026' })).toHaveLength(1)
  expect(screen.getAllByText('02:30')).toHaveLength(2)
  expect(screen.getByText('00:30')).toBeTruthy()
  expect(screen.getByText('23:30')).toBeTruthy()
})

it('keeps the full recorded title and failure in details when the row uses an excerpt', async () => {
  const title = 'Keep all of this recorded title '.repeat(8)
  const failure = 'The repository cannot be opened. '.repeat(16)
  history.mockResolvedValue({ items: [{ ...result, topic: 'workflow.failed', payload: { title, failure } }], streamCursor: '10:20:10', nextCursor: null })
  mount()
  const row = await screen.findByRole('button', { name: /Pump A.*Keep all of this recorded title/ })
  expect(row.textContent).not.toContain(failure)
  fireEvent.click(row)
  const details = screen.getByRole('complementary')
  expect(within(details).getByText(title.trim())).toBeTruthy()
  const technical = within(details).getByText('Technical details').closest('details')!
  expect(technical.textContent).toContain(failure)
  expect(technical.open).toBe(false)
  expect(within(details).queryByText('Harness')).toBeNull()
})
