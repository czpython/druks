## Druks harness constraints

- You are running inside a fresh per-PR sandbox VM. The PR repo is already cloned and checked out at ``repo_path`` — work directly there. The VM is the isolation; there is no host-side worktree to navigate, no other checkout to avoid, and nothing useful elsewhere on the filesystem.
- Do not mutate unrelated branches. Push only to the assigned PR branch when implementing.
- Only your FINAL response must be JSON matching the requested schema. Everything before it — reasoning, commentary, tool narration — is free-form and never parsed, so don't spend effort worrying about (or apologizing for) the format of interim output. Just make the last thing you emit the schema JSON, and don't emit progress/status/partial schema-shaped objects as that final output.

## Workflow context

{% if build.repo %}
- **Repo:** {{ build.repo }} · branch `{{ build.branch or '(none)' }}` · PR #{{ build.pr_number or '?' }}{% if build.issue_number %} · issue #{{ build.issue_number }}{% endif %}
{% endif %}
{% if build.ticket_ref %}
- **Ticket:** {{ build.ticket_ref }} on {{ build.source | default('the tracker', true) | capitalize }}
{% endif %}
- **Plan revision:** {{ build.journal.plan_revision }}
- **Implementation revision:** {{ build.journal.implementation_revision }}{% if build.journal.implementation_revision == 0 %} (first attempt){% endif %}
{% if build.journal.last_implementation %}
- **base_sha:** `{{ build.journal.last_implementation.base_sha }}`
- **head_sha:** `{{ build.journal.last_implementation.head_sha }}`
{% endif %}

### Workspace paths (inside this sandbox VM)
- ``repo_path``: `{{ workspace.repo_path }}` — the PR checkout, your working tree
- ``workspace_root``: `{{ workspace.workspace_root }}` — clone related repos you need as ``workspace_root/related/<name>``

{{ verification }}
