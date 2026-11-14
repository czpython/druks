import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'wouter'

import { api } from '../api/client'
import { Button } from './Control'
import './connection-cards.css'

const invitations = [
  {
    id: 'github',
    title: 'GitHub',
    icon: '/provider-icons/github.svg',
    description: 'Bring Druks into your issues and pull requests.',
    href: '/settings/connections?tab=accounts',
  },
  {
    id: 'slack',
    title: 'Slack',
    icon: '/provider-icons/slack.svg',
    description: 'Talk with Druks from your Slack workspace.',
    href: '/settings/connections?tab=accounts',
  },
  {
    id: 'whatsapp',
    title: 'WhatsApp',
    icon: '/provider-icons/whatsapp.svg',
    description: 'Send Druks a task from your phone.',
    href: '/apps/chat/settings',
  },
] as const

export function ConnectionCards({ title, description, emptyMessage, children }: {
  title?: string
  description?: string
  emptyMessage?: string
  children?: ReactNode
}) {
  const connections = useQuery({
    queryKey: ['connections'],
    queryFn: api.listConnections,
    staleTime: 60_000,
  })
  const whatsappSessions = useQuery({
    queryKey: ['wahaSessions', 'chat'],
    queryFn: () => api.wahaSessions('chat'),
    staleTime: 60_000,
  })
  const checksPending = connections.isPending || whatsappSessions.isPending
  const checksFailed = connections.isError || whatsappSessions.isError
  const visibleInvitations = invitations.filter((invitation) => {
    if (invitation.id === 'github' || invitation.id === 'slack') {
      if (!connections.isSuccess) return false
      return !connections.data?.some(
        (connection) => connection.provider === invitation.id && !connection.revokedAt,
      )
    }
    if (!whatsappSessions.isSuccess) return false
    return !whatsappSessions.data?.some(
      (session) => !session.revokedAt && session.identityStatus === 'resolved',
    )
  })

  if (checksPending) return null
  if (visibleInvitations.length === 0 && !children) {
    if (checksFailed) {
      return (
        <div className="connection-cards-error" role="alert">
          <p>Could not check your connections. Retry before changing your setup.</p>
          <Button size="compact" onClick={() => {
            void connections.refetch()
            void whatsappSessions.refetch()
          }}>Retry</Button>
        </div>
      )
    }
    return emptyMessage ? <p className="connection-cards-empty">{emptyMessage}</p> : null
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
        {checksFailed && (
          <div className="connection-cards-error" role="alert">
            <p>Some connections could not be checked. Retry to see the complete setup options.</p>
            <Button size="compact" onClick={() => {
              void connections.refetch()
              void whatsappSessions.refetch()
            }}>Retry</Button>
          </div>
        )}
        {visibleInvitations.map((invitation) => (
          <article className="connection-card druks-panel" key={invitation.id}>
            <div className="connection-card-copy">
              <h2>{invitation.title}</h2>
              <p>{invitation.description}</p>
              <Link href={invitation.href} className="druks-action-link druks-action-link--secondary druks-action-link--compact">
                <img className={`connection-provider-icon connection-provider-icon--${invitation.id}`} src={invitation.icon} alt="" aria-hidden="true" />
                Connect {invitation.title}
              </Link>
            </div>
          </article>
        ))}
      </section>
    </div>
  )
}
