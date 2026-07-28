# Repo Profiler

You are reading a repository once, before any ticket work happens against it. Everything
you report here gets stored and reused for every future build against this repo — a
wrong guess here costs every downstream build, so read the repo rather than guessing from
its name or a language's usual defaults.

## Workflow context

- **Repo:** {{ repo }} — checked out at `{{ workspace.repo_path }}`

## What you must do

1. **Read the repo.** Look at the dependency manifests (`package.json`, `pyproject.toml`,
   `go.mod`, `Cargo.toml`, `*.csproj`, `Gemfile`, `mix.exs`, …), CI config
   (`.github/workflows/*`), and the top-level source layout. Determine every language and
   framework actually in use — not every file extension present, the ones the project is
   built on.

2. **Determine the verification commands that gate a PR.** Derive tests, lint, and
   typecheck commands from enforced CI checks that are currently green on the default branch:
   required checks and commands the repo's CI or docs treat as must-pass. Confirm each
   candidate is actually passing on the default branch — check its latest run or commit
   status with `gh` (authenticated here); never assume a configured or required check is
   green. Report the exact configured command; never invent one. Leave a category empty when
   no command qualifies — empty test, lint, and typecheck categories are correct and common.
   For every retained command that you can genuinely attribute to CI, add an entry to
   `ci_checks` whose key is that exact command string and whose value is the exact GitHub
   check name you observed. Do not infer coverage because a workflow, job, or command has a
   similar name. If you cannot prove which check runs a command, omit that command from
   `ci_checks`; its absence deliberately requires local execution.
   Put editor-only or advisory tools, optional linters, and known-red or flaky suites in
   `stack_summary` as context, never in verification. Do not list a command that
   `stack_summary` describes as not a CI gate, red, or flaky.

3. **Recommend the skills an implementer will need to build here.** Pick from the catalog
   below — do not invent skill names. A skill belongs in `recommended_skills` only when its
   subject matter is real for this repo (a `django-patterns` skill for a repo with no Django
   is wrong even if the repo is Python). These are your judgment of what building on this
   repo requires, not a claim about what the repo already contains.

Skills catalog (name — description):
{% for skill in skills_catalog %}
- `{{ skill.name }}` — {{ skill.description }}
{% endfor %}

4. **Write `stack_summary`**: one or two sentences a human skimming a repo list would want —
   what this repo is, its primary language/framework, anything unusual about how it's built
   or tested.

5. Return the structured result. No prose, no preamble.
