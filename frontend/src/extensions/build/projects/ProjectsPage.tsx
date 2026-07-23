import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { EmptyState } from '../../../components/EmptyState'
import { Page } from '../../../components/Page'
import { projectsApi } from './api'
import type { Project, ProjectRepo } from './types'

function splitRepo(full: string): { org: string; short: string } {
  const i = full.indexOf('/')
  if (i < 0) return { org: '', short: full }
  return { org: full.slice(0, i + 1), short: full.slice(i + 1) }
}

export function ProjectsPage() {
  const queryClient = useQueryClient()
  const { data, isLoading, isError } = useQuery({
    queryKey: ['projects'],
    queryFn: projectsApi.list,
    // Poll fast while a profiler run is in flight so its chip settles without a reload.
    refetchInterval: (query) => {
      const projects = query.state.data?.projects ?? []
      const profiling = projects.some((p) =>
        p.repos.some((r) => r.profileStatus === 'running'),
      )
      return profiling ? 3_000 : 30_000
    },
  })

  const [draft, setDraft] = useState('')
  const createMutation = useMutation({
    mutationFn: projectsApi.create,
    onSuccess: () => {
      setDraft('')
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
  })
  const onCreate = () => {
    const name = draft.trim()
    if (name) createMutation.mutate({ name })
  }

  if (isLoading) {
    return (
      <Page className="page-projects">
        <EmptyState glyph="…" msg="loading projects" />
      </Page>
    )
  }
  if (isError || !data) {
    return (
      <Page className="page-projects">
        <EmptyState glyph="!" msg="could not load projects" />
      </Page>
    )
  }

  return (
    <Page className="page-projects">
      <div className="pj-col">
        <div className="pj-head">
          <span className="pj-head-title">Projects</span>
          <span className="pj-head-count mono">({data.projects.length})</span>
        </div>

        {data.projects.length === 0 ? (
          <div className="pj-empty">
            <div className="pj-empty-glyph">⊞</div>
            <div className="pj-empty-msg">No projects yet</div>
            <div className="pj-empty-sub">
              A project groups the GitHub repos a build operates on — a primary extension plus the
              sibling repos that give agents cross-repo context. Name your first one to get started.
            </div>
            <CreateRow
              value={draft}
              onChange={setDraft}
              onCreate={onCreate}
              pending={createMutation.isPending}
              variant="empty"
            />
            {createMutation.error && (
              <span className="pj-err mono">{String(createMutation.error)}</span>
            )}
          </div>
        ) : (
          <div className="pj-list">
            <CreateRow
              value={draft}
              onChange={setDraft}
              onCreate={onCreate}
              pending={createMutation.isPending}
            />
            {createMutation.error && (
              <span className="pj-err mono">{String(createMutation.error)}</span>
            )}
            {data.projects.map((p) => (
              <ProjectCard key={p.id} project={p} />
            ))}
          </div>
        )}
      </div>
    </Page>
  )
}

function CreateRow({
  value,
  onChange,
  onCreate,
  pending,
  variant,
}: {
  value: string
  onChange: (v: string) => void
  onCreate: () => void
  pending: boolean
  variant?: 'empty'
}) {
  const enabled = value.trim().length > 0 && !pending
  return (
    <div className={variant === 'empty' ? 'pj-empty-create' : 'pj-create'}>
      <input
        className="pj-create-input"
        placeholder="new project name (e.g. 'Hey Fella')"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && enabled) onCreate()
        }}
      />
      <button
        type="button"
        className={`pj-btn ${enabled ? 'enabled' : ''}`}
        disabled={!enabled}
        onClick={() => enabled && onCreate()}
      >
        <span className="pj-btn-plus">+</span> create
      </button>
    </div>
  )
}

function ProjectCard({ project }: { project: Project }) {
  const queryClient = useQueryClient()
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['projects'] })

  const [collapsed, setCollapsed] = useState(false)
  const [adding, setAdding] = useState(false)
  const [editingName, setEditingName] = useState(false)
  const [name, setName] = useState(project.name)

  const rename = useMutation({
    mutationFn: (next: string) => projectsApi.update(project.id, { name: next }),
    onSuccess: () => {
      setEditingName(false)
      invalidate()
    },
  })
  const remove = useMutation({
    mutationFn: () => projectsApi.delete(project.id),
    onSuccess: invalidate,
  })

  const repoCount = `${project.repos.length} ${project.repos.length === 1 ? 'repo' : 'repos'}`

  return (
    <section className={`pj-card ${collapsed ? 'pj-card-collapsed' : ''}`}>
      <header className="pj-card-head">
        <button
          type="button"
          className="pj-disclosure"
          aria-expanded={!collapsed}
          title={collapsed ? 'expand' : 'collapse'}
          onClick={() => setCollapsed((c) => !c)}
        >
          <span className="pj-disclosure-caret">▾</span>
        </button>
        <div className="pj-card-head-left">
          {editingName ? (
            <input
              className="pj-name-input mono"
              value={name}
              autoFocus
              onChange={(e) => setName(e.target.value)}
              onBlur={() => {
                const next = name.trim()
                if (next && next !== project.name) rename.mutate(next)
                else {
                  setName(project.name)
                  setEditingName(false)
                }
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                if (e.key === 'Escape') {
                  setName(project.name)
                  setEditingName(false)
                }
              }}
            />
          ) : (
            <span
              className="pj-name"
              onClick={() => setEditingName(true)}
              title="click to rename"
            >
              {project.name}
            </span>
          )}
          <span className="pj-repocount mono">{repoCount}</span>
        </div>
        <button
          type="button"
          className="pj-delete mono"
          onClick={() => {
            if (
              confirm(
                `Delete project "${project.name}"? Move or delete its work items first — a project still referenced by work items can't be deleted.`,
              )
            ) {
              remove.mutate()
            }
          }}
          disabled={remove.isPending}
        >
          delete
        </button>
      </header>

      {!collapsed && (
        <>
          {project.repos.length > 0 && (
            <div className="pj-repos">
              {project.repos.map((repo) => (
                <RepoRow
                  key={repo.id}
                  projectId={project.id}
                  repo={repo}
                  onChange={invalidate}
                />
              ))}
            </div>
          )}

          <div className="pj-addrow">
            {adding ? (
              <AddRepoForm
                projectId={project.id}
                taken={project.repos.map((r) => r.fullName)}
                onCancel={() => setAdding(false)}
                onAdded={() => {
                  setAdding(false)
                  invalidate()
                }}
              />
            ) : (
              <button type="button" className="pj-add-btn" onClick={() => setAdding(true)}>
                <span className="pj-add-plus">+</span> add repo
              </button>
            )}
          </div>
        </>
      )}
    </section>
  )
}

function RepoRow({
  projectId,
  repo,
  onChange,
}: {
  projectId: number
  repo: ProjectRepo
  onChange: () => void
}) {
  const { org, short } = splitRepo(repo.fullName)
  const [editing, setEditing] = useState(false)
  const [purpose, setPurpose] = useState(repo.purpose ?? '')
  const [open, setOpen] = useState(false)

  const update = useMutation({
    mutationFn: (next: string) =>
      projectsApi.updateRepo(projectId, repo.id, { purpose: next || null }),
    onSuccess: () => {
      setEditing(false)
      onChange()
    },
  })
  const remove = useMutation({
    mutationFn: () => projectsApi.deleteRepo(projectId, repo.id),
    onSuccess: onChange,
  })
  const profile = useMutation({
    mutationFn: () => projectsApi.profileRepo(projectId, repo.id),
    onSuccess: onChange,
  })

  return (
    <>
      <div className="pj-repo">
        <span className="pj-repo-name" title={repo.fullName}>
          <span className="pj-repo-org">{org}</span>
          <span className="pj-repo-short">{short}</span>
        </span>
        {editing ? (
          <input
            className="pj-purpose-input"
            autoFocus
            value={purpose}
            placeholder="what this repo gives the agent as context…"
            onChange={(e) => setPurpose(e.target.value)}
            onBlur={() => {
              const next = purpose.trim()
              if (next !== (repo.purpose ?? '')) update.mutate(next)
              else setEditing(false)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
              if (e.key === 'Escape') {
                setPurpose(repo.purpose ?? '')
                setEditing(false)
              }
            }}
          />
        ) : repo.purpose ? (
          <span className="pj-repo-purpose" onClick={() => setEditing(true)} title="click to edit">
            {repo.purpose}
          </span>
        ) : (
          <span
            className="pj-repo-purpose pj-repo-purpose-empty"
            onClick={() => setEditing(true)}
            title="click to set a purpose"
          >
            no purpose set
          </span>
        )}
        <ProfileChip
          repo={repo}
          open={open}
          pending={profile.isPending}
          onToggle={() => setOpen((o) => !o)}
          onProfile={() => profile.mutate()}
        />
        <button
          type="button"
          className="pj-repo-x"
          title="remove repo"
          onClick={() => {
            if (confirm(`Remove ${repo.fullName} from this project?`)) remove.mutate()
          }}
        >
          ✕
        </button>
      </div>
      {open && (
        <ProfilePanel
          repo={repo}
          pending={profile.isPending}
          onProfile={() => profile.mutate()}
        />
      )}
      {profile.error && <span className="pj-err mono">{String(profile.error)}</span>}
    </>
  )
}

function ProfileChip({
  repo,
  open,
  pending,
  onToggle,
  onProfile,
}: {
  repo: ProjectRepo
  open: boolean
  pending: boolean
  onToggle: () => void
  onProfile: () => void
}) {
  // The chip is the whole profile affordance: unprofiled → trigger profiling;
  // running → progress; ready/failed → toggle the details panel.
  if (repo.profileStatus === 'unprofiled') {
    return (
      <button type="button" className="pj-profile-chip mono" disabled={pending} onClick={onProfile}>
        {pending ? 'profiling…' : 'profile'}
      </button>
    )
  }
  if (repo.profileStatus === 'running' || pending) {
    return <span className="pj-profile-chip pj-profile-running mono">profiling…</span>
  }
  const failed = repo.profileStatus === 'failed'
  return (
    <button
      type="button"
      className={`pj-profile-chip mono ${failed ? 'pj-profile-failed' : 'pj-profile-ready'}`}
      title={failed ? (repo.profilerRunFailure ?? 'profiler run failed') : 'view profile'}
      onClick={onToggle}
    >
      {failed ? 'profile failed' : 'profiled'} {open ? '▴' : '▾'}
    </button>
  )
}

function ProfilePanel({
  repo,
  pending,
  onProfile,
}: {
  repo: ProjectRepo
  pending: boolean
  onProfile: () => void
}) {
  const found = repo.profile
  const stack = [
    ...(found.languages ?? []),
    ...(found.frameworks ?? []),
    ...(found.package_managers ?? []),
  ]
  const verification = [
    ...(found.verification?.test_commands ?? []),
    ...(found.verification?.lint_commands ?? []),
    ...(found.verification?.typecheck_commands ?? []),
  ]
  return (
    <div className="pj-profile-panel">
      {repo.profileStatus === 'failed' && (
        <div className="pj-profile-failure mono">{repo.profilerRunFailure ?? 'profiler run failed'}</div>
      )}
      {found.stack_summary && <p className="pj-profile-summary">{found.stack_summary}</p>}
      {stack.length > 0 && (
        <div className="pj-profile-row">
          <span className="pj-profile-label">stack</span>
          {stack.map((item) => (
            <span key={item} className="pj-profile-tag mono">
              {item}
            </span>
          ))}
        </div>
      )}
      {verification.length > 0 && (
        <div className="pj-profile-row">
          <span className="pj-profile-label">verification</span>
          <div className="pj-profile-cmds">
            {verification.map((command) => (
              <code key={command} className="pj-profile-cmd mono">
                {command}
              </code>
            ))}
          </div>
        </div>
      )}
      {(found.recommended_skills ?? []).length > 0 && (
        <div className="pj-profile-row">
          <span className="pj-profile-label">skills</span>
          {(found.recommended_skills ?? []).map((skill) => (
            <span key={skill} className="pj-profile-tag mono">
              {skill}
            </span>
          ))}
        </div>
      )}
      <button type="button" className="pj-profile-rerun mono" disabled={pending} onClick={onProfile}>
        {pending ? 'profiling…' : 're-profile'}
      </button>
    </div>
  )
}

function AddRepoForm({
  projectId,
  taken,
  onCancel,
  onAdded,
}: {
  projectId: number
  taken: string[]
  onCancel: () => void
  onAdded: () => void
}) {
  const [pick, setPick] = useState('')
  const [purpose, setPurpose] = useState('')

  const ghRepos = useQuery({
    queryKey: ['github-repos'],
    queryFn: () => projectsApi.listGithubRepos(),
    staleTime: 60_000,
  })
  const add = useMutation({
    mutationFn: () =>
      projectsApi.addRepo(projectId, { fullName: pick, purpose: purpose.trim() || null }),
    onSuccess: onAdded,
  })

  const takenSet = new Set(taken.map((s) => s.toLowerCase()))
  const available = (ghRepos.data?.repos ?? []).filter(
    (r) => !takenSet.has(r.fullName.toLowerCase()),
  )

  return (
    <div className="pj-addform">
      <div className="pj-addform-fields">
        <div className="pj-addform-field">
          <label className="pj-addform-label">repository</label>
          <div className="pj-select-wrap">
            <select
              className="pj-select"
              value={pick}
              onChange={(e) => setPick(e.target.value)}
            >
              <option value="">
                {ghRepos.isLoading
                  ? 'loading repos…'
                  : available.length === 0
                    ? 'no more repos to add'
                    : '— pick a repo —'}
              </option>
              {available.map((r) => (
                <option key={r.fullName} value={r.fullName}>
                  {r.fullName}
                </option>
              ))}
            </select>
            <span className="pj-select-caret">▼</span>
          </div>
        </div>
        <div className="pj-addform-field">
          <label className="pj-addform-label">
            purpose <span className="pj-addform-optional">optional</span>
          </label>
          <input
            className="pj-purpose-input"
            placeholder="what this repo gives the agent as context…"
            value={purpose}
            onChange={(e) => setPurpose(e.target.value)}
          />
        </div>
        <div />
      </div>
      <div className="pj-addform-foot">
        <button
          type="button"
          className={`pj-add-confirm ${pick ? 'enabled' : ''}`}
          disabled={!pick || add.isPending}
          onClick={() => pick && add.mutate()}
        >
          add repo
        </button>
        <button type="button" className="pj-add-cancel" onClick={onCancel}>
          cancel
        </button>
        {ghRepos.isError && (
          <span className="pj-err mono">could not load repos — {String(ghRepos.error)}</span>
        )}
        {add.error && <span className="pj-err mono">{String(add.error)}</span>}
      </div>
    </div>
  )
}
