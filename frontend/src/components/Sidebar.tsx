import { useEffect, useRef, type ReactNode } from 'react'
import { Menu, X } from 'lucide-react'
import { Link, useLocation } from 'wouter'

import type { Account } from '../api/types'

export function Sidebar({
  account,
  home,
  children,
}: {
  account: Account
  home: string
  children: ReactNode
}) {
  const [location] = useLocation()
  const drawer = useRef<HTMLDialogElement>(null)
  const opener = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (drawer.current?.open) drawer.current.close('navigation')
  }, [location])

  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 650px)')
    const closeOnDesktop = () => {
      if (desktop.matches) drawer.current?.close()
    }
    desktop.addEventListener('change', closeOnDesktop)
    return () => desktop.removeEventListener('change', closeOnDesktop)
  }, [])

  const content = (
    <>
      <Link href={home} className="sidebar-brand" aria-label="Druks home">
        <span className="brand-glyph" aria-hidden="true" />
        <span>druks</span>
      </Link>
      {children}
      <div className="sidebar-account">
        {account.username}
      </div>
    </>
  )

  return (
    <>
      <button
        ref={opener}
        type="button"
        className="navigation-toggle"
        aria-label="Open navigation"
        aria-haspopup="dialog"
        onClick={() => {
          drawer.current!.returnValue = ''
          drawer.current!.showModal()
        }}
      >
        <Menu size={20} aria-hidden="true" />
      </button>
      <aside className="sidebar sidebar-desktop" aria-label="Druks navigation">
        {content}
      </aside>
      <dialog
        ref={drawer}
        className="sidebar-drawer"
        aria-label="Druks navigation"
        onCancel={(event) => event.stopPropagation()}
        onClose={() => {
          if (drawer.current?.returnValue !== 'navigation' && !opener.current?.closest('[hidden]'))
            opener.current?.focus()
        }}
        onClick={(event) => {
          if (event.target === event.currentTarget) drawer.current?.close()
        }}
      >
        <div
          className="sidebar"
          onClick={(event) => {
            if ((event.target as Element).closest('a')) drawer.current?.close('navigation')
          }}
        >
          <button
            type="button"
            className="navigation-close"
            aria-label="Close navigation"
            onClick={() => drawer.current?.close()}
          >
            <X size={20} aria-hidden="true" />
          </button>
          {content}
        </div>
      </dialog>
    </>
  )
}
