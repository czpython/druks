from druks.build.contracts import ImplementationOutput, PlanData, ReviewWork, TriageOutput
from druks.build.enums import HumanFeedbackAction
from druks.build.journal import BuildJournal


def _journal(*entries) -> BuildJournal:
    journal = BuildJournal()
    for entry in entries:
        journal.add(entry)
    return journal


def _implementation(status: str = "success") -> ImplementationOutput:
    delivered = status == "success"
    return ImplementationOutput.model_validate(
        {
            "type": "result",
            "status": status,
            "base_sha": "a" if delivered else None,
            "head_sha": "b" if delivered else None,
            "commit_sha": "b" if delivered else None,
            "branch": "agent/eng-1" if delivered else None,
            "pr_number": 7 if delivered else None,
            "files_changed": [],
            "acceptance_results": [],
            "checks": [],
            "known_risks": [],
            "summary": "",
            "workspace_path": "/repo",
            "workspace_retention": None,
        }
    )


def test_plan_is_the_latest_or_an_empty_stand_in():
    assert _journal().plan.plan_markdown == ""
    journal = _journal(PlanData(plan_markdown="v1"), PlanData(plan_markdown="v2"))
    assert journal.plan.plan_markdown == "v2"
    assert journal.plan_revision == 2


def test_only_shipped_deliveries_count_as_implementations():
    journal = _journal(_implementation(), _implementation(status="needs_clarification"))
    assert len(journal.implementations) == 1
    assert journal.implementation_revision == 1
    last = journal.last_implementation
    assert last and last.status == "success"
    assert _journal().last_implementation is None


def test_assignee_scan_falls_through_unresolved_revisions():
    journal = _journal(
        PlanData(plan_markdown="v1", assignee_github_login="alice"),
        PlanData(plan_markdown="v2", assignee_github_login="bob"),
        PlanData(plan_markdown="v3"),  # a revision that resolved nobody
    )
    assert journal.assignee_github_login == "bob"
    assert _journal(PlanData()).assignee_github_login is None


def test_human_feedback_pairs_each_request_changes_reply_with_its_triage():
    journal = _journal(
        ReviewWork(action="approve", reviewer="carol"),  # never triaged, never paired
        ReviewWork(action="request_changes", reviewer="alice", body="raw review text"),
        TriageOutput(
            action=HumanFeedbackAction.CHANGE_REQUIRED,
            body="rename the flag",
            question="",
            implementation_instructions="rename X to Y",
        ),
        ReviewWork(action="request_changes", reviewer=None),
        TriageOutput(
            action=HumanFeedbackAction.QUESTION,
            body="",
            question="is the flag permanent?",
            implementation_instructions="",
        ),
    )
    assert journal.human_feedback == [
        {
            "reviewer": "alice",
            "body": "rename the flag",
            "question": "",
            "implementation_instructions": "rename X to Y",
        },
        {
            "reviewer": "(triage)",
            "body": "",
            "question": "is the flag permanent?",
            "implementation_instructions": "",
        },
    ]
