import { ArrowUpRight } from 'lucide-react'
import { useContext } from 'react'
import { Link as RouteLink } from 'wouter'

import type { Link } from '../api/types'
import { hrefForLink, PagesContext } from './pages'

function isOutbound(url: string): boolean {
  return /^https?:\/\//i.test(url)
}

/** A control that navigates. It is a block of its own, or the link on a value,
    which shows the value's own text. A relative `url` stays in this tab; only
    an absolute http(s) URL is outbound. */
export function LinkControl({
  link,
  label = link.label,
  className = 'dui-link',
}: {
  link: Link
  label?: string
  className?: string
}) {
  const { app, pages } = useContext(PagesContext)
  const href = hrefForLink(link, app, pages)
  if (link.url && isOutbound(link.url)) {
    return (
      <a
        className={className}
        href={href}
        target="_blank"
        rel="noreferrer"
        title="Opens in a new tab"
        aria-label={`${label} (opens in a new tab)`}
      >
        {label}
        <ArrowUpRight className="dui-link-external" size={12} aria-hidden="true" />
      </a>
    )
  }
  if (href) {
    return (
      <RouteLink href={href} className={className}>
        {label}
      </RouteLink>
    )
  }
  return (
    <span className={`${className} dui-link-broken`} title={`no page named ${link.page}`}>
      {label}
    </span>
  )
}
