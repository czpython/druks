import pytest
from druks.durable.exceptions import WorkflowError
from druks.workflows import Journal, OperatorReply
from pydantic import BaseModel


class Finding(BaseModel):
    status: str
    title: str = ""


class Grade(BaseModel):
    decision: str


def _journal() -> Journal:
    journal = Journal()
    journal.add(Finding(status="success", title="a"))
    journal.add(Grade(decision="approve"))
    journal.add(Finding(status="failed", title="b"))
    journal.add(Finding(status="success", title="c"))
    return journal


def test_filter_selects_by_contract_type_in_call_order():
    journal = _journal()
    assert [finding.title for finding in journal.filter(Finding)] == ["a", "b", "c"]
    assert [grade.decision for grade in journal.filter(Grade)] == ["approve"]


def test_latest_is_the_newest_match_or_none():
    journal = _journal()
    latest = journal.latest(Finding)
    assert latest and latest.title == "c"
    assert Journal().latest(Finding) is None


def test_filters_are_flat_anded_equality():
    journal = _journal()
    assert [f.title for f in journal.filter(Finding, status="success")] == ["a", "c"]
    assert [f.title for f in journal.filter(Finding, status="success", title="a")] == ["a"]
    assert journal.latest(Finding, status="missing") is None
    assert Journal().filter(Finding, status="success") == []


def test_a_filter_typo_raises_when_entries_scan():
    with pytest.raises(AttributeError):
        _journal().filter(Finding, verdict="success")


def test_filter_after_anchors_by_identity_not_equality():
    journal = Journal()
    first = Finding(status="success")  # structurally equal twins — the anchor
    second = Finding(status="success")  # must resolve by identity
    journal.add(first)
    journal.add(Grade(decision="approve"))
    journal.add(second)
    anchored = journal.filter(Finding, after=first)
    assert len(anchored) == 1 and anchored[0] is second
    assert journal.filter(Grade, after=second) == []
    with pytest.raises(WorkflowError):
        journal.filter(Finding, after=Finding(status="success"))


def test_review_reply_validates_the_resume_wire_shape():
    # The resume endpoint's payload — {action, answers, note} — validates
    # unchanged; answers and note default for partial senders.
    reply = OperatorReply.model_validate(
        {"action": "request_changes", "answers": {"q1": "redis"}, "note": "why q1"}
    )
    assert reply.action == "request_changes"
    assert (reply.answers, reply.note) == ({"q1": "redis"}, "why q1")
    assert OperatorReply.model_validate({"action": "approve"}).answers == {}
    # The gate's pinned name is the wire's "review", untouched by the class name.
    assert OperatorReply.name == "review"
