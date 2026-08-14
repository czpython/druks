import type { Extension } from '../api/types'
import { ExtensionHomePage } from '../pages/ExtensionHomePage'
import { SubjectPage } from '../pages/SubjectPage'
import { getExtensionUI, registerExtensionUI } from './registry'

// Registers the backend roster into the same UI registry the bundled extensions
// use: an installed extension without bundled UI gets the generic pages; one that
// ships its own dist gets an external home at /app/<name>. Bundled registration
// wins — a name already present is left alone. Idempotent, called on every roster
// response.
export function registerInstalledExtensions(roster: Extension[] | undefined): void {
  const apps = (roster ?? []).filter((info) => !info.builtin)
  apps.sort((a, b) => a.name.localeCompare(b.name))
  for (const info of apps) {
    if (!getExtensionUI(info.name)) registerExtensionUI(installedUI(info))
  }
}

function installedUI(info: Extension) {
  const name = info.name
  if (info.hasFrontend) {
    return { name, home: `/app/${name}`, external: true, routes: [] }
  }
  return {
    name,
    routes: [
      {
        path: `/${name}`,
        render: () => (
          <ExtensionHomePage
            extension={name}
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
            extension={name}
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
