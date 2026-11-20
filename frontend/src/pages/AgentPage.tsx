import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'wouter'

import { api } from '../api/client'
import type { Account, AgentSchedule, RunSummary } from '../api/types'
import { Cost } from '../components/Common'
import { Button } from '../components/Control'
import { Page } from '../components/Page'
import { StatusGlyph } from '../components/StatusGlyph'
import { timeAway } from '../lib/format'
import { RUN_LABELS } from '../lib/states'
import '../agents.css'

function runCost(run: RunSummary): number {
  return run.agentCalls.reduce((sum, call) => sum + (call.costUsd ?? 0), 0)
}

// Monday 00:00 in the viewer's time.
function startOfWeek(now: Date): Date {
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  start.setDate(start.getDate() - ((start.getDay() + 6) % 7))
  return start
}

export function AgentPage({ id, account }: { id: string; account: Account }) {
  const queryClient = useQueryClient()
  const weekStart = startOfWeek(new Date()).toISOString()
  const agentQuery = useQuery({ queryKey: ['configuredAgent', id], queryFn: () => api.configuredAgent(id) })
  const accountsQuery = useQuery({ queryKey: ['accounts'], queryFn: api.accounts })
  const schedulesQuery = useQuery({
    queryKey: ['configuredAgentSchedules', id],
    queryFn: () => api.configuredAgentSchedules(id),
  })
  const runsQuery = useQuery({ queryKey: ['configuredAgentRuns', id], queryFn: () => api.configuredAgentRuns(id) })
  const weekQuery = useQuery({
    queryKey: ['configuredAgentRuns', id, weekStart],
    queryFn: () => api.configuredAgentRuns(id, { since: weekStart, limit: '100' }),
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
        await queryClient.invalidateQueries({ queryKey: ['configuredAgentSchedules', id] })
      }
    }
  }

  return (
    <Page inset className="agents-page">
      <header className="agents-head">
        <div>
          <Link href="/agents">Agents</Link>
          <h1>{agentQuery.data?.name}</h1>
          <p>{agentQuery.data?.description}</p>
        </div>
      </header>
      {agentQuery.isError && <p role="alert">{agentQuery.error.message}</p>}
      {error && <p role="alert">{error}</p>}

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
      {weekQuery.isSuccess && (
        <p>
          This week: <Cost value={weekQuery.data.reduce((sum, run) => sum + runCost(run), 0)} />
        </p>
      )}
      {runsQuery.isPending && <p role="status">Loading runs…</p>}
      {(runsQuery.isError || weekQuery.isError) && (
        <p className="mcp-error" role="alert">
          Could not load the runs.{' '}
          <Button
            onClick={() => {
              void runsQuery.refetch()
              void weekQuery.refetch()
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
                  <Link href={`/runs/${run.id}`}>{run.label}</Link>
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
