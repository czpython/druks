from datetime import timedelta
from typing import Literal

from druks import ui
from druks.accounts.context import current_account_id
from druks.accounts.models import Account
from druks.contrib.software_factory.enums import Priority, Status
from druks.contrib.software_factory.models import Project, ProjectRepo, Ticket, WorkItem
from druks.db import Base

PRIORITY_LABELS: dict[Priority, str] = {
    Priority.NONE: "No priority",
    Priority.URGENT: "Urgent",
    Priority.HIGH: "High",
    Priority.MEDIUM: "Medium",
    Priority.LOW: "Low",
}

UNOWNED = "Unowned"
# An account that is gone, or Druks itself. The row still reads.
UNATTRIBUTED = "Unattributed"
FILTER_ANY = "Any"
# The owner filter's value for unowned. Empty already means any.
UNOWNED_FILTER = "none"


def _repo_options(repos: list[ProjectRepo]) -> list[ui.Option]:
    return [
        ui.Option(repo.full_name, value=str(repo.id), group=repo.project.name) for repo in repos
    ]


def _owner_options(accounts: list[Account]) -> list[ui.Option]:
    return [ui.Option(UNOWNED, value="")] + [
        ui.Option(account.username, value=account.id) for account in accounts
    ]


def _priority_options() -> list[ui.Option]:
    return [ui.Option(label, value=priority.value) for priority, label in PRIORITY_LABELS.items()]


def _status_options() -> list[ui.Option]:
    return [ui.Option(status.label, value=status.value) for status in Status]


def _filter_select(name: str, label: str, options: list[ui.Option], value: str) -> ui.SelectField:
    return ui.SelectField(
        name=name,
        label=label,
        options=[ui.Option(FILTER_ANY, value=""), *options],
        value=value,
    )


def _create_actions(repos: list[ProjectRepo], accounts: list[Account]) -> list[ui.Action]:
    return [
        ui.Action(
            label="New ticket",
            operation="create_ticket",
            tone="primary",
            fields=[
                ui.TextField(name="title", label="Title", is_required=True),
                ui.SelectField(
                    name="repo_id",
                    label="Repo",
                    options=_repo_options(repos),
                    is_required=True,
                    help_text="The GitHub repo this ticket's pull request will target.",
                ),
                ui.TextAreaField(name="description", label="Description", markdown=True, rows=3),
                ui.SelectField(
                    name="status",
                    label="Status",
                    options=_status_options(),
                    value=Status.BACKLOG.value,
                ),
                ui.SelectField(
                    name="priority",
                    label="Priority",
                    options=_priority_options(),
                    value=Priority.NONE.value,
                ),
                ui.SelectField(
                    name="owner_id",
                    label="Owner",
                    options=_owner_options(accounts),
                    value=current_account_id.get() or "",
                ),
            ],
        ),
    ]


def _live_form(ticket: Ticket, field: ui.Field, *, operation: str) -> ui.Form:
    return ui.Form(
        fields=[field],
        action=ui.Action(
            label=f"Save {field.label.lower()}",
            operation=operation,
            arguments={"identifier": ticket.identifier},
            refresh="page",
        ),
        submit="change",
        layout="row",
    )


def _ticket_card(ticket: Ticket, account_names: dict[str, str]) -> ui.Card:
    description = [ticket.identifier]
    priority = Priority(ticket.priority)
    if priority is not Priority.NONE:
        description.append(PRIORITY_LABELS[priority])
    if ticket.owner_id:
        description.append(account_names.get(ticket.owner_id, UNATTRIBUTED))
    return ui.Card(
        title=ticket.title,
        description=" · ".join(description),
        link=ui.Link(ticket.title, page="ticket", arguments={"identifier": ticket.identifier}),
        drag={"identifier": ticket.identifier},
    )


@ui.page("/board")
async def board(
    status: Status | None = None,
    priority: Priority | None = None,
    updated: Literal["today", "week", "month"] | None = None,
    owner: str = "",
    creator: str = "",
    project: int | None = None,
    repo: int | None = None,
):
    updated_since = None
    if updated == "today":
        updated_since = Base.utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    elif updated == "week":
        updated_since = Base.utc_now() - timedelta(days=7)
    elif updated == "month":
        updated_since = Base.utc_now() - timedelta(days=30)
    projects = await Project.list()
    repos = await ProjectRepo.list_all()
    accounts = await Account.list_all()
    account_names = {account.id: account.username for account in accounts}
    tickets = await Ticket.list_matching(
        status=status,
        priority=priority,
        owner=owner,
        creator=creator,
        project_id=project,
        repo_id=repo,
        updated_since=updated_since,
    )
    repo_choices = [item for item in repos if not project or item.project_id == project]
    return ui.Page(
        "Board",
        controls=_create_actions(repos, accounts),
        filters=[
            _filter_select("status", "Status", _status_options(), status or ""),
            _filter_select("priority", "Priority", _priority_options(), priority or ""),
            _filter_select(
                "updated",
                "Updated",
                [
                    ui.Option("Today", value="today"),
                    ui.Option("Past week", value="week"),
                    ui.Option("Past month", value="month"),
                ],
                updated or "",
            ),
            ui.SelectField(
                name="owner",
                label="Owner",
                options=[
                    ui.Option(FILTER_ANY, value=""),
                    ui.Option(UNOWNED, value=UNOWNED_FILTER),
                    *[ui.Option(account.username, value=account.id) for account in accounts],
                ],
                value=owner,
            ),
            _filter_select(
                "creator",
                "Creator",
                [ui.Option(account.username, value=account.id) for account in accounts],
                creator,
            ),
            _filter_select(
                "project",
                "Project",
                [ui.Option(item.name, value=str(item.id)) for item in projects],
                str(project or ""),
            ),
            _filter_select("repo", "Repo", _repo_options(repo_choices), str(repo or "")),
        ],
        blocks=[
            ui.Columns(
                [
                    ui.Section(
                        title=item.label,
                        blocks=[
                            ui.Cards(
                                layout="stack",
                                drop=ui.Action(
                                    label=f"Move to {item.label}",
                                    operation="set_status",
                                    arguments={"status": item.value},
                                    refresh="page",
                                ),
                                cards=[
                                    _ticket_card(ticket, account_names)
                                    for ticket in tickets
                                    if ticket.status == item
                                ],
                                empty=ui.EmptyState(
                                    "Nothing here",
                                    description=f"No ticket is in {item.label}.",
                                ),
                            )
                        ],
                    )
                    for item in Status
                ]
            )
        ],
    )


@ui.page("/tickets/{identifier}")
async def ticket(identifier: str):
    found = await Ticket.get_for_identifier(identifier)
    if not found:
        return ui.Page(
            identifier,
            blocks=[
                ui.EmptyState(
                    "No such ticket",
                    description=f"Nothing on this board is named {identifier}.",
                    controls=[ui.Link("Board", page="board")],
                )
            ],
        )

    repos = await ProjectRepo.list_all()
    accounts = await Account.list_all()
    account_names = {account.id: account.username for account in accounts}
    thread = [
        ui.Card(
            title=comment.author.username,
            description=comment.created_at.isoformat(sep=" ", timespec="minutes"),
            blocks=[ui.Markdown(comment.body)],
        )
        for comment in found.comments
    ] or [ui.EmptyState("No comments yet", description="Say something about this ticket.")]
    build = await WorkItem.get_for_ticket_key(source="druks", ticket_key=found.identifier)
    controls = [ui.Link("Open build", subject=build)] if build else []

    return ui.Page(
        found.identifier,
        controls=controls,
        blocks=[
            ui.Columns(
                [
                    ui.Stack(
                        [
                            ui.Form(
                                fields=[
                                    ui.TextField(
                                        name="title",
                                        label="Title",
                                        value=found.title,
                                        is_required=True,
                                        placeholder="Title",
                                    ),
                                    ui.TextAreaField(
                                        name="description",
                                        label="Description",
                                        value=found.description,
                                        placeholder="Add a description…",
                                        rows=12,
                                        markdown=True,
                                    ),
                                ],
                                action=ui.Action(
                                    label="Save",
                                    operation="update_ticket",
                                    arguments={"identifier": found.identifier},
                                    refresh="none",
                                ),
                                submit="change",
                                layout="prose",
                            ),
                            ui.Section(
                                title="Comments",
                                # Named, so a new comment replaces this section alone.
                                name="comments",
                                blocks=[
                                    *thread,
                                    ui.Form(
                                        title="Add a comment",
                                        fields=[
                                            ui.TextAreaField(
                                                name="body",
                                                label="Comment",
                                                is_required=True,
                                                markdown=True,
                                                rows=3,
                                            )
                                        ],
                                        action=ui.Action(
                                            label="Comment",
                                            operation="add_comment",
                                            arguments={"identifier": found.identifier},
                                            tone="primary",
                                            refresh="region",
                                        ),
                                    ),
                                ],
                            ),
                        ],
                        gap="large",
                    ),
                    ui.Stack(
                        [
                            _live_form(
                                found,
                                ui.SelectField(
                                    name="status",
                                    label="Status",
                                    options=_status_options(),
                                    value=found.status,
                                    is_required=True,
                                ),
                                operation="set_status",
                            ),
                            _live_form(
                                found,
                                ui.SelectField(
                                    name="priority",
                                    label="Priority",
                                    options=_priority_options(),
                                    value=found.priority,
                                ),
                                operation="update_ticket",
                            ),
                            _live_form(
                                found,
                                ui.SelectField(
                                    name="owner_id",
                                    label="Owner",
                                    options=_owner_options(accounts),
                                    value=found.owner_id or "",
                                ),
                                operation="update_ticket",
                            ),
                            _live_form(
                                found,
                                ui.SelectField(
                                    name="repo_id",
                                    label="Repo",
                                    options=_repo_options(repos),
                                    value=str(found.repo_id),
                                ),
                                operation="update_ticket",
                            ),
                            ui.Facts(
                                [
                                    ui.Fact("Identifier", value=ui.TextValue(found.identifier)),
                                    ui.Fact(
                                        "Created by",
                                        value=ui.TextValue(
                                            account_names.get(found.creator_id, UNATTRIBUTED)
                                        ),
                                    ),
                                    ui.Fact("Created", value=ui.TimeValue(found.created_at)),
                                    ui.Fact("Updated", value=ui.TimeValue(found.updated_at)),
                                ]
                            ),
                        ],
                        gap="small",
                    ),
                ],
                layout="sidebar",
            )
        ],
    )
