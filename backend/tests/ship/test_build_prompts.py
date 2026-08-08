import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from druks.contrib import ship
from druks.contrib.ship.contracts import ReviewOutput
from druks.contrib.ship.enums import ReviewDecision
from druks.contrib.ship.journal import BuildJournal
from druks.contrib.ship.models import Project, ProjectRepo
from druks.contrib.ship.policy import RepoPolicy
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
        skills=[
            SimpleNamespace(
                name="python-house-rules",
                description="Apply the Python house rules.",
            )
        ],
        journal=BuildJournal(),
    )


def _workspace() -> SimpleNamespace:
    return SimpleNamespace(
        repo_path="/home/agent/work/repo",
        workspace_root="/home/agent/work",
    )


@pytest.mark.parametrize("template", _OP_TEMPLATES)
async def test_build_operation_prompt_renders(template):
    await render_prompt(
        f"ship/build/{template}",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        **_CALL_KWARGS.get(template, {}),
    )


async def _generate_plan_prompt(
    *, answered_questions=None, operator_note="", reviewer_notes=""
) -> str:
    return await render_prompt(
        "ship/build/generate_plan.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        answered_questions=answered_questions or [],
        operator_note=operator_note,
        reviewer_notes=reviewer_notes,
    )


async def test_generate_plan_prompt_prioritizes_operator_note_over_reviewer_critique():
    prompt = await _generate_plan_prompt(
        operator_note="keep the existing endpoint",
        reviewer_notes="replace the endpoint",
    )

    assert "## Operator note" in prompt
    assert "> keep the existing endpoint" in prompt
    assert "## Plan reviewer critique" in prompt
    assert "> replace the endpoint" in prompt
    assert (
        prompt.count(
            "Where the operator's note conflicts with this critique, the operator's note wins."
        )
        == 1
    )
    assert "Before deep code reading" not in prompt


async def test_generate_plan_prompt_without_reviewer_notes_omits_critique_instructions():
    prompt = await _generate_plan_prompt(operator_note="keep the existing endpoint")

    assert "## Plan reviewer critique" not in prompt
    assert "Where the operator's note conflicts with this critique" not in prompt


async def test_generate_plan_prompt_keeps_first_draft_ambiguity_instructions():
    prompt = await _generate_plan_prompt()

    assert "Before deep code reading" in prompt


async def test_verification_profile_renders_ci_provenance_per_command():
    block = await RepoPolicy().verification_block(
        profile={
            "verification": {
                "lint_commands": [{"command": "ruff check .", "ci_check": "Backend / lint"}],
                "typecheck_commands": [],
                "test_commands": [{"command": "pytest", "ci_check": None}],
            }
        },
        repo=None,
    )

    assert block.splitlines() == [
        "## Verification profile",
        "",
        "**Lint:**",
        "- `ruff check .` — CI: `Backend / lint`",
        "**Tests:**",
        "- `pytest`",
    ]


async def test_reviewer_scrutiny_line_reports_each_seat():
    async def scrutiny(build) -> str:
        prompt = await render_prompt(
            "ship/build/review_code.md",
            build=build,
            verification="VERIFICATION-BLOCK",
            workspace=_workspace(),
        )
        lines = (line.strip() for line in prompt.splitlines())
        return next(line for line in lines if line.startswith("Scrutiny:"))

    unreviewed = _build()
    assert (
        await scrutiny(unreviewed)
        == "Scrutiny: plan critic skipped · evaluation rounds: 0 · line review ran"
    )

    reviewed = _build()
    reviewed.journal.add(ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="split it"))
    reviewed.journal.add(ReviewOutput(decision=ReviewDecision.APPROVE, body="clean"))
    assert (
        await scrutiny(reviewed)
        == "Scrutiny: plan critic ran (2) · evaluation rounds: 0 · line review ran"
    )


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
