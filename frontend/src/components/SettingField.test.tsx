import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SettingField } from './SettingField'

afterEach(cleanup)

describe('SettingField', () => {
  it('groups and searches live choices without changing the selected value', () => {
    const onChange = vi.fn()
    render(<SettingField
      label="Trigger status" type="choices" value="Ready" onChange={onChange}
      choices={['Ready', 'Working', 'Complete']}
      choiceDetails={{
        Ready: { label: 'Ready', help: '', group: 'To do' },
        Working: { label: 'Working', help: '', group: 'In progress' },
        Complete: { label: 'Complete', help: '', group: 'Done' },
      }}
    />)
    expect(screen.getByRole('group', { name: 'In progress' })).toBeTruthy()
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search Trigger status' }), { target: { value: 'progress' } })
    expect(screen.getByRole('option', { name: 'Working' })).toBeTruthy()
    expect(screen.queryByRole('option', { name: 'Complete' })).toBeNull()
    expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('Ready')
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'missing' } })
    expect(screen.getByRole('status').textContent).toContain('No choices match')
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '' } })
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'Complete' } })
    expect(onChange).toHaveBeenCalledWith('Complete')
  })

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

it('shows human choice labels and only the selected policy help', () => {
  const props = { label: 'Plan gate', type: 'enum', choices: ['human', 'machine_then_human'],
    choiceDetails: { human: { label: 'Human review', help: 'You approve each plan.' }, machine_then_human: { label: 'Machine then human', help: 'The machine checks, then you approve.' } }, onChange: vi.fn() }
  const { rerender } = render(<SettingField {...props} value="human" />)
  expect(screen.getByRole('option', { name: 'Machine then human' }).getAttribute('value')).toBe('machine_then_human')
  expect(screen.getByText('You approve each plan.')).toBeTruthy()
  expect(screen.queryByText('The machine checks, then you approve.')).toBeNull()
  rerender(<SettingField {...props} value="machine_then_human" />)
  expect(screen.getByText('The machine checks, then you approve.')).toBeTruthy()
  expect(screen.queryByText('You approve each plan.')).toBeNull()
})
