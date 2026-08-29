import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { Link as RouteLink, useLocation } from 'wouter'

import { api } from '../api/client'
import { EmptyState } from '../components/EmptyState'
import { Page } from '../components/Page'
import { AppSurface } from './AppSurface'
import { Blocks } from './Blocks'
import { hrefUnder, isDetail, PagesContext, parentOf, tabsFor } from './pages'

/** One page an app declared in Python, rendered by the shell. */
export function AppPage({ app, page }: { app: string; page: string }) {
  const [location] = useLocation()
  const roster = useQuery({ queryKey: ['apps'], queryFn: api.listApps, staleTime: 60_000 })
  const pages = roster.data?.find((entry) => entry.name === app)?.pages ?? []
  const path = location.slice(`/${app}`.length)
  const snapshot = useQuery({
    queryKey: ['page', app, path],
    queryFn: () => api.readPage(app, path),
  })

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
