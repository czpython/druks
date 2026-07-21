## Druks harness constraints

- You are running inside a fresh per-PR sandbox VM. The PR repo is already cloned and checked out at ``repo_path`` — work directly there. The VM is the isolation; there is no host-side worktree to navigate, no other checkout to avoid, and nothing useful elsewhere on the filesystem.
- Do not mutate unrelated branches. Push only to the assigned PR branch when implementing.
- Only your FINAL response must be JSON matching the requested schema. Everything before it — reasoning, commentary, tool narration — is free-form and never parsed, so don't spend effort worrying about (or apologizing for) the format of interim output. Just make the last thing you emit the schema JSON, and don't emit progress/status/partial schema-shaped objects as that final output.

## Workflow context

{% if build.repo %}
- **Repo:** {{ build.repo }} · branch `{{ build.branch or '(none)' }}` · PR #{{ build.pr_number or '?' }}{% if build.issue_number %} · issue #{{ build.issue_number }}{% endif %}
{% endif %}
{% if build.ticket_ref %}
- **Ticket:** {{ build.ticket_ref }}

  **MANDATORY FIRST ACTION — fetch the ticket. This is not a suggestion.** Your very first tool call MUST be to fetch `{{ build.ticket_ref }}` from {{ build.source | default('the tracker', true) | capitalize }} using your available tools, then read its full description and **every** comment before you read the codebase, write a plan, edit a file, or emit any output. Do not begin from the ticket reference, title, or the rendered plan alone — those are derived; the ticket and its operator comments are the binding source of truth, and frequently carry exact decisions you must honor verbatim (its description may also end with a `# Druks scope brief` section summarizing problem, scope, and acceptance criteria). The ONLY acceptable reason to proceed without the ticket's full text is a genuine tool failure, which you must report as a blocker — never guess or fabricate the requirements. If the source materially contradicts a plan or acceptance criteria rendered below, surface the conflict rather than silently proceeding.
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
{% if build.journal.plan.plan_markdown %}
## Current plan

{{ build.journal.plan.plan_markdown }}

{% endif %}
{% if build.journal.plan.acceptance_criteria %}
## Acceptance criteria

{% for ac in build.journal.plan.acceptance_criteria %}
### {{ ac.id }}

**Description:** {{ ac.description.strip() }}

{% if ac.verification %}
**Verification:** {{ ac.verification.strip() }}

{% endif %}
{% endfor %}
{% endif %}
{% if build.journal.reviewer_requirements() %}
## Reviewer requirements (active)

These are binding plan clarifications from the plan reviewer. Treat them as part of the plan and apply them.

{% for req in build.journal.reviewer_requirements() %}
### Requirement {{ loop.index }}

{{ req.body.strip() }}

{% endfor %}
{% endif %}
{% if build.journal.evaluations %}
## Prior implementation review

{% for review in build.journal.evaluations %}
### Round {{ loop.index }} — verdict: {{ review.verdict.value if review.verdict.value is defined else review.verdict }}

{% if review.body %}
{{ review.body.strip() }}

{% endif %}
{% endfor %}
{% endif %}
{% if build.journal.human_feedback %}
## Human feedback

{% for fb in build.journal.human_feedback %}
### {{ fb.reviewer }} — status: {{ fb.status }}

{% if fb.body %}
**Body:** {{ fb.body.strip() }}

{% endif %}
{% if fb.question %}
**Question:** {{ fb.question.strip() }}

{% endif %}
{% if fb.implementation_instructions %}
**Implementation instructions:** {{ fb.implementation_instructions.strip() }}

{% endif %}
{% endfor %}
{% endif %}
