import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Tooltip } from './Tooltip'

afterEach(cleanup)

describe('Tooltip', () => {
  it('describes the focusable trigger and closes on Escape', () => {
    render(
      <Tooltip label="Screen wake lock is active." placement="top">
        <span role="img" aria-label="Screen wake lock status" tabIndex={0}>Screen</span>
      </Tooltip>,
    )

    const trigger = screen.getByRole('img', { name: 'Screen wake lock status' })
    const wrapper = trigger.parentElement!
    expect(trigger.getAttribute('aria-describedby')).toBe(screen.getByRole('tooltip').id)

    fireEvent.focus(trigger)
    expect(wrapper.dataset.open).toBe('true')
    fireEvent.keyDown(trigger, { key: 'Escape' })
    expect(wrapper.dataset.open).toBe('false')
  })

  it('moves to a placement that fits the viewport', () => {
    render(
      <Tooltip label="More information" placement="top">
        <span role="img" aria-label="Status" tabIndex={0}>Status</span>
      </Tooltip>,
    )
    const trigger = screen.getByRole('img', { name: 'Status' })
    const tooltip = screen.getByRole('tooltip')
    const rect = (left: number, top: number, width: number, height: number) => ({
      left, top, right: left + width, bottom: top + height, width, height,
      x: left, y: top, toJSON: () => ({}),
    }) as DOMRect
    vi.spyOn(trigger.parentElement!, 'getBoundingClientRect').mockReturnValue(rect(60, 4, 20, 24))
    vi.spyOn(tooltip, 'getBoundingClientRect').mockReturnValue(rect(0, 0, 100, 40))

    fireEvent.focus(trigger)
    expect(tooltip.dataset.placement).toBe('bottom')
    expect(tooltip.style.top).toBe('36px')
  })
})
