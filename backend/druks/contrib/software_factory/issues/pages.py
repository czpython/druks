from datetime import timedelta

from druks import ui
from druks.accounts.models import Account
from druks.contrib.software_factory.issues.enums import Priority, Status
from druks.contrib.software_factory.issues.models import Comment, Ticket
from druks.contrib.software_factory.models import Project, ProjectRepo, WorkItem
from druks.db import Base

# The board's columns, worked-on left to right. Cancelled and blocked are off
# it: ``Ticket.list_board`` leaves those rows out.
BOARD_STATUSES = (
    Status.BACKLOG,
    Status.READY_FOR_AGENT,
    Status.IN_PROGRESS,
    Status.IN_REVIEW,
    Status.DONE,
)

# The words the screens spell a priority with. The stored value stays
# snake_case; only these strings change when the board wants different words.
PRIORITY_LABELS: dict[Priority, str] = {
    Priority.NONE: "No priority",
    Priority.URGENT: "Urgent",
    Priority.HIGH: "High",
    Priority.MEDIUM: "Medium",
    Priority.LOW: "Low",
}

UNASSIGNED = "Unassigned"
# An account that has since gone, or druks' own system actor: the row still
# reads, it just carries no name.
UNATTRIBUTED = "Unattributed"
# Empty value on a page filter: any ticket. Assignee uses ``none`` for
# unassigned because this empty value already means "no filter".
FILTER_ANY = "Any"
UNASSIGNED_FILTER = "none"
UPDATED_WINDOWS = ("today", "week", "month")


def _repo_options(repos: list[ProjectRepo]) -> list[ui.Option]:
    """Every registered repo whose project can mint an identifier. Grouped by
    GitHub project so the operator picks a repo, not a second project table."""
    return [
        ui.Option(repo.full_name, value=str(repo.id), group=repo.project.name) for repo in repos
    ]


def _assignee_options(accounts: list[Account]) -> list[ui.Option]:
    """Who work can be handed to, plus nobody. Unassigned carries the empty
    value the doors read back as "no assignee"."""
    return [ui.Option(UNASSIGNED, value="")] + [
        ui.Option(account.username, value=account.id) for account in accounts
    ]


def _priority_options() -> list[ui.Option]:
    return [ui.Option(label, value=priority.value) for priority, label in PRIORITY_LABELS.items()]


def _status_options() -> list[ui.Option]:
    return [ui.Option(status.label, value=status.value) for status in Status]


def _assignee_name(assignee_id: str | None, account_names: dict[str, str]) -> str:
    if not assignee_id:
        return UNASSIGNED
    return account_names.get(assignee_id, UNATTRIBUTED)


def _creator_name(creator_id: str | None, account_names: dict[str, str]) -> str:
    if not creator_id:
        return UNATTRIBUTED
    return account_names.get(creator_id, UNATTRIBUTED)


def _create_actions(repos: list[ProjectRepo], accounts: list[Account]) -> list[ui.Action]:
    """Creation is a control on the board, not a destination: a page that lists
    nothing is not where a ticket gets written."""
    repo_choices = _repo_options(repos)
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
                    options=repo_choices,
                    # An empty value still paints the first option in the browser.
                    # The door takes an int, so the field has to start on a real id.
                    value=repo_choices[0].value if repo_choices else "",
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
                    name="assignee_id",
                    label="Assignee",
                    options=_assignee_options(accounts),
                ),
            ],
        ),
    ]


def _ticket_link(ticket: Ticket, label: str) -> ui.Link:
    return ui.Link(label, page="ticket", arguments={"identifier": ticket.identifier})


def _live_form(
    ticket: Ticket,
    field: ui.Field,
    *,
    operation: str,
    layout: str,
    refresh: str,
) -> ui.Form:
    return ui.Form(
        fields=[field],
        action=ui.Action(
            label=f"Save {field.label.lower()}",
            operation=operation,
            arguments={"identifier": ticket.identifier},
            refresh=refresh,
        ),
        submit="change",
        layout=layout,
    )


def _ticket_card(ticket: Ticket, account_names: dict[str, str]) -> ui.Card:
    description = [ticket.identifier]
    priority = Priority(ticket.priority)
    if priority is not Priority.NONE:
        description.append(PRIORITY_LABELS[priority])
    if ticket.assignee_id:
        description.append(_assignee_name(ticket.assignee_id, account_names))
    return ui.Card(
        title=ticket.title,
        description=" · ".join(description),
        link=_ticket_link(ticket, ticket.title),
        drag={"identifier": ticket.identifier},
    )


def _comment_blocks(comments: list[Comment], account_names: dict[str, str]) -> list[ui.Card]:
    return [
        ui.Card(
            title=account_names.get(comment.author_id, UNATTRIBUTED),
            description=comment.created_at.isoformat(sep=" ", timespec="minutes"),
            blocks=[ui.Markdown(comment.body)],
        )
        for comment in comments
    ]


def _optional_int(raw: str) -> int | None:
    return int(raw) if raw else None


def _choice(raw: str, allowed: set[str], name: str) -> str:
    if not raw:
        return ""
    if raw not in allowed:
        raise ValueError(f"{name} filter {raw!r} is not one of {sorted(allowed)}")
    return raw


def _filter_select(name: str, label: str, options: list[ui.Option], value: str) -> ui.SelectField:
    return ui.SelectField(
        name=name,
        label=label,
        options=[ui.Option(FILTER_ANY, value=""), *options],
        value=value,
    )


def _ticket_filters(
    *,
    status: str,
    priority: str,
    updated: str,
    assignee: str,
    creator: str,
    project: str,
    repo: str,
    projects: list[Project],
    repos: list[ProjectRepo],
    accounts: list[Account],
) -> list[ui.SelectField]:
    repo_choices = [item for item in repos if not project or str(item.project_id) == project]
    return [
        _filter_select(
            "status",
            "Status",
            [ui.Option(item.label, value=item.value) for item in Status],
            status,
        ),
        _filter_select(
            "priority",
            "Priority",
            [ui.Option(label, value=item.value) for item, label in PRIORITY_LABELS.items()],
            priority,
        ),
        ui.SelectField(
            name="updated",
            label="Updated",
            options=[
                ui.Option(FILTER_ANY, value=""),
                ui.Option("Today", value="today"),
                ui.Option("Past week", value="week"),
                ui.Option("Past month", value="month"),
            ],
            value=updated,
        ),
        ui.SelectField(
            name="assignee",
            label="Assignee",
            options=[
                ui.Option(FILTER_ANY, value=""),
                ui.Option(UNASSIGNED, value=UNASSIGNED_FILTER),
                *[ui.Option(account.username, value=account.id) for account in accounts],
            ],
            value=assignee,
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
            project,
        ),
        _filter_select("repo", "Repo", _repo_options(repo_choices), repo),
    ]


async def _ticket_collection(
    *,
    exclude_cancelled: bool,
    status: str = "",
    priority: str = "",
    updated: str = "",
    assignee: str = "",
    creator: str = "",
    project: str = "",
    repo: str = "",
):
    status = _choice(status, {item.value for item in Status}, "status")
    priority = _choice(priority, {item.value for item in Priority}, "priority")
    if updated and updated not in UPDATED_WINDOWS:
        raise ValueError(f"updated filter {updated!r} is not today, week, or month")
    updated_since = None
    if updated:
        now = Base.utc_now()
        if updated == "today":
            updated_since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif updated == "week":
            updated_since = now - timedelta(days=7)
        else:
            updated_since = now - timedelta(days=30)
    projects = await Project.list()
    repos = await ProjectRepo.list_for_tickets()
    accounts = await Account.list_all()
    tickets = await Ticket.list_matching(
        exclude_cancelled=exclude_cancelled and not status,
        status=status,
        priority=priority,
        assignee=assignee,
        creator=creator,
        project_id=_optional_int(project),
        repo_id=_optional_int(repo),
        updated_since=updated_since,
    )
    return (
        tickets,
        repos,
        accounts,
        _ticket_filters(
            status=status,
            priority=priority,
            updated=updated,
            assignee=assignee,
            creator=creator,
            project=project,
            repo=repo,
            projects=projects,
            repos=repos,
            accounts=accounts,
        ),
    )


@ui.page("/board")
async def board(
    status: str = "",
    priority: str = "",
    updated: str = "",
    assignee: str = "",
    creator: str = "",
    project: str = "",
    repo: str = "",
):
    tickets, repos, accounts, filters = await _ticket_collection(
        exclude_cancelled=True,
        status=status,
        priority=priority,
        updated=updated,
        assignee=assignee,
        creator=creator,
        project=project,
        repo=repo,
    )
    account_names = {account.id: account.username for account in accounts}
    columns = BOARD_STATUSES
    if status in {Status.CANCELLED, Status.BLOCKED}:
        columns = BOARD_STATUSES + (Status(status),)
    return ui.Page(
        "Board",
        # Built from the repos and accounts alone, so an empty install still
        # offers create: the board is where a first ticket gets written.
        controls=_create_actions(repos, accounts),
        filters=filters,
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
                    for item in columns
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

    repos = await ProjectRepo.list_for_tickets()
    accounts = await Account.list_all()
    account_names = {account.id: account.username for account in accounts}
    comments = await found.list_comments()
    thread = _comment_blocks(comments, account_names) or [
        ui.EmptyState("No comments yet", description="Say something about this ticket.")
    ]
    build = await WorkItem.get_for_ticket_key(source="issues", ticket_key=found.identifier)

    return ui.Page(
        found.identifier,
        # The whole page follows the ticket, so a status write from anywhere —
        # Software Factory included — redraws it without a navigation.
        follows=found,
        controls=(
            [ui.Link("Open build", url=f"/software_factory/work-items/{build.id}")] if build else []
        ),
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
                                # Named, so the comment below replaces this section alone and
                                # the thread grows in place.
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
                                layout="row",
                                refresh="page",
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
                                layout="row",
                                refresh="page",
                            ),
                            _live_form(
                                found,
                                ui.SelectField(
                                    name="assignee_id",
                                    label="Assignee",
                                    options=_assignee_options(accounts),
                                    value=found.assignee_id or "",
                                ),
                                operation="update_ticket",
                                layout="row",
                                refresh="page",
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
                                layout="row",
                                refresh="page",
                            ),
                            ui.Facts(
                                [
                                    ui.Fact("Identifier", value=ui.TextValue(found.identifier)),
                                    ui.Fact(
                                        "Created by",
                                        value=ui.TextValue(
                                            _creator_name(found.creator_id, account_names)
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
