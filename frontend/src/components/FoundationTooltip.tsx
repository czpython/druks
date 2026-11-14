import { cloneElement, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type FocusEvent, type KeyboardEvent, type PointerEvent, type ReactElement } from 'react'
import { createPortal } from 'react-dom'

interface FoundationTooltipProps {
  children: ReactElement<{ 'aria-describedby'?: string }>
  label: string
  placement?: 'top' | 'bottom' | 'inline-start' | 'inline-end'
}

export function FoundationTooltip({ children, label, placement = 'top' }: FoundationTooltipProps) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [portalTarget, setPortalTarget] = useState<HTMLElement | null>(null)
  const [position, setPosition] = useState<{ placement: NonNullable<FoundationTooltipProps['placement']>; left: number; top: number } | null>(null)
  const root = useRef<HTMLSpanElement>(null)
  const content = useRef<HTMLSpanElement>(null)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useLayoutEffect(() => {
    if (!open) return
    const target = root.current?.closest<HTMLDialogElement>('dialog[open]') ?? document.body
    setPortalTarget((current) => current === target ? current : target)
  }, [open])

  useLayoutEffect(() => {
    if (!open) return

    const candidates = [placement, ...(['top', 'bottom', 'inline-end', 'inline-start'] as const)
      .filter((candidate) => candidate !== placement)]
    const placeTooltip = () => {
      const wrapper = root.current
      const tooltip = content.current
      if (!wrapper || !tooltip) return

      const margin = 8
      const gap = 8
      const trigger = wrapper.getBoundingClientRect()
      const bounds = tooltip.getBoundingClientRect()
      const direction = getComputedStyle(wrapper).direction
      const viewportWidth = document.documentElement.clientWidth || window.innerWidth
      const viewportHeight = document.documentElement.clientHeight || window.innerHeight
      const coordinates = (candidate: NonNullable<FoundationTooltipProps['placement']>) => {
        switch (candidate) {
          case 'top': return { left: trigger.left + trigger.width / 2 - bounds.width / 2, top: trigger.top - bounds.height - gap }
          case 'bottom': return { left: trigger.left + trigger.width / 2 - bounds.width / 2, top: trigger.bottom + gap }
          case 'inline-start': return {
            left: direction === 'rtl' ? trigger.right + gap : trigger.left - bounds.width - gap,
            top: trigger.top + trigger.height / 2 - bounds.height / 2,
          }
          case 'inline-end': return {
            left: direction === 'rtl' ? trigger.left - bounds.width - gap : trigger.right + gap,
            top: trigger.top + trigger.height / 2 - bounds.height / 2,
          }
        }
      }
      const fits = (point: { left: number; top: number }) =>
        point.left >= margin && point.top >= margin &&
        point.left + bounds.width <= viewportWidth - margin &&
        point.top + bounds.height <= viewportHeight - margin

      let best = placement
      let bestVisibleArea = -1
      let bestPoint = coordinates(placement)

      for (const candidate of candidates) {
        const point = coordinates(candidate)
        const visibleWidth = Math.max(0, Math.min(point.left + bounds.width, viewportWidth - margin) - Math.max(point.left, margin))
        const visibleHeight = Math.max(0, Math.min(point.top + bounds.height, viewportHeight - margin) - Math.max(point.top, margin))
        const visibleArea = visibleWidth * visibleHeight
        if (visibleArea > bestVisibleArea) {
          best = candidate
          bestPoint = point
          bestVisibleArea = visibleArea
        }
        if (fits(point)) {
          best = candidate
          bestPoint = point
          break
        }
      }

      wrapper.dataset.placement = best
      setPosition({
        placement: best,
        left: Math.min(Math.max(margin, bestPoint.left), Math.max(margin, viewportWidth - bounds.width - margin)),
        top: Math.min(Math.max(margin, bestPoint.top), Math.max(margin, viewportHeight - bounds.height - margin)),
      })
    }

    placeTooltip()
    window.addEventListener('resize', placeTooltip)
    window.addEventListener('scroll', placeTooltip, true)
    return () => {
      window.removeEventListener('resize', placeTooltip)
      window.removeEventListener('scroll', placeTooltip, true)
    }
  }, [open, placement, portalTarget])

  useEffect(() => () => {
    if (closeTimer.current) clearTimeout(closeTimer.current)
  }, [])

  function deferClose() {
    if (closeTimer.current) clearTimeout(closeTimer.current)
    closeTimer.current = setTimeout(() => {
      if (!root.current?.contains(document.activeElement)) setOpen(false)
    }, 100)
  }

  function cancelDeferredClose() {
    if (closeTimer.current) clearTimeout(closeTimer.current)
    closeTimer.current = null
  }

  function closeOnBlur(event: FocusEvent<HTMLSpanElement>) {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      cancelDeferredClose()
      setOpen(false)
    }
  }

  function closeOnEscape(event: KeyboardEvent<HTMLSpanElement>) {
    if (event.key === 'Escape') {
      cancelDeferredClose()
      setOpen(false)
    }
  }

  function openOnPointerEnter(event: PointerEvent<HTMLSpanElement>) {
    if (event.pointerType === 'mouse' || event.pointerType === 'pen') setOpen(true)
  }

  const describedBy = [children.props['aria-describedby'], id].filter(Boolean).join(' ')

  return (
    <span
      ref={root}
      className="druks-tooltip"
      data-placement={placement}
      data-open={open}
      onPointerEnter={openOnPointerEnter}
      onPointerLeave={deferClose}
      onFocus={() => setOpen(true)}
      onBlur={closeOnBlur}
      onKeyDown={closeOnEscape}
    >
      {cloneElement(children, { 'aria-describedby': describedBy })}
      {createPortal(
        <span
          ref={content}
          className="druks-tooltip__content"
          id={id}
          role="tooltip"
          data-portal="true"
          data-open={open}
          data-placement={position?.placement ?? placement}
          style={position ? ({ left: `${position.left}px`, top: `${position.top}px` } as CSSProperties) : { left: '-10000px', top: '-10000px' }}
          onPointerEnter={cancelDeferredClose}
          onPointerLeave={deferClose}
        >
          {label}
        </span>,
        portalTarget ?? document.body,
      )}
    </span>
  )
}
