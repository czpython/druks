import pytest
from druks.accounts.models import Account
from druks.apps.loader import iter_apps
from druks.contrib.software_factory.issues.enums import Status
from druks.contrib.software_factory.issues.exceptions import (
    InvalidPrefix,
    PrefixLocked,
    ProjectNotFound,
)
from druks.contrib.software_factory.issues.models import Comment, IssuesProject, Ticket
from sqlalchemy.exc import IntegrityError


def test_issues_is_not_a_bundled_app():
    assert "issues" not in {app.name for app in iter_apps()}


async def test_ticket_identifiers_are_monotonic_per_project_and_never_reused():
    dru = await IssuesProject.create(name="druks", prefix="dru")
    first = await Ticket.create(project_id=dru.id, title="one")
    second = await Ticket.create(project_id=dru.id, title="two")
    assert first.identifier == "DRU-1"
    assert second.identifier == "DRU-2"

    await first.delete()
    third = await Ticket.create(project_id=dru.id, title="three")
    assert third.identifier == "DRU-3"

    eng = await IssuesProject.create(name="engine", prefix="eng")
    other = await Ticket.create(project_id=eng.id, title="eng-first")
    assert other.identifier == "ENG-1"


async def test_unknown_project_refuses_a_ticket():
    with pytest.raises(ProjectNotFound):
        await Ticket.create(project_id=0, title="orphan")


async def test_duplicate_project_names_fail():
    await IssuesProject.create(name="alpha", prefix="alp")
    with pytest.raises(IntegrityError):
        await IssuesProject.create(name="alpha", prefix="bet")


async def test_duplicate_project_prefixes_fail():
    await IssuesProject.create(name="alpha", prefix="alp")
    with pytest.raises(IntegrityError):
        await IssuesProject.create(name="other", prefix="alp")


async def test_prefix_must_be_two_to_six_letters():
    with pytest.raises(InvalidPrefix):
        await IssuesProject.create(name="short", prefix="A")
    with pytest.raises(InvalidPrefix):
        await IssuesProject.create(name="digits", prefix="DR1")


async def test_prefix_cannot_change_after_a_ticket_is_minted():
    project = await IssuesProject.create(name="locked", prefix="lok")
    await project.set_prefix("lokx")
    assert project.prefix == "LOKX"

    await Ticket.create(project_id=project.id, title="minted")
    with pytest.raises(PrefixLocked):
        await project.set_prefix("newpre")
    assert (await IssuesProject.get(project.id)).prefix == "LOKX"


async def test_comments_are_rows_and_empty_is_a_list():
    account = await Account.get_or_create("op@example.com")
    project = await IssuesProject.create(name="thread", prefix="thd")
    ticket = await Ticket.create(project_id=project.id, title="quiet")

    assert await ticket.list_comments() == []

    first = await ticket.add_comment(author_id=account.id, body="first")
    second = await ticket.add_comment(author_id=account.id, body="second")
    listed = await ticket.list_comments()
    assert [comment.body for comment in listed] == ["first", "second"]
    assert listed[0].id == first.id
    assert listed[1].id == second.id
    assert all(isinstance(comment, Comment) for comment in listed)


async def test_list_board_omits_cancelled():
    project = await IssuesProject.create(name="board", prefix="brd")
    live = await Ticket.create(project_id=project.id, title="live")
    gone = await Ticket.create(project_id=project.id, title="gone")
    await gone.set_status(Status.CANCELLED)

    board = await Ticket.list_board()
    identifiers = {ticket.identifier for ticket in board}
    assert live.identifier in identifiers
    assert gone.identifier not in identifiers
    found = await Ticket.get_for_identifier(live.identifier)
    assert found is not None
    assert found.id == live.id
    assert found.get_label() == live.identifier
    assert found.get_summary().title == "live"
    assert found.get_summary().status is Status.TODO
