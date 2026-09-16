import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

export function Menu({
  anchor,
  children,
  className,
  onClose,
}: {
  anchor: HTMLElement | null
  children: ReactNode
  className?: string
  onClose: () => void
}) {
  const menu = useRef<HTMLDivElement>(null)
  const [position, setPosition] = useState<{
    top: number
    left: number
  } | null>(null)
  useLayoutEffect(() => {
    if (!anchor) return
    const bounds = anchor.getBoundingClientRect()
    const menuHeight = menu.current ? menu.current.offsetHeight : 240
    const below = window.innerHeight - bounds.bottom
    const top =
      below < menuHeight + 12 && bounds.top > menuHeight + 12
        ? bounds.top - menuHeight - 4
        : bounds.bottom + 4
    let left = bounds.left
    const menuWidth = menu.current ? menu.current.offsetWidth : 200
    if (left + menuWidth > window.innerWidth - 12) left = window.innerWidth - menuWidth - 12
    setPosition({ top, left: Math.max(12, left) })
  }, [anchor])
  useEffect(() => {
    const onDown = (event: MouseEvent) => {
      if (
        menu.current &&
        !menu.current.contains(event.target as Node) &&
        anchor &&
        !anchor.contains(event.target as Node)
      )
        onClose()
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !menu.current?.closest('[hidden]')) {
        event.preventDefault()
        event.stopPropagation()
        onClose()
      }
    }
    const onScroll = (event: Event) => {
      if (!menu.current?.contains(event.target as Node)) onClose()
    }
    document.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onClose)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onClose)
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [anchor, onClose])
  return (
    <div
      className={className ? `set-menu ${className}` : 'set-menu'}
      ref={menu}
      style={position ? { top: position.top, left: position.left } : { visibility: 'hidden' }}
    >
      {children}
    </div>
  )
}
