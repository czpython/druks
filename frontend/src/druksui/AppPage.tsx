import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo, useRef, type ReactNode } from 'react'
import { Link as RouteLink, useLocation } from 'wouter'

import { api } from '../api/client'
import type { Follows, PageSnapshot } from '../api/types'
import { EmptyState } from '../components/EmptyState'
import { Page } from '../components/Page'
import { AppSurface } from './AppSurface'
import { Blocks } from './Blocks'
import { followedSubjects, hrefUnder, isDetail, mergeRegions, PagesContext, parentOf, tabsFor } from './pages'
import { SubjectStream } from './SubjectStream'

// The wait before each attempt at one refresh. Three tries, then the page
// keeps what it had until the subject changes again.
const REFRESH_WAITS = [0, 300, 1200]

/** One page an app declared in Python, rendered by the shell. */
export function AppPage({ app, page }: { app: string; page: string }) {
  const [location] = useLocation()
  const queryClient = useQueryClient()
  const roster = useQuery({ queryKey: ['apps'], queryFn: api.listApps, staleTime: 60_000 })
  const pages = roster.data?.find((entry) => entry.name === app)?.pages ?? []
  const path = location.slice(`/${app}`.length)
  const key = useMemo(() => ['page', app, path], [app, path])
  const snapshot = useQuery({
    queryKey: key,
    queryFn: () => api.readPage(app, path),
    // The stream is what keeps this page fresh. Without this, a background
    // refetch would write the cache outside the numbered reads below and could
    // land after a newer snapshot.
    staleTime: Infinity,
  })

  // Every read gets a number, per subject: a read that lands after a newer read
  // of the same subject is stale, while another subject's read is not.
  const latest = useRef(new Map<string, number>())
  const reread = useCallback(
    async (subject: Follows) => {
      const watched = `${subject.subjectType}/${subject.subjectId}`
      const mine = (latest.current.get(watched) ?? 0) + 1
      latest.current.set(watched, mine)
      // The stream repeats nothing, so a read that fails would leave the page
      // stale until the subject changes again. Back off and try again, still
      // numbered, so a newer read still wins.
      for (const wait of REFRESH_WAITS) {
        if (wait) await new Promise((resume) => setTimeout(resume, wait))
        const fresh = await api.readPage(app, path).catch(() => undefined)
        if (mine !== latest.current.get(watched)) return
        if (fresh) {
          queryClient.setQueryData(key, (previous?: PageSnapshot) =>
            previous ? mergeRegions(previous, fresh, subject) : fresh,
          )
          // A snapshot can open, change, or close a gate on a run this page
          // already shows, so the gates read themselves again.
          void queryClient.invalidateQueries({ queryKey: ['gate'] })
          return
        }
      }
    },
    [app, path, key, queryClient],
  )

  const followed = useMemo(
    () => (snapshot.data ? followedSubjects(snapshot.data) : []),
    [snapshot.data],
  )

  if (snapshot.isLoading) {
    return (
      <Page className="dui-page">
        <EmptyState glyph="…" msg="loading" />
      </Page>
    )
  }
  if (snapshot.isError || !snapshot.data) {
    const detail = snapshot.error instanceof Error ? snapshot.error.message : ''
    return appError(app, detail, () => snapshot.refetch())
  }

  const current = pages.find((entry) => entry.name === page)
  const root = pages.find((entry) => entry.name === current?.parent) ?? current
  // A page shows its family's tabs only when it is one of them. A
  // parameterized child is a detail page, so it gets the parent link instead.
  const family = root ? tabsFor(pages, root) : []
  const tabs = family.some((tab) => tab.name === page) ? family : []
  const parent = current && isDetail(pages, current) ? parentOf(pages, current) : undefined

  return (
    <AppSurface
      fallback={(clear) =>
        appError(app, 'the page snapshot was not renderable', () => {
          clear()
          snapshot.refetch()
        })
      }
    >
      {followed.map((subject) => (
        <SubjectStream
          key={`${subject.subjectType}/${subject.subjectId}`}
          app={app}
          subject={subject}
          onSnapshot={reread}
        />
      ))}
      <PagesContext.Provider value={{ app, pages }}>
        <Page className="dui-page">
          {parent && (
            <RouteLink href={hrefUnder(location, parent)} className="breadcrumb dui-parent">
              ← {parent.label}
            </RouteLink>
          )}
          <h1 className="dui-title">{snapshot.data.title}</h1>
          {snapshot.data.description && (
            <p className="dui-description dim">{snapshot.data.description}</p>
          )}
          {tabs.length > 0 && root && (
            <nav className="dui-tabs" aria-label={`${app} page tabs`}>
              {tabs.map((tab) => (
                <RouteLink
                  key={tab.name}
                  href={hrefUnder(location, root) + tab.path.slice(root.path.length)}
                  className={`dui-tab mono ${tab.name === page ? 'dui-tab-active' : ''}`}
                  aria-current={tab.name === page ? 'page' : undefined}
                >
                  {tab.label}
                </RouteLink>
              ))}
            </nav>
          )}
          <Blocks blocks={snapshot.data.blocks} />
        </Page>
      </PagesContext.Provider>
    </AppSurface>
  )
}

function appError(app: string, detail: string, retry: () => void): ReactNode {
  return (
    <Page className="dui-page">
      <EmptyState
        glyph="!"
        msg={`${app} could not render this page`}
        sub={detail || undefined}
        action={
          <button type="button" className="dui-retry mono" onClick={retry}>
            try again
          </button>
        }
      />
    </Page>
  )
}
