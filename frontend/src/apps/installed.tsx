import type { App } from '../api/types'
import { AppHomePage } from '../pages/AppHomePage'
import { SubjectPage } from '../pages/SubjectPage'
import { InstalledAppHost } from './InstalledAppHost'
import { getAppUI, registerAppUI } from './registry'

// Registers the backend roster into the same UI registry the bundled apps
// use: an installed app without bundled UI gets the generic pages; one that
// ships its own dist gets mounted inside the shell at /<name>. Bundled
// registration wins — a name already present is left alone. Idempotent, called
// on every roster response.
export function registerInstalledApps(roster: App[] | undefined): void {
  const apps = (roster ?? []).filter((info) => !info.builtin)
  apps.sort((a, b) => a.name.localeCompare(b.name))
  for (const info of apps) {
    if (!getAppUI(info.name)) registerAppUI(installedUI(info))
  }
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
  return {
    name,
    routes: [
      {
        path: `/${name}`,
        render: () => (
          <AppHomePage
            app={name}
            description={info.description}
            subjectTypes={info.subjectTypes}
          />
        ),
      },
      {
        // Wildcard, not :id — a subject id can contain slashes ("owner/repo#7").
        path: `/${name}/:subjectType/*`,
        render: (params: Record<string, string>) => (
          <SubjectPage
            app={name}
            subjectType={params.subjectType ?? ''}
            subjectId={params['*'] ?? ''}
          />
        ),
      },
    ],
    subjectPath: ({ type, id }: { type: string; id: string }) =>
      info.subjectTypes.includes(type) ? `/${name}/${type}/${id}` : undefined,
  }
}
