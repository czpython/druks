import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FilterChip } from './FilterChip'

afterEach(cleanup)

describe('FilterChip', () => {
  it('uses the Foundation compact toggle Button contract', () => {
    const onSelect = vi.fn()
    render(<FilterChip value="open" current="open" label="Open" onSelect={onSelect} />)

    const chip = screen.getByRole('button', { name: 'Open', pressed: true })
    expect(chip.className).toBe('druks-button druks-button--quiet druks-button--compact')
    fireEvent.click(chip)
    expect(onSelect).toHaveBeenCalledWith('open')
  })
})
