import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Button, Select } from './Control'

afterEach(cleanup)

describe('Foundation control adapters', () => {
  it('maps Button variant, density, icon-only, and native action props to Foundation classes', () => {
    const onClick = vi.fn()
    render(
      <Button
        variant="danger"
        size="compact"
        iconOnly
        aria-label="Remove connection"
        aria-pressed="false"
        disabled
        onClick={onClick}
      >
        <svg aria-hidden="true" />
      </Button>,
    )

    const button = screen.getByRole('button', { name: 'Remove connection' })
    expect(button.className).toBe('druks-button druks-button--danger druks-button--compact druks-button--icon-only')
    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect((button as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(button)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('keeps Select native and puts its decorative indicator outside the hit target', () => {
    render(
      <label>
        Theme
        <Select aria-label="Theme" wrapperClassName="theme-select" defaultValue="system" onChange={() => undefined}>
          <option value="system">System</option>
          <option value="dark">Dark</option>
        </Select>
      </label>,
    )

    const select = screen.getByRole('combobox', { name: 'Theme' })
    expect(select.tagName).toBe('SELECT')
    expect(select.className).toBe('druks-select')
    expect(select.closest('.druks-select-wrap')).toBeTruthy()
    expect(select.closest('.druks-select-wrap')?.classList.contains('theme-select')).toBe(true)
    expect(select.parentElement?.querySelector('.druks-select-indicator')?.getAttribute('aria-hidden')).toBe('true')
    fireEvent.change(select, { target: { value: 'dark' } })
    expect((select as HTMLSelectElement).value).toBe('dark')
  })

  it('maps the quiet variant to the Foundation quiet treatment', () => {
    render(<Button variant="quiet">More</Button>)
    expect(screen.getByRole('button', { name: 'More' }).className).toBe('druks-button druks-button--quiet')
  })

  it('maps the quiet icon-toggle variant to its Foundation pressed-state contract', () => {
    render(<Button variant="quiet-icon-toggle" size="compact" iconOnly aria-label="Unpin conversation" aria-pressed="true"><svg aria-hidden="true" /></Button>)
    const button = screen.getByRole('button', { name: 'Unpin conversation' })
    expect(button.className).toBe('druks-button druks-button--quiet druks-button--quiet-icon-toggle druks-button--compact druks-button--icon-only')
    expect(button.getAttribute('aria-pressed')).toBe('true')
  })

  it('maps the combined quiet-danger treatment without a consumer-side class assembly', () => {
    render(<Button variant="quiet-danger">Remove</Button>)
    expect(screen.getByRole('button', { name: 'Remove' }).className).toBe(
      'druks-button druks-button--danger druks-button--quiet',
    )
  })

  it('deduplicates Select class tokens while preserving consumer layout hooks', () => {
    render(<Select aria-label="Provider" className="provider-select druks-select"><option>Claude</option></Select>)
    expect(screen.getByRole('combobox', { name: 'Provider' }).className).toBe('druks-select provider-select')
  })
})
