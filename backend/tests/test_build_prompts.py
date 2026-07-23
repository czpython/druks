import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from druks.build import workflows as build_workflows
from druks.build.journal import BuildJournal
from druks.build.prompt_context import BuildPromptContext
from druks.prompts import render_prompt

_OP_TEMPLATES = [
    "generate_plan.md",
    "review_plan.md",
    "revise_contract.md",
    "implement.md",
    "evaluate_implementation.md",
    "review_code.md",
    "triage_human_feedback.md",
]

# The kwargs the workflow passes at each template's agent call site.
_CALL_KWARGS = {
    "generate_plan.md": {"answered_questions": [], "operator_note": "", "reviewer_notes": ""},
}


def _build() -> SimpleNamespace:
    """A stand-in BuildPromptContext exposing the fields the templates read —
    identity facts faked, the journal real and empty."""
    return SimpleNamespace(
        repo="acme/widget",
        branch="agent/eng-1",
        pr_number=7,
        ticket_ref="ACME-1",
        source="github",
        issue_number=None,
        task_owner_name=None,
        task_owner_email=None,
        related_repos=[],
        journal=BuildJournal(),
    )


def _workspace() -> SimpleNamespace:
    return SimpleNamespace(
        repo_path="/home/agent/work/repo",
        workspace_root="/home/agent/work",
    )


@pytest.mark.parametrize("template", _OP_TEMPLATES)
async def test_build_operation_prompt_renders(template):
    output = await render_prompt(
        f"build/build_workflow/{template}",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        **_CALL_KWARGS.get(template, {}),
    )

    # The build-derived bits resolved — a leftover ``workflow`` ref would have
    # raised on StrictUndefined.
    assert "acme/widget" in output


async def test_implement_prompt_provisions_when_no_pr_exists():
    # The first delivery has no PR: the implementer is told to create the branch and
    # open the draft PR; the revision path (dismiss stale reviews) must not render.
    build = _build()
    build.branch = None
    build.pr_number = None
    output = await render_prompt(
        "build/build_workflow/implement.md",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )
    assert "gh pr create --draft" in output
    # The PR body carries the plan — what reviewers review the diff against.
    assert "## Plan" in output
    assert "dismiss the PR's existing reviews" not in output


async def test_generate_plan_prompt_quotes_operator_content():
    """Free-text answers and the operator's note render block-quoted line by line —
    operator words stay answer content in the prompt, never instruction text."""
    output = await render_prompt(
        "build/build_workflow/generate_plan.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        answered_questions=[{"question": "Which cache?", "answer": "redis\nwith a 5m TTL"}],
        operator_note="Tighten the rollout.\nSplit phase 2.",
        reviewer_notes="",
    )
    assert "> redis\n  > with a 5m TTL" in output
    assert "> Tighten the rollout.\n> Split phase 2." in output


async def test_generate_plan_prompt_quotes_the_reviewer_critique():
    output = await render_prompt(
        "build/build_workflow/generate_plan.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        answered_questions=[],
        operator_note="",
        reviewer_notes="Name the wire schema.\nSplit the migration.",
    )
    assert "## Plan reviewer critique" in output
    assert "> Name the wire schema.\n> Split the migration." in output


async def test_the_planner_resolves_the_assignee_not_the_reviewer():
    # Only the planner always runs, so only its prompt owns the resolution.
    planner = await render_prompt(
        "build/build_workflow/generate_plan.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        **_CALL_KWARGS["generate_plan.md"],
    )
    reviewer = await render_prompt(
        "build/build_workflow/review_plan.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )
    assert "ASSIGNEE RESOLUTION" in planner
    assert "assignee_github_login" not in reviewer


@pytest.mark.parametrize("template", _OP_TEMPLATES)
async def test_build_prompt_orders_the_ticket_fetch(template):
    """Every build agent is ordered to fetch the ticket from its source before
    acting — a mandatory first step, not a suggestion. Regression guard for the
    silently-skipped-fetch bug (agents working off the ticket ref alone)."""
    build = _build()
    build.source = "linear"
    output = await render_prompt(
        f"build/build_workflow/{template}",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        **_CALL_KWARGS.get(template, {}),
    )
    assert "MANDATORY FIRST ACTION" in output
    assert "fetch `ACME-1`" in output
    assert "from Linear" in output


async def test_review_code_prompt_owns_its_followup_subissue():
    """The reviewer files its own follow-up sub-issue via its tracker tools —
    regression guard for the dangling promise left when druks-side sub-issue
    creation was removed and nothing filed it. LLM-first: the agent does the
    tracker write, druks acts on nothing it returns."""
    build = _build()
    build.source = "linear"
    output = await render_prompt(
        "build/build_workflow/review_code.md",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )
    assert "File a follow-up sub-issue" in output
    assert "same tracker tools" in output  # the agent writes it, not druks
    assert '"summary"' in output  # the only thing it returns


def test_build_prompt_context_covers_template_attrs():
    # Every build prompt reads build.<attr>; assert BuildPromptContext carries them
    # all, so a template ref can never outrun the context contract.
    prompts_root = Path(build_workflows.__file__).resolve().parents[2]
    prompts_dir = prompts_root / "templates/prompts/build/build_workflow"
    attrs: set[str] = set()
    for template in prompts_dir.glob("*.md"):
        attrs |= set(re.findall(r"\bbuild\.([a-z_]+)", template.read_text()))
    fields = set(BuildPromptContext.__dataclass_fields__)
    missing = sorted(a for a in attrs if a not in fields)
    assert not missing, f"BuildPromptContext missing template attrs: {missing}"
