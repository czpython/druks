import pytest
from druks.accounts.models import Account
from druks.apps.loader import iter_apps
from druks.contrib.software_factory.exceptions import (
    InvalidPrefix,
    MissingPrefix,
    PrefixLocked,
    PrefixTaken,
    ProjectNotFound,
    RepoNotFound,
)
from druks.contrib.software_factory.issues.enums import Status
from druks.contrib.software_factory.issues.models import Comment, Ticket
from druks.contrib.software_factory.models import Project, ProjectRepo
from sqlalchemy.exc import IntegrityError


async def _open_repo(*, name="Acme", prefix="dru", full_name="acme/druks"):
    project = await Project.create(name=name, prefix=prefix)
    return await ProjectRepo.create(project_id=project.id, full_name=full_name)


def test_issues_is_not_a_bundled_app():
    assert "issues" not in {app.name for app in iter_apps()}


async def test_ticket_identifiers_are_monotonic_per_project_and_never_reused():
    dru = await _open_repo(name="Druks", prefix="dru", full_name="acme/druks")
    first = await Ticket.create(repo_id=dru.id, title="one")
    second = await Ticket.create(repo_id=dru.id, title="two")
    assert first.identifier == "DRU-1"
    assert second.identifier == "DRU-2"

    await first.delete()
    third = await Ticket.create(repo_id=dru.id, title="three")
    assert third.identifier == "DRU-3"

    eng = await _open_repo(name="Engine", prefix="eng", full_name="acme/engine")
    other = await Ticket.create(repo_id=eng.id, title="eng-first")
    assert other.identifier == "ENG-1"


async def test_unknown_repo_refuses_a_ticket():
    with pytest.raises(RepoNotFound):
        await Ticket.create(repo_id=0, title="orphan")


async def test_a_project_without_a_prefix_refuses_a_ticket():
    project = await Project.create(name="bare")
    repo = await ProjectRepo.create(project_id=project.id, full_name="acme/bare")
    with pytest.raises(MissingPrefix):
        await Ticket.create(repo_id=repo.id, title="orphan")
    with pytest.raises(ProjectNotFound):
        await Project.mint_identifier(0)


async def test_duplicate_project_names_fail():
    await Project.create(name="alpha", prefix="alp")
    with pytest.raises(IntegrityError):
        await Project.create(name="alpha", prefix="bet")


async def test_duplicate_project_prefixes_fail():
    await Project.create(name="alpha", prefix="alp")
    with pytest.raises(PrefixTaken, match="ALP"):
        await Project.create(name="other", prefix="alp")


async def test_set_prefix_refuses_a_prefix_another_project_holds():
    await Project.create(name="BOX", prefix="box")
    acme = await Project.create(name="Acme")
    with pytest.raises(PrefixTaken, match="BOX"):
        await acme.set_prefix("box")


async def test_prefix_must_be_two_to_six_letters():
    with pytest.raises(InvalidPrefix):
        await Project.create(name="short", prefix="A")
    with pytest.raises(InvalidPrefix):
        await Project.create(name="digits", prefix="DR1")


async def test_prefix_cannot_change_after_a_ticket_is_minted():
    project = await Project.create(name="locked", prefix="lok")
    repo = await ProjectRepo.create(project_id=project.id, full_name="acme/locked")
    await project.set_prefix("lokx")
    assert project.prefix == "LOKX"

    await Ticket.create(repo_id=repo.id, title="minted")
    with pytest.raises(PrefixLocked):
        await project.set_prefix("newpre")
    assert (await Project.get(project.id)).prefix == "LOKX"


async def test_comments_are_rows_and_empty_is_a_list():
    account = await Account.get_or_create("op@example.com")
    repo = await _open_repo(name="Thread", prefix="thd", full_name="acme/thread")
    ticket = await Ticket.create(repo_id=repo.id, title="quiet")

    assert await ticket.list_comments() == []

    first = await ticket.add_comment(author_id=account.id, body="first")
    second = await ticket.add_comment(author_id=account.id, body="second")
    listed = await ticket.list_comments()
    assert [comment.body for comment in listed] == ["first", "second"]
    assert listed[0].id == first.id
    assert listed[1].id == second.id
    assert all(isinstance(comment, Comment) for comment in listed)


async def test_list_board_omits_cancelled():
    repo = await _open_repo(name="Board", prefix="brd", full_name="acme/board")
    live = await Ticket.create(repo_id=repo.id, title="live")
    gone = await Ticket.create(repo_id=repo.id, title="gone")
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


async def test_list_matching_filters_by_assignee_creator_and_repo():
    account = await Account.get_or_create("op@example.com")
    dru = await _open_repo(name="Filter", prefix="flt", full_name="acme/filter")
    other = await _open_repo(name="Other", prefix="oth", full_name="acme/other")
    await Ticket.create(repo_id=dru.id, title="mine", assignee_id=account.id, creator_id=account.id)
    await Ticket.create(repo_id=dru.id, title="open")
    await Ticket.create(repo_id=other.id, title="elsewhere")

    assert {ticket.title for ticket in await Ticket.list_matching(assignee="none")} == {
        "open",
        "elsewhere",
    }
    assert {ticket.title for ticket in await Ticket.list_matching(assignee=account.id)} == {"mine"}
    assert {ticket.title for ticket in await Ticket.list_matching(creator=account.id)} == {"mine"}
    assert {ticket.title for ticket in await Ticket.list_matching(repo_id=other.id)} == {
        "elsewhere"
    }
    assert {ticket.title for ticket in await Ticket.list_matching(project_id=dru.project_id)} == {
        "mine",
        "open",
    }
