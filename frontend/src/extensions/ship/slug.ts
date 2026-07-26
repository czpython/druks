/**
 * Slug helpers for canonical, shareable detail URLs.
 *
 * URLs follow Linear's pattern: ``/<type>/<id>-<slug>``. The integer id
 * is the canonical handle the router cares about; everything after the
 * first hyphen is decorative but stable. ``useCanonicalPath`` redirects
 * any non-matching URL to the canonical form on load, so the URL bar
 * always shows ``id-slug`` and stale slugs self-heal.
 *
 * Slug generation is intentionally local to the frontend — it's purely
 * presentational, doesn't need an API round-trip, and adapters/tests
 * never see it.
 */

import type { DashboardItem, WorkItemSummary } from './api'

const SLUG_MAX_LEN = 40

/**
 * Normalize free text into a URL-safe slug. Lowercases, strips
 * combining diacritics (NFKD + drop), collapses runs of non-alphanumeric
 * to single hyphens, trims edge hyphens, and truncates at the last
 * hyphen before ``maxLen`` so we never cut a word mid-character.
 */
export function slugify(text: string, maxLen: number = SLUG_MAX_LEN): string {
  const normalized = text
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  if (normalized.length <= maxLen) return normalized
  const truncated = normalized.substring(0, maxLen)
  const lastHyphen = truncated.lastIndexOf('-')
  return lastHyphen > 0 ? truncated.substring(0, lastHyphen) : truncated
}

/**
 * Extract the leading integer from a URL slug. Returns ``NaN`` when the
 * slug doesn't start with digits — callers should treat that as a 404.
 */
export function parseLeadingId(slug: string): number {
  const match = /^(\d+)/.exec(slug)
  return match ? Number(match[1]) : Number.NaN
}

function joinLabels(...parts: Array<string | null | undefined>): string {
  return parts.filter((p): p is string => Boolean(p)).join(' ')
}

/**
 * Build the canonical ``<id>-<slug>`` segment. Falls back to bare
 * ``<id>`` when there's no usable label text (no title and no ticket
 * ref); the router parses the leading int regardless.
 */
export function itemSlug(
  id: number | string,
  ...labelParts: Array<string | null | undefined>
): string {
  const text = slugify(joinLabels(...labelParts))
  return text ? `${id}-${text}` : `${id}`
}

// ---------------------------------------------------------------------------
// Per-type path builders. Use these everywhere instead of
// hand-templating ``/work-items/${id}`` so the canonical URL stays in
// one place.
// ---------------------------------------------------------------------------

export function workItemPath(
  id: number | string,
  ticketRef?: string | null,
  title?: string | null,
): string {
  return `/work-items/${itemSlug(id, ticketRef, title)}`
}

export function workItemPathFromSummary(item: WorkItemSummary): string {
  return workItemPath(item.id, item.remoteKey, item.title)
}

export function dashboardItemPath(item: DashboardItem): string {
  // History items are all work items now (scope is a work item, not a
  // separate kind), so this always resolves to the work-item page.
  return workItemPath(item.sourceId, item.ticketRef, item.title)
}

export function agentCallPath(
  workItemId: number | string,
  workItemRemoteKey: string | null | undefined,
  workItemTitle: string | null | undefined,
  callId: string,
): string {
  // AgentCall id is uuid7 — bare uuid as the URL handle, no slug tail.
  return `${workItemPath(workItemId, workItemRemoteKey, workItemTitle)}/agent-calls/${callId}`
}
