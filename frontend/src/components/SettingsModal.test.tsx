import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SettingsModal } from './SettingsModal'

const extensionSettings = {
  allowedEfforts: [],
  extensions: [
    {
      name: 'ship',
      description: 'Ship settings',
      icon: 'ship',
      builtin: false,
      agents: [],
      workflows: [],
      settings: [
        {
          name: 'tracker',
          label: 'Tracker',
          help: '',
          type: 'enum',
          value: 'linear',
          default: 'linear',
          choices: ['none', 'linear', 'jira'],
          section: '',
          visibleWhenField: '',
          visibleWhenValue: null,
          secretSet: null,
          overridden: false,
        },
        {
          name: 'linear_api_key',
          label: 'Linear API key',
          help: '',
          type: 'secret',
          value: null,
          default: null,
          choices: null,
          section: 'Linear',
          visibleWhenField: 'tracker',
          visibleWhenValue: 'linear',
          secretSet: false,
          overridden: false,
        },
        {
          name: 'linear_webhook_secret',
          label: 'Linear webhook secret',
          help: '',
          type: 'secret',
          value: null,
          default: null,
          choices: null,
          section: 'Linear',
          visibleWhenField: 'tracker',
          visibleWhenValue: 'linear',
          secretSet: false,
          overridden: false,
        },
        {
          name: 'jira_webhook_secret',
          label: 'Jira webhook secret',
          help: '',
          type: 'secret',
          value: null,
          default: null,
          choices: null,
          section: 'Jira',
          visibleWhenField: 'tracker',
          visibleWhenValue: 'jira',
          secretSet: false,
          overridden: false,
        },
      ],
    },
    {
      name: 'review',
      description: 'Review settings',
      icon: 'git-pull-request',
      builtin: false,
      agents: [],
      workflows: [],
      settings: [
        {
          name: 'app_id',
          label: 'Review App ID',
          help: '',
          type: 'secret',
          value: null,
          default: null,
          choices: null,
          section: '',
          visibleWhenField: '',
          visibleWhenValue: null,
          secretSet: false,
          multiline: false,
          overridden: false,
        },
        {
          name: 'private_key',
          label: 'Review App private key',
          help: '',
          type: 'secret',
          value: null,
          default: null,
          choices: null,
          section: '',
          visibleWhenField: '',
          visibleWhenValue: null,
          secretSet: true,
          multiline: true,
          overridden: true,
        },
      ],
    },
  ],
}

function stubFetch(shouldRejectPatch = true) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/settings/extensions' && init?.method === 'PATCH') {
        if (!shouldRejectPatch) return new Response('{}', { status: 200 })
        return new Response(
          JSON.stringify({
            detail: {
              ship: {
                linear_api_key: 'The Linear API key is invalid.',
                linear_webhook_secret: 'Required once the Linear API key is set.',
              },
            },
          }),
          { status: 422, statusText: 'Unprocessable Entity' },
        )
      }
      if (path === '/api/settings/extensions') {
        return new Response(JSON.stringify(extensionSettings), { status: 200 })
      }
      if (path === '/api/settings/harnesses') {
        return new Response('[]', { status: 200 })
      }
      if (path === '/api/settings') {
        return new Response(JSON.stringify({ timezone: 'UTC', updatedAt: '2026-08-01T00:00:00Z' }), {
          status: 200,
        })
      }
      return new Response('{}', { status: 404 })
    }),
  )
}

function renderModal(onClose = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <SettingsModal open onClose={onClose} />
    </QueryClientProvider>,
  )
  return onClose
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('SettingsModal extension fields', () => {
  it('renders every 422 message under the field named by the backend', async () => {
    stubFetch()
    const onClose = renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'ship' }))
    const apiKeyField = screen.getByText('Linear API key').closest('.set-field')
    const apiKeyInput = apiKeyField?.querySelector('input')
    expect(apiKeyInput).toBeTruthy()
    fireEvent.change(apiKeyInput as HTMLInputElement, { target: { value: 'lin-secret' } })
    fireEvent.click(screen.getByRole('button', { name: 'save' }))

    const linearError = await screen.findByText('Required once the Linear API key is set.')
    const apiKeyError = await screen.findByText('The Linear API key is invalid.')
    expect(linearError.closest('.set-field')?.textContent).toContain('Linear webhook secret')
    expect(apiKeyError.closest('.set-field')?.textContent).toContain('Linear API key')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('reveals the chosen section immediately and prunes edits hidden before save', async () => {
    stubFetch(false)
    const onClose = renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'ship' }))
    const options = screen.getByText('ship options').closest('.set-group')
    expect(options?.textContent?.indexOf('Tracker')).toBeLessThan(
      options?.textContent?.indexOf('Linear') ?? -1,
    )
    const apiKeyField = screen.getByText('Linear API key').closest('.set-field')
    fireEvent.change(apiKeyField?.querySelector('input') as HTMLInputElement, {
      target: { value: 'lin-secret' },
    })
    const trackerField = screen.getByText('Tracker').closest('.set-field')
    fireEvent.change(trackerField?.querySelector('select') as HTMLSelectElement, {
      target: { value: 'jira' },
    })

    expect(screen.queryByText('Linear API key')).toBeNull()
    expect(screen.getByText('Jira webhook secret')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'save' }))

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    const patchCall = vi
      .mocked(fetch)
      .mock.calls.find(
        ([input, init]) =>
          String(input) === '/api/settings/extensions' && init?.method === 'PATCH',
      )
    const body = JSON.parse(String(patchCall?.[1]?.body))
    expect(body.extensionSettings.ship).toEqual({ tracker: 'jira' })
  })

  it('renders a multiline secret as a textarea and PATCHes the paste with newlines intact', async () => {
    stubFetch(false)
    const onClose = renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'review' }))
    const pemField = screen.getByText('Review App private key').closest('.set-field')
    const textarea = pemField?.querySelector('textarea')
    expect(textarea).toBeTruthy()
    // A stored secret never redisplays — the control shows only the set hint.
    expect((textarea as HTMLTextAreaElement).value).toBe('')
    expect((textarea as HTMLTextAreaElement).placeholder).toBe('•••••••• (set)')
    // The single-line secret sibling keeps its password input.
    const appIdField = screen.getByText('Review App ID').closest('.set-field')
    expect(appIdField?.querySelector('input')).toBeTruthy()
    expect(appIdField?.querySelector('textarea')).toBeNull()

    const pem =
      '-----BEGIN RSA PRIVATE KEY-----\nline-one\nline-two\n-----END RSA PRIVATE KEY-----'
    fireEvent.change(textarea as HTMLTextAreaElement, { target: { value: pem } })
    fireEvent.click(screen.getByRole('button', { name: 'save' }))

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    const patchCall = vi
      .mocked(fetch)
      .mock.calls.find(
        ([input, init]) =>
          String(input) === '/api/settings/extensions' && init?.method === 'PATCH',
      )
    const body = JSON.parse(String(patchCall?.[1]?.body))
    expect(body.extensionSettings.review).toEqual({ private_key: pem })
  })

  it('renders a 422 message for a field hidden by the tracker selection', async () => {
    stubFetch()
    renderModal()

    fireEvent.click(await screen.findByRole('button', { name: 'ship' }))
    const trackerField = screen.getByText('Tracker').closest('.set-field')
    fireEvent.change(trackerField?.querySelector('select') as HTMLSelectElement, {
      target: { value: 'jira' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'save' }))

    expect(
      await screen.findByText(
        'Linear webhook secret: Required once the Linear API key is set.',
      ),
    ).toBeTruthy()
  })
})
