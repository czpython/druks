import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SettingField } from './SettingField'

afterEach(cleanup)

describe('SettingField', () => {
  it('renders a textarea for a multiline field regardless of type', () => {
    // multiline is declared independent of type on the wire — a pasted long
    // description, not only a pasted secret, can carry newlines.
    render(
      <SettingField
        label="Notes"
        type="str"
        multiline
        value="line one\nline two"
        onChange={vi.fn()}
      />,
    )
    expect(screen.getByLabelText('Notes').tagName).toBe('TEXTAREA')
  })

  it('renders a password input for a non-multiline secret, with a set-ness placeholder', () => {
    render(
      <SettingField label="API key" type="secret" secretSet value="" onChange={vi.fn()} />,
    )
    const input = screen.getByLabelText('API key') as HTMLInputElement
    expect(input.tagName).toBe('INPUT')
    expect(input.type).toBe('password')
    expect(input.placeholder).toBe('•••••••• (set)')
  })

  it('shows the not-set placeholder when the secret has no stored value', () => {
    render(
      <SettingField label="API key" type="secret" secretSet={false} value="" onChange={vi.fn()} />,
    )
    expect(screen.getByPlaceholderText('not set')).toBeTruthy()
  })

  it('renders a textarea for a multiline secret, keeping the set-ness placeholder', () => {
    render(
      <SettingField label="Private key" type="secret" multiline secretSet value="" onChange={vi.fn()} />,
    )
    const field = screen.getByLabelText('Private key')
    expect(field.tagName).toBe('TEXTAREA')
    expect((field as HTMLTextAreaElement).placeholder).toBe('•••••••• (set)')
  })

  it('says so when an enum declares no choices, instead of a silent text box', () => {
    render(<SettingField label="Effort" type="enum" choices={[]} value="" onChange={vi.fn()} />)
    expect(screen.getByText('Effort declares no choices')).toBeTruthy()
    expect(screen.queryByLabelText('Effort')).toBeNull()
  })
})
