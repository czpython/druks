from druks.prompts import render_prompt


async def _render(**kwargs) -> str:
    """Render the scope-brief slot exactly as the runtime invokes it: the run's
    call kwargs (remote_key, source) over the workflow's prompt context."""
    kwargs.setdefault("remote_key", "ACME-1")
    kwargs.setdefault("source", "linear")
    kwargs.setdefault("target_repo", "ClawHaven/acme-app")
    kwargs.setdefault("target_purpose", "")
    kwargs.setdefault("repos", [])
    kwargs.setdefault("scoped_label", "druks-scoped")
    kwargs.setdefault("post_refinement_status", None)
    kwargs.setdefault("recommended_skills", ("django-patterns",))
    return await render_prompt("build/scope/scope_brief.md", **kwargs)


async def test_prompt_includes_issue_and_repos():
    output = await _render(repos=[{"full_name": "ClawHaven/web", "purpose": "frontend"}])
    assert "ACME-1" in output
    assert "ClawHaven/web" in output
    assert "frontend" in output


async def test_prompt_tells_scoper_to_fetch_the_source_itself():
    # We never inject the description or comments — the agent self-fetches.
    # The prompt MUST therefore name the ticket and tell the scoper to read it,
    # or the model has no source material at all.
    output = await _render()
    assert "Read the source yourself" in output
    assert "ACME-1" in output
    # The prior brief block is the model's own output — it must be told to skip it.
    assert "# Druks scope brief" in output


async def test_prompt_reports_no_repositories_when_project_empty():
    output = await _render()
    assert "(no repositories registered for this project)" in output


async def test_prompt_owns_the_tracker_writes():
    output = await _render()
    assert "Write the brief into the ticket description" in output
    assert "druks-scoped" in output
    assert "open questions for ACME-1" in output


async def test_prompt_carries_the_recommended_skills():
    output = await _render(recommended_skills=("django-patterns", "django-models"))
    assert "django-patterns" in output
    assert "django-models" in output


async def test_prompt_moves_status_only_when_configured():
    parked = await _render(post_refinement_status="Ready for Agent")
    assert "Move the ticket's status to `Ready for Agent`" in parked
    left = await _render(post_refinement_status=None)
    assert "the label is the signal" in left


async def test_prompt_tells_scoper_to_focus_stack_hints_on_task_surface():
    output = await _render(
        repos=[{"full_name": "ClawHaven/acme-app", "purpose": "Django backend."}]
    )
    assert "specific task surface" in output
    assert "Do not include backend/database/server labels" in output


async def test_prompt_teaches_scoper_to_raise_oversized_work_as_a_question():
    # Oversized-ticket detection is the scoper's main upstream defence against
    # wasted review loops. It routes through needs_answers now (ask the operator
    # whether to split) rather than a structured proposal — if this guidance ever
    # drops out, scoping silently regresses to always-ready and we won't notice
    # until tickets get too big again.
    output = await _render()
    assert "too large to ship as one reviewable PR" in output
    assert "whether to split" in output
    assert "Vertical slices are the" in output


async def test_jira_prompt_carries_connector_routing_hint():
    """A codex run once concluded 'no Jira access' from an empty MCP
    resource listing and parked instead of calling the rovo tool."""
    output = await _render(source="jira", remote_key="SHRP-1")
    assert "rovo_getjiraissue" in output


async def test_linear_prompt_has_no_jira_hint():
    output = await _render()
    assert "rovo_getjiraissue" not in output
