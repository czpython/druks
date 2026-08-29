import type { App } from '../api/types'
import { AppPage } from '../druksui/AppPage'
import { AppHomePage } from '../pages/AppHomePage'
import { SubjectPage } from '../pages/SubjectPage'
import { InstalledAppHost } from './InstalledAppHost'
import { getAppUI, registerAppUI, type AppRoute } from './registry'

// Bundled registration wins: a name already present is left alone, so this is
// safe to call on every roster response.
export function registerInstalledApps(roster: App[] | undefined): void {
  const apps = (roster ?? []).filter((info) => !info.builtin)
  apps.sort((a, b) => a.name.localeCompare(b.name))
  for (const info of apps) {
    if (!getAppUI(info.name)) registerAppUI(installedUI(info))
  }
}

// A declared page path carries ``{note_id}`` placeholders; wouter matches those
// as ``:note_id``. Only a bare ``*`` spans path segments, so that is what a
// catch-all becomes — the page reads its own parameters from the API, not from
// the matched route.
function wouterPath(path: string): string {
  return path.replace(
    /\{([a-zA-Z_][a-zA-Z0-9_]*)(:path)?\}/g,
    (_whole, name: string, catchAll: string | undefined) => (catchAll ? '*' : `:${name}`),
  )
}

function installedUI(info: App) {
  const name = info.name
  if (info.hasFrontend) {
    return {
      name,
      routes: [
        {
          // One route owns the whole namespace; the app routes inside it.
          path: `/${name}/*?`,
          render: () => <InstalledAppHost name={name} />,
        },
      ],
    }
  }
  // Declaration order would put a parameterized page ahead of a literal one,
  // so the roster's own match order decides which route wins.
  const routes: AppRoute[] = info.pages.map((entry) => ({
    path: wouterPath(entry.path),
    render: () => <AppPage app={name} page={entry.name} />,
  }))
  if (routes.length === 0) {
    routes.push({
      path: `/${name}`,
      render: () => (
        <AppHomePage app={name} description={info.description} subjectTypes={info.subjectTypes} />
      ),
    })
  }
  // Last: the subject matcher spans any path under the app, so a declared page
  // must have its chance first.
  routes.push({
    // Wildcard, not :id — a subject id can contain slashes ("owner/repo#7").
    path: `/${name}/:subjectType/*`,
    render: (params: Record<string, string>) => (
      <SubjectPage
        app={name}
        subjectType={params.subjectType ?? ''}
        subjectId={params['*'] ?? ''}
      />
    ),
  })
  return {
    name,
    routes,
    subjectPath: ({ type, id }: { type: string; id: string }) =>
      info.subjectTypes.includes(type) ? `/${name}/${type}/${id}` : undefined,
  }
}
