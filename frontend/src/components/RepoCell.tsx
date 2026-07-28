interface Props {
  repo?: string | null
  project?: string | null
}

/**
 * Repo column — an ``owner/name`` shown by its bare name, with the Druks Project
 * as a hover tooltip when bound. Items without a repo (e.g. scope rows
 * pre-target) render an em-dash.
 */
export function RepoCell({ repo, project }: Props) {
  if (!repo) return <span className="mono dim">—</span>
  const repoBare = repo.slice(repo.indexOf('/') + 1)
  const title = project ? `${project} · ${repoBare}` : repoBare
  return (
    <span className="row-repo mono" title={title}>
      {repoBare}
    </span>
  )
}
