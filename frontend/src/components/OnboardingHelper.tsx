import { useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, Book, BookOpen, Circle, CircleCheck, MessageSquare, X } from 'lucide-react'
import { Link } from 'wouter'

import { api } from '../api/client'
import { Button } from './Control'
import { FoundationTooltip } from './FoundationTooltip'
import './onboarding-helper.css'

function useOnboardingProgress() {
  const subscriptions = useQuery({
    queryKey: ['providerSubscriptions'],
    queryFn: api.providerSubscriptions,
    staleTime: 60_000,
  })
  const keys = useQuery({
    queryKey: ['providerKeys'],
    queryFn: api.providerKeys,
    staleTime: 60_000,
  })
  const overview = useQuery({
    queryKey: ['dashboard', 'overview', ''],
    queryFn: () => api.dashboardOverview(),
    staleTime: 30_000,
  })

  const providerConnected = Boolean(
    subscriptions.data?.some((subscription) => subscription.connected && !subscription.revokedAt)
      || keys.data?.length,
  )
  const providerReady = providerConnected
    ? true
    : subscriptions.isPending || keys.isPending || subscriptions.isError || keys.isError
      ? null
      : false
  const overviewData = overview.data
  const hasFirstTask = Boolean(overviewData && (
    overviewData.needsYou.total || overviewData.running.total || overviewData.failed.total
      || overviewData.lastFinishedAt || overviewData.lastFailedAt
  ))
  const firstTaskStarted = hasFirstTask
    ? true
    : overview.isPending || overview.isError
      ? null
      : false

  return {
    checking: subscriptions.isPending || keys.isPending || overview.isPending,
    unavailable: providerReady === null || firstTaskStarted === null,
    steps: [
      { label: 'Connect an AI provider', complete: providerReady, href: '/settings/providers' },
      { label: 'Send your first task', complete: firstTaskStarted, href: '/chat' },
    ],
  }
}

export function OnboardingNavigationItem({ onClick, active }: { onClick: () => void; active: boolean }) {
  const { checking, unavailable, steps } = useOnboardingProgress()
  const completed = steps.filter((step) => step.complete === true).length

  return (
    <button type="button" className="sidebar-link onboarding-navigation-item" aria-current={active ? 'page' : undefined} onClick={onClick}>
      {active ? <BookOpen size={17} aria-hidden="true" /> : <Book size={17} aria-hidden="true" />}
      <span>Onboarding</span>
      <span className="onboarding-navigation-count">{checking ? '…' : unavailable ? '—' : `${completed} of ${steps.length}`}</span>
    </button>
  )
}

function MilestoneSummary({ compact = false }: { compact?: boolean }) {
  const { checking, unavailable, steps } = useOnboardingProgress()
  const completed = steps.filter((step) => step.complete === true).length
  const allComplete = !unavailable && completed === steps.length

  return (
    <div className={`onboarding-milestone-content${compact ? ' onboarding-milestone-content--compact' : ''}`}>
      <div className="onboarding-milestone-header">
        <div className="onboarding-milestone-copy">
          <div className="onboarding-milestone-title-row">
            <h2>{allComplete ? 'You’re ready to explore' : 'Get Druks ready'}</h2>
            <p className="onboarding-progress" aria-live="polite">
              {checking ? 'Checking…' : unavailable ? 'Status unavailable' : `${completed} of ${steps.length}`}
            </p>
          </div>
          <p>{allComplete ? 'Your first task is complete.' : 'Connect a provider, then give Druks a first task.'}</p>
        </div>
      </div>
      <div className="onboarding-milestone-details">
        <ul className="onboarding-checklist">
          {steps.map((step) => (
            <li key={step.label}>
              {step.complete === true
                ? <CircleCheck className="onboarding-checklist-icon is-complete" aria-hidden="true" />
                : <Circle className="onboarding-checklist-icon" aria-hidden="true" />}
              <span>{step.label}</span>
              {step.complete === null && !checking && <span className="onboarding-step-status">Couldn’t check</span>}
              {step.complete === false && (
                <Link
                  className="druks-action-link druks-action-link--primary druks-action-link--compact druks-action-link--icon-only"
                  href={step.href}
                  aria-label={`Go to ${step.label}`}
                >
                  <ArrowRight aria-hidden="true" />
                </Link>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

export function CurrentMilestoneCard() {
  return (
    <article className="connection-card onboarding-milestone-card druks-panel">
      <MilestoneSummary compact />
    </article>
  )
}

export function OnboardingHelper({ open, onOpenChange, tab, onTabChange }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  tab: 'onboarding' | 'chat'
  onTabChange: (tab: 'onboarding' | 'chat') => void
}) {
  const [draft, setDraft] = useState('')
  if (!open) return null

  function moveTab(event: KeyboardEvent<HTMLDivElement>) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const next = event.key === 'Home'
      ? 'onboarding'
      : event.key === 'End'
        ? 'chat'
        : tab === 'onboarding'
          ? 'chat'
          : 'onboarding'
    onTabChange(next)
    event.currentTarget.querySelector<HTMLButtonElement>(`[data-tab="${next}"]`)?.focus()
  }

  return (
    <aside className="onboarding-helper" aria-label="Druks onboarding and help">
      <section className="onboarding-helper-panel druks-panel" aria-label={tab === 'onboarding' ? 'Onboarding' : 'Chat with Druks'}>
          <header className="onboarding-helper-header">
            <div className="onboarding-helper-tabs" role="tablist" aria-label="Help panel" onKeyDown={moveTab}>
              <button
                type="button"
                className="sidebar-link onboarding-helper-tab"
                role="tab"
                data-tab="onboarding"
                id="onboarding-helper-tab-onboarding"
                aria-controls="onboarding-helper-panel-onboarding"
                aria-selected={tab === 'onboarding'}
                tabIndex={tab === 'onboarding' ? 0 : -1}
                onClick={() => onTabChange('onboarding')}
              >
                <BookOpen size={16} aria-hidden="true" /> Onboarding
              </button>
              <button
                type="button"
                className="sidebar-link onboarding-helper-tab"
                role="tab"
                data-tab="chat"
                id="onboarding-helper-tab-chat"
                aria-controls="onboarding-helper-panel-chat"
                aria-selected={tab === 'chat'}
                tabIndex={tab === 'chat' ? 0 : -1}
                onClick={() => onTabChange('chat')}
              >
                <MessageSquare size={16} aria-hidden="true" /> Chat
              </button>
            </div>
            <FoundationTooltip label="You can reopen this guide from Settings anytime." placement="top">
              <Button
                className="onboarding-helper-close"
                variant="quiet"
                size="compact"
                iconOnly
                aria-label="Hide onboarding and chat panel"
                onClick={() => onOpenChange(false)}
              >
                <X size={16} aria-hidden="true" />
              </Button>
            </FoundationTooltip>
          </header>

          <div className="onboarding-helper-body" id="onboarding-helper-panel-onboarding" role="tabpanel" aria-labelledby="onboarding-helper-tab-onboarding" hidden={tab !== 'onboarding'}>
            <MilestoneSummary />
          </div>
          <div className="onboarding-helper-body onboarding-helper-chat" id="onboarding-helper-panel-chat" role="tabpanel" aria-labelledby="onboarding-helper-tab-chat" hidden={tab !== 'chat'}>
            <div className="onboarding-chat-message">
              <span className="onboarding-chat-mark onboarding-dragon-mark" aria-hidden="true" />
              <div>
                <p>Hi, I’m Druks. I can help you get started or find your way around.</p>
                <div className="onboarding-chat-suggestions">
                  <Button className="onboarding-chat-suggestion" variant="quiet" size="compact" onClick={() => setDraft('What can Druks help me with?')}>What can Druks help me with?</Button>
                  <Button className="onboarding-chat-suggestion" variant="quiet" size="compact" onClick={() => setDraft('Help me connect a provider')}>Help me connect a provider</Button>
                </div>
              </div>
            </div>
            <div className="onboarding-chat-composer druks-field druks-field--compact">
              <input aria-label="Message Druks" placeholder="Ask Druks for help…" value={draft} onChange={(event) => setDraft(event.target.value)} />
              <Link href="/chat" className="onboarding-open-chat" aria-label="Open the Chat page" title="Open the Chat page"><ArrowRight size={16} aria-hidden="true" /></Link>
            </div>
          </div>
      </section>
    </aside>
  )
}
