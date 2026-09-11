import pytest
from druks.accounts.models import Account
from druks.contrib.software_factory.exceptions import PrefixTakenError
from druks.contrib.software_factory.issues.enums import Status
from druks.contrib.software_factory.models import Project, ProjectRepo, Ticket, derive_prefix


async def _open_repo(*, name="Druks", full_name="acme/druks"):
    project = await Project.create(name=name)
    return await ProjectRepo.create(project_id=project.id, full_name=full_name)


async def test_ticket_identifiers_count_up_per_project():
    druks = await _open_repo(name="Druks", full_name="acme/druks")
    engine = await _open_repo(name="Engine", full_name="acme/engine")

    identifiers = [
        (await Ticket.create(repo=druks, title="one")).identifier,
        (await Ticket.create(repo=druks, title="two")).identifier,
        (await Ticket.create(repo=engine, title="eng-first")).identifier,
    ]

    assert identifiers == ["DRU-1", "DRU-2", "ENG-1"]


def test_derive_prefix_takes_the_first_unused_third_letter():
    assert derive_prefix("Acme", set()) == "ACM"
    assert derive_prefix("Acme Tools", {"ACM"}) == "ACE"
    assert derive_prefix("Go", set()) == "GO"
    with pytest.raises(PrefixTakenError):
        derive_prefix("Go", {"GO"})
    with pytest.raises(PrefixTakenError):
        derive_prefix("A", set())


async def test_create_derives_an_unused_prefix():
    first = await Project.create(name="Acme")
    second = await Project.create(name="Acme Tools")

    assert (first.prefix, second.prefix) == ("ACM", "ACE")


async def test_comments_read_back_oldest_first_with_their_author():
    account = await Account.get_or_create("op@example.com")
    ticket = await Ticket.create(repo=await _open_repo(), title="quiet")

    await ticket.add_comment(author=account, body="first")
    await ticket.add_comment(author=account, body="second")

    found = await Ticket.get_for_identifier(ticket.identifier)
    assert [(comment.author.username, comment.body) for comment in found.comments] == [
        ("op@example.com", "first"),
        ("op@example.com", "second"),
    ]


async def test_list_matching_filters_by_status_owner_creator_repo_and_project():
    account = await Account.get_or_create("op@example.com")
    here = await _open_repo(name="Filter", full_name="acme/filter")
    other = await _open_repo(name="Other", full_name="acme/other")
    mine = await Ticket.create(repo=here, title="mine", owner_id=account.id, creator_id=account.id)
    await Ticket.create(repo=here, title="open")
    await Ticket.create(repo=other, title="elsewhere")
    await mine.transition(Status.BLOCKED)

    async def titles(**filters):
        return {ticket.title for ticket in await Ticket.list_matching(**filters)}

    assert await titles(status=Status.BLOCKED) == {"mine"}
    assert await titles(owner="none") == {"open", "elsewhere"}
    assert await titles(owner=account.id) == {"mine"}
    assert await titles(creator=account.id) == {"mine"}
    assert await titles(repo_id=other.id) == {"elsewhere"}
    assert await titles(project_id=here.project_id) == {"mine", "open"}
