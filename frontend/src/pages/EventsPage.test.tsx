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
  emit(type: string, data?: FeedItem) {
    this.dispatchEvent(data ? new MessageEvent(type, { data: JSON.stringify(data) }) : new Event(type))
  }
}
const result: FeedItem = {
  id: 'event:10', seq: 10, at: '2026-09-09T15:00:00Z', kind: 'gist.prepared', app: 'field_notes',
  subjectType: 'note', subjectId: '7', subjectLabel: 'Pump A', run: 'run-ten',
  artifactId: 'saved-ten', summary: 'The pump ran hot.',
}
const app: App = {
  name: 'field_notes', builtin: false, description: '', icon: 'box', hasFrontend: false,
  subjectTypes: ['note'], navigation: [], pages: [], operations: [],
}
const history = vi.spyOn(api, 'listEvents')
const kinds = vi.spyOn(api, 'listEventKinds')
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
  history.mockResolvedValue({ items: [result], nextCursor: null })
  kinds.mockResolvedValue(['gist.prepared', 'older.kind'])
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
  const row = await screen.findByRole('button', { name: /Gist prepared Pump A/ })
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
    ? { items: [{ ...result, id: 'event:2', seq: 2, kind: 'older.kind' }], nextCursor: null }
    : { items: [result], nextCursor: '10' })
  mount()
  await screen.findByRole('button', { name: /Gist prepared Pump A/ })
  expect(screen.queryByRole('option', { name: 'Core' })).toBeNull()
  expect(screen.queryByRole('option', { name: 'Usage' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Load older activity' }))
  await screen.findByRole('button', { name: /Older kind Pump A/ })
  const input = screen.getByRole('searchbox', { name: 'Search work labels' })
  input.focus()
  fireEvent.change(input, { target: { value: '50' } })
  fireEvent.change(input, { target: { value: '50%_pump' } })
  await waitFor(() => expect(history).toHaveBeenLastCalledWith(expect.objectContaining({ q: '50%_pump', before: undefined })))
  expect(history).not.toHaveBeenCalledWith(expect.objectContaining({ q: '50' }))
  expect(document.activeElement).toBe(input)
  expect(screen.getByRole('option', { name: 'Older kind' })).toBeTruthy()
  fireEvent.change(screen.getByRole('combobox', { name: 'Activity type' }), { target: { value: 'older.kind' } })
  await waitFor(() => expect(history).toHaveBeenLastCalledWith(expect.objectContaining({ q: '50%_pump', kind: 'older.kind' })))
  act(() => source().emit('message', { ...result, id: 'event:30', seq: 30, kind: 'new.kind' }))
  expect(screen.queryByRole('option', { name: 'New kind' })).toBeNull()
  expect(kinds).toHaveBeenCalledTimes(1)
  fireEvent.change(screen.getByRole('combobox', { name: 'App' }), { target: { value: 'field_notes' } })
  await waitFor(() => expect(history).toHaveBeenLastCalledWith(expect.objectContaining({ app: 'field_notes', kind: 'older.kind' })))
  expect(kinds).toHaveBeenLastCalledWith('field_notes')
})

it('buffers and deduplicates arrivals while reading, then resumes after the last received sequence', async () => {
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Gist prepared Pump A/ }))
  act(() => {
    source().emit('open')
    source().emit('message', { ...result, id: 'event:12', seq: 12, subjectLabel: 'New work' })
    source().emit('message', { ...result, id: 'event:12', seq: 12, subjectLabel: 'New work' })
  })
  expect(screen.getByRole('button', { name: 'New activity · 1' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: /Gist prepared New work/ })).toBeNull()
  const connection = source()
  fireEvent.click(screen.getByRole('button', { name: 'Pause updates' }))
  expect(connection.closed).toBe(true)
  expect(screen.getByText('Paused')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Resume updates' }))
  expect(new URL(source().url, window.location.origin).searchParams.get('after')).toBe('12')
  await act(async () => source().emit('error'))
  expect(screen.getByText('Reconnecting')).toBeTruthy()
  act(() => source().emit('open'))
  expect(screen.getByText('Live')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'New activity · 1' }))
  expect(await screen.findByRole('button', { name: /Gist prepared New work/ })).toBeTruthy()
  expect(screen.getByRole('complementary')).toBeTruthy()
})

it('starts live updates on an empty feed without a cursor', async () => {
  history.mockResolvedValue({ items: [], nextCursor: null })
  mount()
  await screen.findByText('No activity matches these filters.')
  expect(new URL(source().url, window.location.origin).searchParams.has('after')).toBe(false)
  act(() => source().emit('message', result))
  expect(await screen.findByRole('button', { name: /Gist prepared Pump A/ })).toBeTruthy()
})

it('retries failed reads and shows missing destinations without hiding historical facts', async () => {
  history.mockRejectedValueOnce(new Error('Offline'))
  mount()
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Could not load activity'))
  history.mockResolvedValue({ items: [result], nextCursor: null })
  destinations.mockResolvedValue({ isSubjectAvailable: false, isRunAvailable: false, isArtifactAvailable: false })
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  fireEvent.click(await screen.findByRole('button', { name: /Gist prepared Pump A/ }))
  expect(await screen.findByText('This saved result is no longer available.')).toBeTruthy()
  expect(destinations).toHaveBeenCalledWith(10)
  expect(screen.getByText('Work destination unavailable.')).toBeTruthy()
  expect(screen.getByText('This run is no longer available.')).toBeTruthy()
})

it('shows the saved result when the destination check fails, then retries the check', async () => {
  destinations.mockRejectedValueOnce(new Error('Offline'))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Gist prepared Pump A/ }))
  expect(await screen.findByText('Exact old result.')).toBeTruthy()
  fireEvent.click(await screen.findByRole('button', { name: 'Retry destinations' }))
  expect(await screen.findByRole('link', { name: 'Open work' })).toBeTruthy()
})

it('reads a selection outside the loaded pages and leaves no gap when it closes', async () => {
  window.history.replaceState(null, '', '/events?selected=5')
  history.mockImplementation(async (params) => params?.limit === 1
    ? { items: [{ ...result, id: 'event:5', seq: 5, subjectLabel: 'Pump E' }], nextCursor: null }
    : { items: [result], nextCursor: '10' })
  mount()
  const details = await screen.findByRole('complementary', { name: 'Activity details' })
  expect(await within(details).findByText('Pump E')).toBeTruthy()
  fireEvent.click(within(details).getByRole('button', { name: 'Close details' }))
  expect(screen.queryByRole('button', { name: /Pump E/ })).toBeNull()
  expect(document.activeElement).toBe(screen.getByRole('list', { name: 'Activity history' }))
})

it('shows past request facts and preserves filters and selection after returning from the owner', async () => {
  window.history.replaceState(null, '', '/events?app=field_notes&q=Pump')
  history.mockResolvedValue({ items: [{ ...result, artifactId: null, kind: 'workflow.parked',
    gate: 'review', parkedAt: '2026-09-09T15:00:00.123456Z', inputRequest: {
      presentation: 'in_app', controls: ['approve'], context: 'Recorded context',
    } }], nextCursor: null })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Input requested Pump A/ }))
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
  history.mockResolvedValue({ items: [result, { ...result, id: 'event:9', seq: 9, artifactId: 'saved-nine', subjectLabel: 'Pump B' }], nextCursor: null })
  artifact.mockRejectedValueOnce(new Error('Offline'))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Gist prepared Pump A/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Retry result' }))
  await screen.findByText('Exact old result.')
  fireEvent.click(screen.getByRole('button', { name: /Gist prepared Pump B/ }))
  await waitFor(() => expect(artifact).toHaveBeenLastCalledWith('saved-nine'))
})

it('shows Factory review findings through the shared saved-result renderer', async () => {
  history.mockResolvedValue({ items: [{ ...result, app: 'software_factory', kind: 'review.completed',
    subjectType: 'work_item', subjectId: '42', subjectLabel: 'DRU-42', artifactId: 'review-ten',
  }], nextCursor: null })
  artifact.mockResolvedValue({ kind: 'markdown', title: 'Review', content: '## Missing validation\nRecorded evidence.\n\nSource: backend/app.py:12' })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: /Review completed DRU-42/ }))
  const details = screen.getByRole('complementary', { name: 'Activity details' })
  expect(await within(details).findByRole('heading', { name: 'Missing validation' })).toBeTruthy()
  expect(within(details).getByText('Recorded evidence.')).toBeTruthy()
  expect(artifact).toHaveBeenCalledWith('review-ten')
  expect((await within(details).findByRole('link', { name: 'Open work' })).getAttribute('href')).toBe('/software_factory/work-items/42?run=run-ten')
})

it('sends the operator timezone day boundaries to history and the live stream', async () => {
  settings.mockResolvedValue({ timezone: 'Europe/Madrid', gateParkDestinationId: null })
  mount()
  await screen.findByText('Dates in Europe/Madrid')
  fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-08' } })
  fireEvent.change(screen.getByLabelText('Through'), { target: { value: '2026-09-09' } })
  await waitFor(() => expect(history).toHaveBeenLastCalledWith(expect.objectContaining({
    from: '2026-09-07T22:00:00.000Z', until: '2026-09-09T22:00:00.000Z',
  })))
  await waitFor(() => expect(new URL(source().url, window.location.origin).searchParams.get('until')).toBe('2026-09-09T22:00:00.000Z'))
  expect(new URL(source().url, window.location.origin).searchParams.get('from')).toBe('2026-09-07T22:00:00.000Z')
})
