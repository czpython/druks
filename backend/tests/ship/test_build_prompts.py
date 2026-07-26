import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from druks.contrib import ship
from druks.contrib.ship.contracts import PlanData
from druks.contrib.ship.journal import BuildJournal
from druks.contrib.ship.models import Project, ProjectRepo
from druks.contrib.ship.prompt_context import BuildPromptContext
from druks.prompts import render_prompt
from druks.workflows import FatalError

_OP_TEMPLATES = [
    "generate_plan.md",
    "review_plan.md",
    "revise_contract.md",
    "implement.md",
    "evaluate_implementation.md",
    "review_code.md",
    "triage_human_feedback.md",
]

# The roles that carry the plan, the acceptance criteria, and the ticket behind
# them. The code reviewer is deliberately outside this set: it grades the diff.
_CONTRACT_TEMPLATES = [name for name in _OP_TEMPLATES if name != "review_code.md"]

# The kwargs the workflow passes at each template's agent call site.
_CALL_KWARGS = {
    "generate_plan.md": {"answered_questions": [], "operator_note": "", "reviewer_notes": ""},
}


def _build() -> SimpleNamespace:
    """A stand-in BuildPromptContext exposing the fields the templates read —
    identity facts faked, the journal real and empty."""
    return SimpleNamespace(
        repo="acme/widget",
        work_item_url="https://druks.test/work-items/1",
        branch="agent/eng-1",
        pr_number=7,
        ticket_ref="ACME-1",
        source="github",
        issue_number=None,
        task_owner_name=None,
        task_owner_email=None,
        related_repos=[],
        skills=["python-house-rules"],
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
        f"ship/build/{template}",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        **_CALL_KWARGS.get(template, {}),
    )

    # The build-derived bits resolved — a leftover ``workflow`` ref would have
    # raised on StrictUndefined.
    assert "acme/widget" in output
    assert "- `/python-house-rules`" in output


async def test_implement_prompt_provisions_when_no_pr_exists():
    # The first delivery has no PR: the implementer is told to create the branch and
    # open the draft PR; the revision path (dismiss stale reviews) must not render.
    build = _build()
    build.branch = None
    build.pr_number = None
    output = await render_prompt(
        "ship/build/implement.md",
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
        "ship/build/generate_plan.md",
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
        "ship/build/generate_plan.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        answered_questions=[],
        operator_note="",
        reviewer_notes="Name the wire schema.\nSplit the migration.",
    )
    assert "## Plan reviewer critique" in output
    assert "> Name the wire schema.\n> Split the migration." in output


async def test_ruled_out_approaches_cross_the_plan_review_park():
    """The planner's rejected approaches ride the journal into implement — the park
    reaps the warm VM, so the journal is the only thing that carries."""
    build = _build()
    build.journal.add(
        PlanData(
            plan_markdown="p",
            rejected_approaches=["a status column on Run — derives from DBOS already"],
        )
    )
    output = await render_prompt(
        "ship/build/implement.md",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )
    assert "## Ruled out" in output
    assert "a status column on Run — derives from DBOS already" in output


async def test_the_planner_resolves_the_assignee_not_the_reviewer():
    # Only the planner always runs, so only its prompt owns the resolution.
    planner = await render_prompt(
        "ship/build/generate_plan.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        **_CALL_KWARGS["generate_plan.md"],
    )
    reviewer = await render_prompt(
        "ship/build/review_plan.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )
    assert "ASSIGNEE RESOLUTION" in planner
    assert "assignee_github_login" not in reviewer


@pytest.mark.parametrize("template", _CONTRACT_TEMPLATES)
async def test_build_prompt_orders_the_ticket_fetch(template):
    """Every agent that works the contract is ordered to fetch the ticket from its
    source before acting — a mandatory first step, not a suggestion. Regression
    guard for the silently-skipped-fetch bug (agents working off the ticket ref
    alone). The code reviewer is ordered to read the diff instead."""
    build = _build()
    build.source = "linear"
    output = await render_prompt(
        f"ship/build/{template}",
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
        "ship/build/review_code.md",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )
    assert "File a follow-up sub-issue" in output
    assert "same tracker tools" in output  # the agent writes it, not druks
    assert '"summary"' in output  # the only thing it returns


async def test_contract_context_is_omitted_only_from_code_review():
    build = _build()
    build.journal = SimpleNamespace(
        plan_revision=1,
        implementation_revision=1,
        last_implementation=SimpleNamespace(base_sha="base123", head_sha="head456"),
        plan=SimpleNamespace(
            plan_markdown="PLAN-MARKER",
            acceptance_criteria=[
                SimpleNamespace(
                    id="AC-1",
                    description="AC-MARKER",
                    verification="VERIFY-MARKER",
                )
            ],
            rejected_approaches=["RULED-OUT-MARKER"],
        ),
        evaluations=[SimpleNamespace(verdict="pass", body="EVALUATION-MARKER")],
        human_feedback=[
            SimpleNamespace(
                reviewer="REVIEWER-MARKER",
                body="FEEDBACK-MARKER",
                question="QUESTION-MARKER",
                implementation_instructions="INSTRUCTIONS-MARKER",
            )
        ],
    )
    headings = (
        "## Current plan",
        "## Acceptance criteria",
        "## Prior implementation review",
        "## Human feedback",
    )
    markers = headings + (
        "MANDATORY FIRST ACTION — fetch the ticket",
        "PLAN-MARKER",
        "AC-MARKER",
        "RULED-OUT-MARKER",
        "EVALUATION-MARKER",
        "FEEDBACK-MARKER",
        "QUESTION-MARKER",
        "INSTRUCTIONS-MARKER",
    )
    for template in _OP_TEMPLATES:
        output = await render_prompt(
            f"ship/build/{template}",
            build=build,
            verification="VERIFICATION-BLOCK",
            workspace=_workspace(),
            **_CALL_KWARGS.get(template, {}),
        )
        if template == "review_code.md":
            assert all(marker not in output for marker in markers)
            assert "`git diff <base_sha>..<head_sha>`" in output
        else:
            positions = [output.index(heading) for heading in headings]
            assert positions == sorted(positions)
            assert all(marker in output for marker in markers)


def test_build_prompt_context_covers_template_attrs():
    # Every build prompt reads build.<attr>; assert BuildPromptContext carries them
    # all, so a template ref can never outrun the context contract.
    prompts_dir = Path(ship.__file__).parent / "templates/build"
    templates = sorted(prompts_dir.glob("*.md"))
    assert templates, f"no build prompts under {prompts_dir}"
    attrs: set[str] = set()
    for template in templates:
        attrs |= set(re.findall(r"\bbuild\.([a-z_]+)", template.read_text()))
    fields = set(BuildPromptContext.__dataclass_fields__)
    missing = sorted(a for a in attrs if a not in fields)
    assert not missing, f"BuildPromptContext missing template attrs: {missing}"


def test_get_for_repo_returns_the_repo(druks_db):
    project = Project.create(name="acme/widget")
    ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    assert ProjectRepo.get_for_repo("acme/widget").full_name == "acme/widget"


def test_get_for_repo_raises_when_the_repo_was_transferred(druks_db):
    # Registered under the new name; a run still holding the old name must fail
    # with the reason, not an opaque NoneType crash.
    project = Project.create(name="czpython/druks")
    ProjectRepo.create(project_id=project.id, full_name="czpython/druks")
    with pytest.raises(FatalError, match="renamed or transferred"):
        ProjectRepo.get_for_repo("clawhaven/druks", raise_on_missing=True)
