import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'wouter'

import { api } from '../api/client'
import type { Account, AgentSchedule, RunSummary } from '../api/types'
import { Cost } from '../components/Common'
import { Button } from '../components/Control'
import { Page } from '../components/Page'
import { StatusGlyph } from '../components/StatusGlyph'
import { activityDay } from '../lib/feed'
import { timeAway, zonedParts } from '../lib/format'
import { useTimezone } from '../lib/preferences'
import { RUN_LABELS } from '../lib/states'
import '../agents.css'

function runCost(run: RunSummary): number {
  return run.agentCalls.reduce((sum, call) => sum + (call.costUsd ?? 0), 0)
}

// Monday 00:00 in the account's timezone.
function startOfWeek(now: Date, timezone: string): string {
  const today = zonedParts(now, timezone)
  const weekday = new Date(Date.UTC(today.year, today.month - 1, today.day)).getUTCDay()
  const monday = new Date(Date.UTC(today.year, today.month - 1, today.day - ((weekday + 6) % 7)))
  return activityDay(monday.toISOString().slice(0, 10), timezone)
}

export function AgentPage({ id, account }: { id: string; account: Account }) {
  const queryClient = useQueryClient()
  const timezone = useTimezone()
  const weekStart = startOfWeek(new Date(), timezone)
  const agentQuery = useQuery({ queryKey: ['agents', id], queryFn: () => api.agent(id) })
  const accountsQuery = useQuery({ queryKey: ['accounts'], queryFn: api.accounts })
  const schedulesQuery = useQuery({
    queryKey: ['agentSchedules', id],
    queryFn: () => api.agentSchedules(id),
  })
  const runsQuery = useQuery({ queryKey: ['agentRuns', id], queryFn: () => api.agentRuns(id) })
  const costQuery = useQuery({
    queryKey: ['agentCost', id, weekStart],
    queryFn: () => api.agentCost(id, weekStart),
  })
  const [stopping, setStopping] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const usernames = new Map((accountsQuery.data ?? []).map((entry) => [entry.id, entry.username]))
  const schedules = schedulesQuery.data ?? []
  const runs = runsQuery.data ?? []

  async function stopSchedule(schedule: AgentSchedule) {
    if (window.confirm(`Stop the schedule of ${schedule.tool}?`)) {
      setStopping(schedule.id)
      setError(null)
      try {
        await api.stopSchedule(schedule.id)
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught))
      } finally {
        setStopping(null)
        // A failed stop can mean that someone else stopped the schedule first.
        await queryClient.invalidateQueries({ queryKey: ['agentSchedules', id] })
      }
    }
  }

  if (agentQuery.isError) {
    return (
      <Page inset className="agents-page">
        <p className="mcp-error" role="alert">
          {agentQuery.error.message}
        </p>
        <Link className="ins-crumb-back" href="/agents">
          ← Agents
        </Link>
      </Page>
    )
  }

  return (
    <Page inset className="agents-page">
      <header className="agents-head">
        <div>
          <Link className="ins-crumb-back" href="/agents">
            ← Agents
          </Link>
          <h1>{agentQuery.data?.name}</h1>
          <p>{agentQuery.data?.description}</p>
        </div>
      </header>
      {error && (
        <p className="mcp-error" role="alert">
          {error}
        </p>
      )}

      <h2>Schedules</h2>
      {schedulesQuery.isPending && <p role="status">Loading schedules…</p>}
      {schedulesQuery.isError && (
        <p className="mcp-error" role="alert">
          Could not load the schedules.{' '}
          <Button onClick={() => void schedulesQuery.refetch()}>Try again</Button>
        </p>
      )}
      {schedulesQuery.isSuccess && schedules.length === 0 && <p>No one has set a schedule with this agent.</p>}
      {schedules.length > 0 && (
        <table className="connections-table" aria-label="Schedules">
          <thead>
            <tr>
              <th scope="col">What</th>
              <th scope="col">When</th>
              <th scope="col">Owner</th>
              <th scope="col">Last run</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {schedules.map((schedule) => (
              <tr key={schedule.id}>
                <th scope="row">
                  <span className="connection-name">{schedule.tool}</span>
                  <code>{JSON.stringify(schedule.arguments)}</code>
                </th>
                <td data-label="When">
                  <code>{schedule.cron}</code> · next {timeAway(schedule.nextRunAt)}
                </td>
                <td data-label="Owner">{usernames.get(schedule.accountId)}</td>
                <td data-label="Last run">
                  {schedule.lastRun
                    ? `${RUN_LABELS[schedule.lastRun.status] ?? schedule.lastRun.status} ${timeAway(schedule.lastRun.createdAt)}`
                    : 'Not run yet'}
                </td>
                <td className="connection-actions">
                  {(account.role === 'admin' || schedule.accountId === account.id) && (
                    <Button
                      variant="danger"
                      onClick={() => void stopSchedule(schedule)}
                      disabled={Boolean(stopping)}
                    >
                      Stop
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Runs</h2>
      {costQuery.isSuccess && (
        <p>
          This week: <Cost value={costQuery.data.costUsd} />
        </p>
      )}
      {runsQuery.isPending && <p role="status">Loading runs…</p>}
      {(runsQuery.isError || costQuery.isError) && (
        <p className="mcp-error" role="alert">
          Could not load the runs.{' '}
          <Button
            onClick={() => {
              void runsQuery.refetch()
              void costQuery.refetch()
            }}
          >
            Try again
          </Button>
        </p>
      )}
      {runsQuery.isSuccess && runs.length === 0 && <p>This agent has not started a run.</p>}
      {runs.length > 0 && (
        <table className="connections-table" aria-label="Runs">
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Status</th>
              <th scope="col">Asked by</th>
              <th scope="col">Started</th>
              <th scope="col">Cost</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id}>
                <th scope="row">
                  <Link className="connection-name" href={`/runs/${run.id}`}>
                    {run.label}
                  </Link>
                </th>
                <td data-label="Status">
                  <StatusGlyph state={run.state} /> {run.state}
                </td>
                <td data-label="Asked by">{run.accountUsername}</td>
                <td data-label="Started">{timeAway(run.createdAt)}</td>
                <td data-label="Cost">
                  <Cost value={runCost(run)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Page>
  )
}
