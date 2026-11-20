import { useState } from 'react'
import { ArrowRight, Circle, CircleCheck, MessageSquare, ShieldCheck } from 'lucide-react'
import { Link } from 'wouter'

import { AppGlyph } from '../components/AppGlyph'
import { Page } from '../components/Page'
import { PageHeading } from '../components/PageHeading'
import brandLockupVertical from '../assets/brand-lockup-vertical.svg?raw'
import './onboarding-page.css'

const tasks = [
  {
    id: 'first-task',
    title: 'Start with one task',
    description: 'Choose one thing you want done. Druks will guide you through only the setup it needs.',
    icon: <MessageSquare aria-hidden="true" />,
  },
  {
    id: 'security-sweep',
    title: 'Set up a Security Sweep',
    description: 'Review security alerts and decide what Druks should do next.',
    icon: <ShieldCheck aria-hidden="true" />,
  },
  {
    id: 'software-factory',
    title: 'Set up Software Factory',
    description: 'Explore a guided workflow for taking a work item toward a pull request.',
    icon: <AppGlyph name="factory" size={24} />,
  },
]

export function OnboardingPage() {
  const [selectedTask, setSelectedTask] = useState(tasks[0]!.id)

  return (
    <Page className="page-onboarding" scroll="page">
      <div className="onboarding-page-topbar">
        <Link href="/chat/new" className="druks-button">
          Skip tutorial
        </Link>
      </div>

      <section className="onboarding-page-content" aria-label="Your Druks onboarding">
        <section className="onboarding-welcome" aria-label="Welcome to Druks">
          <article className="druks-chat-message">
            <div className="druks-chat-message__meta">
              <span className="brand-lockup onboarding-welcome-logo" role="img" aria-label="Druks" dangerouslySetInnerHTML={{ __html: brandLockupVertical }} />
            </div>
            <div className="druks-chat-message__content druks-prose">
              <p>What would you like to get done? Choose a starting point below. Druks will guide you through the setup it needs and bring decisions back to you.</p>
            </div>
          </article>
        </section>

        <section className="onboarding-first-step" aria-labelledby="onboarding-step-title">
          <header className="onboarding-step-heading">
            <p className="onboarding-step-kicker"><span className="druks-badge druks-badge--featured">Step 1</span></p>
            <PageHeading
              titleId="onboarding-step-title"
              title="What should Druks help you do first?"
              description="Start with one task, check security alerts, or work toward a pull request."
              className="onboarding-page-heading"
            />
          </header>

          <div className="onboarding-task-list" role="group" aria-label="Choose a starting task">
            {tasks.map((task) => (
              <button
                type="button"
                key={task.id}
                className="onboarding-task druks-panel"
                aria-pressed={selectedTask === task.id}
                onClick={() => setSelectedTask(task.id)}
              >
                <span className="onboarding-task-icon">{task.icon}</span>
                <span className="onboarding-task-copy">
                  <span className="onboarding-task-title">{task.title}</span>
                  <span className="onboarding-task-description">{task.description}</span>
                </span>
                <span className="onboarding-task-indicator" aria-hidden="true">
                  {selectedTask === task.id
                    ? <>Selected <CircleCheck aria-hidden="true" /></>
                    : <>Choose <Circle aria-hidden="true" /></>}
                </span>
              </button>
            ))}
          </div>

          <div className="onboarding-step-footer">
            <p>We’ll help you connect only what this task needs.</p>
            <Link href="/chat/new" className="druks-button druks-button--primary">
              Continue with this task <ArrowRight size={16} aria-hidden="true" />
            </Link>
          </div>
        </section>
      </section>
    </Page>
  )
}
