import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from druks.contrib import software_factory
from druks.contrib.software_factory.journal import BuildJournal
from druks.contrib.software_factory.models import Project, ProjectRepo
from druks.contrib.software_factory.policy import RepoPolicy
from druks.contrib.software_factory.prompt_context import BuildPromptContext
from druks.prompts import render_prompt
from druks.workflows import FatalError

_OP_TEMPLATES = [
    "generate_plan.md",
    "review_plan.md",
    "revise_contract.md",
    "implement.md",
    "evaluate_implementation.md",
    "triage_human_feedback.md",
]

# The kwargs the workflow passes at each template's agent call site.
_CALL_KWARGS = {
    "generate_plan.md": {"answered_questions": [], "operator_note": "", "reviewer_notes": ""},
}


def _build(*, review_code: bool = True) -> SimpleNamespace:
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
        review_code=review_code,
        review_mode="approve",
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
        f"software_factory/build/{template}",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        **_CALL_KWARGS.get(template, {}),
    )


async def _generate_plan_prompt(
    *, answered_questions=None, operator_note="", reviewer_notes=""
) -> str:
    return await render_prompt(
        "software_factory/build/generate_plan.md",
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


async def test_evaluation_prompt_renders_code_review_lens_only_when_enabled():
    headings = {}
    for review_code in (True, False):
        prompt = await render_prompt(
            "software_factory/build/evaluate_implementation.md",
            build=_build(review_code=review_code),
            verification="VERIFICATION-BLOCK",
            workspace=_workspace(),
        )
        headings[review_code] = {line for line in prompt.splitlines() if line.startswith("## ")}

    assert headings[False] < headings[True]
    assert len(headings[True] - headings[False]) == 1


async def test_approve_mode_posts_verdict_reviews():
    prompt = await render_prompt(
        "software_factory/build/evaluate_implementation.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )

    assert "an **approving** verdict → `APPROVE`" in prompt
    assert "Submit every review as a `COMMENT` event" not in prompt


async def test_comment_mode_swaps_the_review_event_mapping():
    # The operator authored the PR, and GitHub refuses an author's APPROVE /
    # REQUEST_CHANGES — the prompt swaps the event mapping for comment-mode.
    build = _build()
    build.review_mode = "comment"

    prompt = await render_prompt(
        "software_factory/build/evaluate_implementation.md",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )

    assert "Submit every review as a `COMMENT` event" in prompt
    assert "an **approving** verdict → `APPROVE`" not in prompt


def test_build_prompt_context_covers_template_attrs():
    # Every build prompt reads build.<attr>; assert BuildPromptContext carries them
    # all, so a template ref can never outrun the context contract.
    prompts_dir = Path(software_factory.__file__).parent / "templates/build"
    templates = sorted(prompts_dir.glob("*.md"))
    assert templates, f"no build prompts under {prompts_dir}"
    attrs: set[str] = set()
    for template in templates:
        attrs |= set(re.findall(r"\bbuild\.([a-z_]+)", template.read_text()))
    fields = set(BuildPromptContext.__dataclass_fields__)
    missing = sorted(a for a in attrs if a not in fields)
    assert not missing, f"BuildPromptContext missing template attrs: {missing}"


async def test_get_for_repo_returns_the_repo(druks_db):
    project = await Project.create(name="acme/widget")
    await ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    assert (await ProjectRepo.get_for_repo("acme/widget")).full_name == "acme/widget"


async def test_get_for_repo_raises_when_the_repo_was_transferred(druks_db):
    # Registered under the new name; a run still holding the old name must fail
    # with the reason, not an opaque NoneType crash.
    project = await Project.create(name="czpython/druks")
    await ProjectRepo.create(project_id=project.id, full_name="czpython/druks")
    with pytest.raises(FatalError, match="renamed or transferred"):
        await ProjectRepo.get_for_repo("clawhaven/druks", raise_on_missing=True)
