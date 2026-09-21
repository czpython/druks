import { Button, EmptyState, Page, PageHeader, TextInput } from '@druks/ui'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, MoreVertical, Pencil, Plus, RotateCw, Trash2, X } from 'lucide-react'
import { useEffect, useId, useRef, useState, type ReactNode } from 'react'

import { projectsApi } from './api'
import { repoProfiling, useRepoRuns } from './profiling'
import type { Project, ProjectRepo } from './types'
import './projects.css'

export function ProjectsPage() {
  const runs = useRepoRuns()
  const anyProfiling = [...runs.values()].some((status) => status.state === 'running')
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['projects'],
    queryFn: projectsApi.list,
    // Completed profiler runs change the stored profile as well as the repo board.
    refetchInterval: anyProfiling ? 3_000 : 30_000,
  })
  const [creating, setCreating] = useState(false)

  if (isLoading) {
    return <Page className="page-projects"><EmptyState glyph="…" msg="Loading projects" /></Page>
  }
  if (!data) {
    return (
      <Page className="page-projects">
        <EmptyState
          glyph="!"
          msg="Could not load projects"
          action={<Button onClick={() => void refetch()}>Retry</Button>}
        />
      </Page>
    )
  }

  const head = (
    <PageHeader
      eyebrow="projects"
      count={data.projects.length}
      right={
        <Button variant="primary" onClick={() => setCreating(true)}>
          <Plus size={14} aria-hidden="true" /> New project
        </Button>
      }
    />
  )

  return (
    <Page className="page-projects" header={head}>
      {isError && <div className="pj-refresh-error">
        <p role="alert">Could not refresh projects. Druks shows the last saved data.</p>
        <Button onClick={() => void refetch()}>Retry</Button>
      </div>}
      {data.projects.length ? (
        <div className="pj-list">
          {data.projects.map((project) => <ProjectSection key={project.id} project={project} />)}
        </div>
      ) : (
        <EmptyState
          glyph="⊞"
          msg="No projects yet"
          sub="A project groups the GitHub repositories a build operates on. Each work item targets one of those repos for its changes, while the rest give agents cross-repo context. Create a project to start."
        />
      )}
      {creating && <ProjectNameDialog onClose={() => setCreating(false)} />}
    </Page>
  )
}

function ProjectDialog({
  title,
  pending,
  onClose,
  children,
}: {
  title: string
  pending: boolean
  onClose: () => void
  children: ReactNode
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const [opener] = useState(() => document.activeElement)

  useEffect(() => {
    const element = dialog.current!
    element.showModal()
    return () => {
      element.close()
      if (opener instanceof HTMLElement) opener.focus()
    }
  }, [opener])

  return (
    <dialog
      ref={dialog}
      className="pj-dialog"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault()
        if (!pending) onClose()
      }}
    >
      <header className="pj-dialog-head">
        <h2 id={titleId}>{title}</h2>
        <button type="button" className="pj-icon-button" aria-label="Close" disabled={pending} onClick={onClose}>
          <X size={16} aria-hidden="true" />
        </button>
      </header>
      {children}
    </dialog>
  )
}

function ProjectNameDialog({ project, onClose }: { project?: Project; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState(project?.name ?? '')
  const save = useMutation({
    mutationFn: (next: string) => project
      ? projectsApi.update(project.id, { name: next })
      : projectsApi.create({ name: next }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['projects'] })
      onClose()
    },
  })

  return (
    <ProjectDialog title={project ? 'Rename project' : 'New project'} pending={save.isPending} onClose={onClose}>
      <form onSubmit={(event) => {
        event.preventDefault()
        if (name.trim() && !save.isPending) save.mutate(name.trim())
      }}>
        <div className="pj-dialog-body">
          <label className="pj-field">
            Project name
            <TextInput autoFocus value={name} disabled={save.isPending} onChange={(event) => setName(event.target.value)} />
          </label>
          {!project && <p>A project groups the repos a build operates on. Add repos after you create it.</p>}
          {save.error && <p className="pj-error" role="alert">{String(save.error)}</p>}
        </div>
        <footer className="pj-dialog-foot">
          <Button disabled={save.isPending} onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={!name.trim() || save.isPending}>
            {save.isPending ? 'Saving…' : project ? 'Save name' : 'Create project'}
          </Button>
        </footer>
      </form>
    </ProjectDialog>
  )
}

function ProjectSection({ project }: { project: Project }) {
  const queryClient = useQueryClient()
  const actions = useRef<HTMLDetailsElement>(null)
  const [collapsed, setCollapsed] = useState(false)
  const [dialog, setDialog] = useState<'rename' | 'add' | null>(null)
  const reposId = useId()
  const headingId = useId()
  const remove = useMutation({
    mutationFn: () => projectsApi.delete(project.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects'] }),
  })
  const focusActions = () => actions.current!.querySelector('summary')!.focus()
  const closeActions = () => {
    actions.current!.open = false
    focusActions()
  }

  return (
    <section className="pj-project" aria-labelledby={headingId}>
      <header className="pj-project-head">
        <h2 id={headingId}>
          <button
            type="button"
            className="pj-disclosure"
            aria-expanded={!collapsed}
            aria-controls={reposId}
            onClick={() => setCollapsed(!collapsed)}
          >
            <ChevronDown size={16} aria-hidden="true" />
            <span>{project.name}</span>
          </button>
        </h2>
        <details
          ref={actions}
          className="pj-actions"
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') closeActions()
          }}
        >
          <summary className="pj-icon-button" aria-label={`Project actions for ${project.name}`}>
            <MoreVertical size={16} aria-hidden="true" />
          </summary>
          <div className="pj-action-list">
            <button type="button" onClick={() => { closeActions(); setDialog('add') }}>
              <Plus size={15} aria-hidden="true" /> Add repo
            </button>
            <button type="button" onClick={() => { closeActions(); setDialog('rename') }}>
              <Pencil size={15} aria-hidden="true" /> Rename project
            </button>
            <button type="button" className="pj-danger" disabled={remove.isPending} onClick={() => {
              closeActions()
              if (confirm(`Delete project "${project.name}"? This permanently deletes the project and every work item it owns.`)) {
                remove.mutate()
              }
            }}>
              <Trash2 size={15} aria-hidden="true" /> {remove.isPending ? 'Deleting…' : 'Delete project'}
            </button>
          </div>
        </details>
      </header>
      {remove.error && <p className="pj-error" role="alert">{String(remove.error)}</p>}
      <div id={reposId} hidden={collapsed}>
        {project.repos.length ? (
          <div className="pj-repos">
            {project.repos.map((repo) => <RepoRow key={repo.id} projectId={project.id} repo={repo} />)}
          </div>
        ) : (
          <div className="pj-empty-project">
            <p>No repositories yet. Add a repo to give builds their code and context.</p>
            {/* A dialog returns focus to whatever opened it. Point it at the actions
                menu, which outlives the empty state this button sits in. */}
            <Button onClick={() => { focusActions(); setDialog('add') }}>
              <Plus size={14} aria-hidden="true" /> Add repo
            </Button>
          </div>
        )}
      </div>
      {dialog === 'rename' && <ProjectNameDialog project={project} onClose={() => setDialog(null)} />}
      {dialog === 'add' && <AddRepoDialog project={project} onClose={() => setDialog(null)} />}
    </section>
  )
}

function RepoRow({ projectId, repo }: { projectId: number; repo: ProjectRepo }) {
  const queryClient = useQueryClient()
  const runs = useRepoRuns()
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [purpose, setPurpose] = useState(repo.purpose ?? '')
  const [showFullSummary, setShowFullSummary] = useState(false)
  const [showAllCommands, setShowAllCommands] = useState(false)
  const detailsId = useId()
  const headingId = useId()
  const commandsId = useId()
  const summaryId = useId()
  const profiling = repoProfiling(repo.profile, runs.get(repo.id))
  const profile = useMutation({
    mutationFn: () => projectsApi.profileRepo(projectId, repo.id),
    onSuccess: () => Promise.all([
      queryClient.invalidateQueries({ queryKey: ['projects'] }),
      queryClient.invalidateQueries({ queryKey: ['project-repo-board'] }),
    ]),
  })
  const remove = useMutation({
    mutationFn: () => projectsApi.deleteRepo(projectId, repo.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['projects'] }),
  })
  const update = useMutation({
    mutationFn: () => projectsApi.updateRepo(projectId, repo.id, { purpose: purpose.trim() || null }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['projects'] })
      setEditing(false)
    },
  })

  // The repo board has not seen the run yet in the moment after the request.
  const profileState = profile.isPending ? 'running' : profiling.state
  const isProfiling = profileState === 'running'
  const status = {
    unprofiled: 'Not profiled',
    running: 'Profiling…',
    ready: 'Profiled',
    failed: 'Profile failed',
  }[profileState]
  const separator = repo.fullName.indexOf('/') + 1
  const stack = [...new Set([
    ...(repo.profile.languages ?? []),
    ...(repo.profile.frameworks ?? []),
    ...(repo.profile.package_managers ?? []),
  ])]
  const commands = [...new Set([
    ...(repo.profile.verification?.test_commands ?? []),
    ...(repo.profile.verification?.lint_commands ?? []),
    ...(repo.profile.verification?.typecheck_commands ?? []),
  ].map((entry) => entry.command))]
  const skills = repo.profile.recommended_skills ?? []
  const summary = repo.profile.stack_summary ?? ''
  const isLongSummary = summary.length > 280

  return (
    <div className="pj-repo">
      <button
        type="button"
        className="pj-repo-row"
        aria-expanded={open}
        aria-controls={detailsId}
        onClick={() => setOpen(!open)}
      >
        <span id={headingId} className="pj-repo-name">
          <span className="pj-repo-org">{repo.fullName.slice(0, separator)}</span>
          {repo.fullName.slice(separator)}
        </span>
        <span className={`pj-repo-purpose ${repo.purpose ? '' : 'pj-muted'}`}>
          {repo.purpose || 'No purpose set'}
        </span>
        <span className="pj-profile-status" data-state={profileState}>{status}</span>
      </button>
      <div id={detailsId} role="region" aria-labelledby={headingId} hidden={!open}>
        <div className="pj-repo-details">
          <div className="pj-repo-rail">
            <dl className="pj-profile-facts">
              <div><dt>Stack</dt><dd>{stack.length} entries</dd></div>
              <div><dt>Skills</dt><dd>{skills.length} recommended</dd></div>
            </dl>
            <div className="pj-repo-actions">
              <Button disabled={isProfiling || remove.isPending} onClick={() => profile.mutate()}>
                <RotateCw size={13} aria-hidden="true" />
                {isProfiling ? 'Profiling…' : profileState === 'ready' ? 'Re-profile' : 'Profile repo'}
              </Button>
              <Button disabled={remove.isPending || update.isPending} onClick={() => {
                setPurpose(repo.purpose ?? '')
                update.reset()
                setEditing(true)
              }}><Pencil size={13} aria-hidden="true" /> Edit purpose</Button>
              <Button disabled={remove.isPending || isProfiling || update.isPending} onClick={() => {
                if (confirm(`Remove ${repo.fullName} from this project?`)) remove.mutate()
              }}>
                <X size={13} aria-hidden="true" /> {remove.isPending ? 'Removing…' : 'Remove'}
              </Button>
            </div>
          </div>
          <div className="pj-profile-content">
            {editing && (
              <form className="pj-purpose-form" onSubmit={(event) => {
                event.preventDefault()
                if (!update.isPending) update.mutate()
              }}>
                <label className="pj-field">
                  Purpose
                  <TextInput autoFocus value={purpose} disabled={update.isPending} onChange={(event) => setPurpose(event.target.value)} />
                </label>
                <div className="pj-form-actions">
                  <Button type="submit" variant="primary" disabled={update.isPending}>{update.isPending ? 'Saving…' : 'Save purpose'}</Button>
                  <Button disabled={update.isPending} onClick={() => setEditing(false)}>Cancel</Button>
                </div>
                {update.error && <p className="pj-error" role="alert">{String(update.error)}</p>}
              </form>
            )}
            {profileState === 'failed' && <p className="pj-error" role="alert">{profiling.failure || 'The profiler run failed. Try Profile repo again.'}</p>}
            {profileState === 'unprofiled' && <p className="pj-muted">Profile this repo to find its stack, verification commands, and recommended skills.</p>}
            {isProfiling && <p className="pj-muted" role="status">The profiler examines this repo. The profile updates when the run completes.</p>}
            {summary && (
              <div>
                <p id={summaryId} className="pj-profile-summary">
                  {isLongSummary && !showFullSummary ? `${summary.slice(0, 280).replace(/\s+\S*$/, '')}…` : summary}
                </p>
                {isLongSummary && <button type="button" className="pj-text-button" aria-controls={summaryId} aria-expanded={showFullSummary} onClick={() => setShowFullSummary(!showFullSummary)}>
                  {showFullSummary ? 'Less' : 'More'} <ChevronDown size={12} aria-hidden="true" />
                </button>}
              </div>
            )}
            {stack.length > 0 && <div className="pj-profile-line"><h3>Stack</h3><p className="mono">{stack.join(' · ')}</p></div>}
            {commands.length > 0 && (
              <div className="pj-profile-line">
                <h3>Verify</h3>
                <div>
                  <ul id={commandsId} className="pj-profile-commands">
                    {(showAllCommands ? commands : commands.slice(0, 3)).map((command) => <li key={command}><code>{command}</code></li>)}
                  </ul>
                  {commands.length > 3 && <button type="button" className="pj-text-button" aria-controls={commandsId} aria-expanded={showAllCommands} onClick={() => setShowAllCommands(!showAllCommands)}>
                    {showAllCommands ? 'Fewer commands' : `+${commands.length - 3} more`} <ChevronDown size={12} aria-hidden="true" />
                  </button>}
                </div>
              </div>
            )}
            {skills.length > 0 && <div className="pj-profile-line"><h3>Skills</h3><p className="mono">{skills.join(' · ')}</p></div>}
          </div>
        </div>
      </div>
      {profile.error && <p className="pj-error" role="alert">{String(profile.error)}</p>}
      {remove.error && <p className="pj-error" role="alert">{String(remove.error)}</p>}
    </div>
  )
}

function AddRepoDialog({ project, onClose }: { project: Project; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [pick, setPick] = useState('')
  const [filter, setFilter] = useState('')
  const [purpose, setPurpose] = useState('')
  const githubRepos = useQuery({
    queryKey: ['github-repos'],
    queryFn: () => projectsApi.listGithubRepos(),
    staleTime: 60_000,
  })
  const add = useMutation({
    mutationFn: () => projectsApi.addRepo(project.id, { fullName: pick, purpose: purpose.trim() || null }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['projects'] })
      onClose()
    },
  })
  const taken = new Set(project.repos.map((repo) => repo.fullName.toLowerCase()))
  const available = (githubRepos.data?.repos ?? []).filter((repo) => !taken.has(repo.fullName.toLowerCase()))
  const search = filter.trim().toLowerCase()
  const matches = available.filter((repo) => repo.fullName.toLowerCase().includes(search))
  // A repo the filter hides cannot be added, because its radio is gone.
  const canAdd = matches.some((repo) => repo.fullName === pick) && !add.isPending

  return (
    <ProjectDialog title={`Add repo to ${project.name}`} pending={add.isPending} onClose={onClose}>
      <form onSubmit={(event) => {
        event.preventDefault()
        if (canAdd) add.mutate()
      }}>
        <div className="pj-dialog-body">
          {githubRepos.isLoading ? <p role="status">Loading repositories…</p> : githubRepos.isError ? (
            <div>
              <p className="pj-error" role="alert">Could not load repositories. {String(githubRepos.error)}</p>
              <Button onClick={() => void githubRepos.refetch()}>Retry</Button>
            </div>
          ) : available.length ? (
            <>
              <label className="pj-field">
                Filter repositories
                <TextInput autoFocus value={filter} disabled={add.isPending} onChange={(event) => setFilter(event.target.value)} placeholder="Owner or name" />
              </label>
              {matches.length ? (
                <fieldset className="pj-repo-choices" disabled={add.isPending}>
                  <legend>Repository</legend>
                  {matches.map((repo) => (
                    <label key={repo.fullName} className="pj-repo-choice">
                      <input type="radio" name="repository" value={repo.fullName} checked={pick === repo.fullName} onChange={() => setPick(repo.fullName)} />
                      <span className="mono">{repo.fullName}</span>
                    </label>
                  ))}
                </fieldset>
              ) : <p>No repository matches this filter.</p>}
            </>
          ) : <p>No more repositories to add. Check repository access in the GitHub connection if a repo is missing.</p>}
          {available.length > 0 && <label className="pj-field">
            Purpose (optional)
            <TextInput value={purpose} disabled={add.isPending} onChange={(event) => setPurpose(event.target.value)} placeholder="Context this repo gives the agent" />
          </label>}
          {add.error && <p className="pj-error" role="alert">{String(add.error)}</p>}
        </div>
        <footer className="pj-dialog-foot">
          <Button disabled={add.isPending} onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={!canAdd}>{add.isPending ? 'Adding…' : 'Add repo'}</Button>
        </footer>
      </form>
    </ProjectDialog>
  )
}
