import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from druks.contrib import ship
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
