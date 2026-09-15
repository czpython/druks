import type { ReactNode } from 'react'

function stringOr(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback
}

/**
 * The final payload of a software_factory agent call: the evaluator's
 * {verdict, body, findings, checks, acceptance_results}, the plan reviewer's
 * {decision, body}, or the implementer's {status, summary}. Plans and scope
 * briefs have their own views and stay out of the transcript.
 */
export function harnessResult(result: Record<string, unknown>): ReactNode {
  const verdict = stringOr(result.verdict ?? result.decision ?? result.status, '').trim()
  const body = stringOr(result.body ?? result.summary, '').trim()
  if (!verdict && !body) return null
  const findings = Array.isArray(result.findings) ? result.findings.length : null
  const checks = Array.isArray(result.checks) ? result.checks : null
  const acceptance = Array.isArray(result.acceptance_results)
    ? result.acceptance_results.length
    : null
  // Only the evaluation carries checks. Its blocked verdict says the checks
  // could not decide, not that the work has a blocker, so the label names the
  // failed or skipped checks instead of the bare word.
  const verificationBlocked = verdict === 'blocked' && checks !== null
  const blockedChecks = verificationBlocked
    ? (checks as Record<string, unknown>[])
        .filter((check) => check.status === 'fail' || check.status === 'not_run')
        .map((check) => ({ name: stringOr(check.name, ''), evidence: stringOr(check.evidence, '') }))
    : []
  // Only the segments this payload shape has, so a plan-review row does not
  // read "0 findings · 0 checks · 0 AC".
  const counts: string[] = []
  if (findings !== null) counts.push(`${findings} finding${findings === 1 ? '' : 's'}`)
  if (checks !== null) counts.push(`${checks.length} check${checks.length === 1 ? '' : 's'}`)
  if (acceptance !== null) counts.push(`${acceptance} AC`)
  const lower = verdict.toLowerCase()
  const isError =
    lower === 'fail' ||
    lower === 'failed' ||
    lower === 'blocked' ||
    lower === 'request_changes' ||
    lower === 'file_followup'
  return (
    <div
      className={`stream-row stream-row-harness-result mono${isError ? ' stream-row-result-error' : ''}`}
    >
      <div className="stream-row-harness-head">
        <span className="stream-glyph">⊕</span>{' '}
        {verdict && (
          <span className="stream-harness-verdict mono">
            {verificationBlocked ? 'Verification blocked' : verdict}
          </span>
        )}
        {counts.length > 0 && (
          <span className="stream-harness-counts mono dim">{counts.join(' · ')}</span>
        )}
      </div>
      {blockedChecks.map((check, index) => (
        <div key={index} className="stream-row-harness-body">
          <strong>{check.name}</strong>
          {check.name && check.evidence ? ': ' : ''}
          {check.evidence}
        </div>
      ))}
      {body && <div className="stream-row-harness-body">{body}</div>}
    </div>
  )
}
