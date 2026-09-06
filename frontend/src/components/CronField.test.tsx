import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CronField } from './CronField'

afterEach(cleanup)

describe('CronField', () => {
  it('keeps the preset select visible in custom mode, so custom is not a one-way door', () => {
    const onChange = vi.fn()
    render(<CronField label="Schedule" value="0 * * * *" onChange={onChange} disabled={false} />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'custom' } })
    expect(screen.getByPlaceholderText(/cron, e.g./)).toBeTruthy()
    expect(screen.getByRole('combobox')).toBeTruthy()

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '*/15 * * * *' } })
    expect(onChange).toHaveBeenCalledWith('*/15 * * * *')
    expect(screen.queryByPlaceholderText(/cron, e.g./)).toBeNull()
  })

  it('opens directly in custom mode for a value outside the presets', () => {
    render(<CronField label="Schedule" value="7 3 * * 1" onChange={() => {}} disabled={false} />)
    const input = screen.getByPlaceholderText(/cron, e.g./) as HTMLInputElement
    expect(input.value).toBe('7 3 * * 1')
    expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('custom')
  })

  it('shows the saved custom cadence when an unsaved preset is discarded', () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <CronField label="Schedule" value="7 3 * * 1" onChange={onChange} disabled={false} />,
    )
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '*/15 * * * *' } })
    rerender(
      <CronField label="Schedule" value="*/15 * * * *" onChange={onChange} disabled={false} />,
    )
    expect(screen.queryByRole('textbox')).toBeNull()
    rerender(<CronField label="Schedule" value="7 3 * * 1" onChange={onChange} disabled={false} />)
    expect(
      (screen.getByRole('textbox', { name: 'Schedule (cron)' }) as HTMLInputElement).value,
    ).toBe('7 3 * * 1')
    expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('custom')
  })
})
