import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FoundationTooltip } from './FoundationTooltip'

afterEach(cleanup)

describe('FoundationTooltip', () => {
  it('describes the focusable trigger and supports Escape dismissal', () => {
    render(
      <FoundationTooltip label="Screen wake lock is active." placement="top">
        <span role="img" aria-label="Screen wake lock status" tabIndex={0}>Screen</span>
      </FoundationTooltip>,
    )

    const trigger = screen.getByRole('img', { name: 'Screen wake lock status' })
    const tooltip = screen.getByRole('tooltip')
    const wrapper = trigger.parentElement!
    expect(trigger.getAttribute('aria-describedby')).toBe(tooltip.id)
    expect(wrapper.dataset.placement).toBe('top')

    fireEvent.focus(trigger)
    expect(wrapper.dataset.open).toBe('true')
    fireEvent.keyDown(trigger, { key: 'Escape' })
    expect(wrapper.dataset.open).toBe('false')
  })

  it('preserves an existing description relationship', () => {
    render(
      <FoundationTooltip label="Screen wake lock is active.">
        <span role="img" aria-label="Screen status" aria-describedby="existing-help" tabIndex={0}>Screen</span>
      </FoundationTooltip>,
    )

    const trigger = screen.getByRole('img', { name: 'Screen status' })
    const tooltip = screen.getByRole('tooltip')
    expect(trigger.getAttribute('aria-describedby')).toBe(`existing-help ${tooltip.id}`)
  })

  it('portals into the nearest open dialog so the tooltip stays in the modal layer', () => {
    render(
      <dialog open>
        <FoundationTooltip label="Keeps the screen awake." placement="inline-end">
          <span role="img" aria-label="Screen wake lock status" tabIndex={0}>Screen</span>
        </FoundationTooltip>
      </dialog>,
    )

    fireEvent.focus(screen.getByRole('img', { name: 'Screen wake lock status' }))
    const tooltip = screen.getByRole('tooltip')
    expect(tooltip.parentElement?.tagName).toBe('DIALOG')
    expect(tooltip.dataset.open).toBe('true')
  })

  it('portals outside clipping ancestors and switches to a placement that fits', () => {
    render(
      <aside style={{ overflow: 'hidden' }}>
        <FoundationTooltip label="More information" placement="top">
        <span role="img" aria-label="Status" tabIndex={0}>Status</span>
        </FoundationTooltip>
      </aside>,
    )
    const trigger = screen.getByRole('img', { name: 'Status' })
    const wrapper = trigger.parentElement!
    const tooltip = screen.getByRole('tooltip')
    const rect = (left: number, top: number, width: number, height: number) => ({
      left, top, right: left + width, bottom: top + height, width, height,
      x: left, y: top, toJSON: () => ({}),
    }) as DOMRect
    vi.spyOn(wrapper, 'getBoundingClientRect').mockReturnValue(rect(60, 4, 20, 24))
    vi.spyOn(tooltip, 'getBoundingClientRect').mockReturnValue(rect(0, 0, 100, 40))

    fireEvent.focus(trigger)
    expect(wrapper.dataset.placement).toBe('bottom')
    expect(tooltip.dataset.placement).toBe('bottom')
    expect(tooltip.parentElement).toBe(document.body)
    expect(tooltip.dataset.open).toBe('true')
    expect(tooltip.style.left).toBe('20px')
    expect(tooltip.style.top).toBe('36px')
  })

  it('keeps a sidebar tooltip fully inside the viewport at the left edge', () => {
    render(
      <aside style={{ overflow: 'hidden' }}>
        <FoundationTooltip label="Screen wake lock active." placement="inline-end">
          <span role="img" aria-label="Status" tabIndex={0}>Status</span>
        </FoundationTooltip>
      </aside>,
    )
    const trigger = screen.getByRole('img', { name: 'Status' })
    const wrapper = trigger.parentElement!
    const tooltip = screen.getByRole('tooltip')
    const rect = (left: number, top: number, width: number, height: number) => ({
      left, top, right: left + width, bottom: top + height, width, height,
      x: left, y: top, toJSON: () => ({}),
    }) as DOMRect
    vi.spyOn(wrapper, 'getBoundingClientRect').mockReturnValue(rect(8, 80, 20, 24))
    vi.spyOn(tooltip, 'getBoundingClientRect').mockReturnValue(rect(0, 0, 154, 48))

    fireEvent.focus(trigger)

    expect(tooltip.parentElement).toBe(document.body)
    expect(tooltip.dataset.placement).toBe('inline-end')
    expect(Number.parseFloat(tooltip.style.left)).toBe(36)
    expect(Number.parseFloat(tooltip.style.left) + 154).toBeLessThan(window.innerWidth)
  })

  it('opens while the pointer is over the trigger and closes after it leaves the trigger and tooltip', async () => {
    vi.useFakeTimers()
    render(
      <FoundationTooltip label="More information">
        <span role="img" aria-label="Status" tabIndex={0}>Status</span>
      </FoundationTooltip>,
    )
    const trigger = screen.getByRole('img', { name: 'Status' })
    const wrapper = trigger.parentElement!

    fireEvent.pointerEnter(wrapper, { pointerType: 'mouse' })
    expect(wrapper.dataset.open).toBe('true')
    fireEvent.pointerLeave(wrapper)
    await act(async () => { await vi.advanceTimersByTimeAsync(100) })
    expect(wrapper.dataset.open).toBe('false')
  })

  it('does not open for touch pointer entry', () => {
    render(
      <FoundationTooltip label="More information">
        <span role="img" aria-label="Status" tabIndex={0}>Status</span>
      </FoundationTooltip>,
    )
    const wrapper = screen.getByRole('img', { name: 'Status' }).parentElement!
    fireEvent.pointerEnter(wrapper, { pointerType: 'touch' })
    expect(wrapper.dataset.open).toBe('false')
  })
})
