import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'wouter'

import { api } from '../api/client'
import { Button } from './Control'
import { Markdown } from './Markdown'
import './connection-cards.css'

export function ConnectionCards({ title, description, children }: {
  title?: string
  description?: string
  children?: ReactNode
}) {
  const services = useQuery({ queryKey: ['services'], queryFn: () => api.services(), staleTime: 60_000 })
  const invitations = services.data?.filter((service) => !service.connected) ?? []

  if (!services.isPending && invitations.length === 0 && !children) {
    return services.isError && (
      <div className="connection-cards-error" role="alert">
        <p>Could not check your connections. Retry before changing your setup.</p>
        <Button size="compact" onClick={() => void services.refetch()}>Retry</Button>
      </div>
    )
  }

  return (
    <div className={title ? 'connection-onboarding' : undefined}>
      {title && (
        <header className="app-page-heading connection-onboarding-heading">
          <div className="app-page-heading-main">
            <h2>{title}</h2>
            {description && <p>{description}</p>}
          </div>
        </header>
      )}
      <section className="connection-cards" aria-label={title ? 'Get started with Druks' : 'Connect your tools'}>
        {children}
        {services.isPending && <p className="connection-cards-loading" role="status">Checking connections…</p>}
        {services.isError && (
          <div className="connection-cards-error" role="alert">
            <p>Some connections could not be checked. Retry to see the complete setup options.</p>
            <Button size="compact" onClick={() => void services.refetch()}>Retry</Button>
          </div>
        )}
        {invitations.map((service) => (
          <article className="connection-card druks-panel" key={service.slug}>
            <div className="connection-card-copy">
              <h2>{service.title}</h2>
              <Markdown className="connection-card-description" source={service.description} />
              <Link href="/settings/connections?tab=services" className="druks-button druks-button--compact">
                Connect {service.title}
              </Link>
            </div>
          </article>
        ))}
      </section>
    </div>
  )
}
