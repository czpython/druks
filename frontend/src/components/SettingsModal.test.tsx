import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
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
          name: 'linear_api_key',
          label: 'Linear API key',
          help: '',
          type: 'secret',
          value: null,
          default: null,
          choices: null,
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
          secretSet: false,
          overridden: false,
        },
      ],
    },
  ],
}

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/settings/extensions' && init?.method === 'PATCH') {
        return new Response(
          JSON.stringify({
            detail: {
              ship: {
                linear_webhook_secret: 'Required once the Linear API key is set.',
                jira_webhook_secret: 'Required once the Jira API token is set.',
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

describe('SettingsModal extension coherence errors', () => {
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
    const jiraError = await screen.findByText('Required once the Jira API token is set.')
    expect(linearError.closest('.set-field')?.textContent).toContain('Linear webhook secret')
    expect(jiraError.closest('.set-field')?.textContent).toContain('Jira webhook secret')
    expect(onClose).not.toHaveBeenCalled()
  })
})
