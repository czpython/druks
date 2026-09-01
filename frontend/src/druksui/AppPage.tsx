import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo, useRef, type ReactNode } from 'react'
import { Link as RouteLink, useLocation } from 'wouter'

import { api } from '../api/client'
import type { Follows, PageEntry, PageSnapshot } from '../api/types'
import { EmptyState } from '../components/EmptyState'
import { Page } from '../components/Page'
import { AppSurface } from './AppSurface'
import { Blocks } from './Blocks'
import { leadingFieldAction } from './blockLayout'
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
  const installed = roster.data?.find((entry) => entry.name === app)
  const pages = installed?.pages ?? []
  const operations = installed?.operations ?? []
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

  const current = pages.find((entry) => entry.name === page)
  const root = pages.find((entry) => entry.name === current?.parent) ?? current
  // A page shows its family's tabs only when it is one of them. A
  // parameterized child is a detail page, so it gets the parent link instead.
  const family = root ? tabsFor(pages, root) : []
  const tabs = family.some((tab) => tab.name === page) ? family : []
  const parent = current && isDetail(pages, current) ? parentOf(pages, current) : undefined
  const chrome = { app, page, location, parent, root, tabs }

  if (snapshot.isLoading) {
    const waiting = `loading ${app.replaceAll('_', ' ')}`
    // The breadcrumb and the tabs come from the roster, which is already warm,
    // so the page keeps its frame while the body arrives. A cold deeplink has
    // no roster yet and waits bare.
    if (!pages.length) {
      return (
        <Page className="dui-page">
          <EmptyState glyph="…" msg={waiting} />
        </Page>
      )
    }
    return (
      <Page className="dui-page">
        {/* The title is the app's to compute, so it is held rather than
            guessed: a page label would show the wrong words first. */}
        <PageChrome {...chrome} title="…" />
        <EmptyState glyph="…" msg={waiting} />
      </Page>
    )
  }
  if (snapshot.isError || !snapshot.data) {
    const detail = snapshot.error instanceof Error ? snapshot.error.message : ''
    return appError(app, detail, () => snapshot.refetch())
  }

  const pageAction = leadingFieldAction(snapshot.data.blocks)
  const bodyBlocks = pageAction ? snapshot.data.blocks.slice(1) : snapshot.data.blocks

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
      <PagesContext.Provider value={{ app, pages, operations }}>
        <Page className="dui-page">
          <PageChrome
            {...chrome}
            title={snapshot.data.title}
            description={snapshot.data.description}
            action={pageAction && <Blocks blocks={[pageAction]} />}
          />
          <Blocks blocks={bodyBlocks} />
        </Page>
      </PagesContext.Provider>
    </AppSurface>
  )
}

// The frame a page wears whether or not its body has arrived: where it sits,
// what it is called, and the tabs beside it.
function PageChrome({
  app,
  page,
  location,
  parent,
  root,
  tabs,
  title,
  description,
  action,
}: {
  app: string
  page: string
  location: string
  parent?: PageEntry
  root?: PageEntry
  tabs: PageEntry[]
  title: ReactNode
  description?: string
  action?: ReactNode
}) {
  return (
    <>
      {parent && (
        <RouteLink href={hrefUnder(location, parent)} className="breadcrumb dui-parent">
          {/* The arrow points; it is not part of the link's name. */}
          <span aria-hidden="true">← </span>
          {parent.label}
        </RouteLink>
      )}
      <div className="dui-page-head">
        <div className="dui-page-head-copy">
          <h1 className="dui-title">{title}</h1>
          {description && <p className="dui-description dim">{description}</p>}
        </div>
        {action && <div className="dui-page-actions">{action}</div>}
      </div>
      {tabs.length > 0 && root && (
        <nav className="dui-tabs" aria-label={`${app} page tabs`}>
          {tabs.map((tab) => (
            <RouteLink
              key={tab.name}
              href={hrefUnder(location, root) + tab.path.slice(root.path.length)}
              className={`dui-tab ${tab.name === page ? 'dui-tab-active' : ''}`}
              aria-current={tab.name === page ? 'page' : undefined}
            >
              {tab.label}
            </RouteLink>
          ))}
        </nav>
      )}
    </>
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
          <button type="button" className="dui-retry" onClick={retry}>
            try again
          </button>
        }
      />
    </Page>
  )
}
