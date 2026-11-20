import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FilterChip } from './FilterChip'

afterEach(cleanup)

describe('FilterChip', () => {
  it('reports the chosen value as pressed', () => {
    const onSelect = vi.fn()
    render(<FilterChip value="open" current="open" label="Open" onSelect={onSelect} />)

    const chip = screen.getByRole('button', { name: 'Open', pressed: true })
    fireEvent.click(chip)
    expect(onSelect).toHaveBeenCalledWith('open')
  })
})
