from druks import ui
from druks.accounts.models import Account
from druks.contrib.issues.enums import Priority, Status
from druks.contrib.issues.models import Comment, Project, Ticket

# The board's columns, worked-on left to right. Cancelled is not a column: a
# cancelled ticket is off the board, which is what ``Ticket.list_board`` reads.
BOARD_STATUSES = (
    Status.BACKLOG,
    Status.TODO,
    Status.READY_FOR_AGENT,
    Status.IN_PROGRESS,
    Status.IN_REVIEW,
    Status.DONE,
)
# The list's sections, worked-on first, with the finished ones at the bottom.
LIST_STATUSES = (
    Status.IN_PROGRESS,
    Status.IN_REVIEW,
    Status.READY_FOR_AGENT,
    Status.TODO,
    Status.BACKLOG,
    Status.DONE,
    Status.CANCELLED,
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
# How a status reads as a chip. Presentation only — the workflow is the enum.
STATUS_TONES: dict[Status, str] = {
    Status.BACKLOG: "neutral",
    Status.TODO: "neutral",
    Status.READY_FOR_AGENT: "warning",
    Status.IN_PROGRESS: "active",
    Status.IN_REVIEW: "active",
    Status.DONE: "success",
    Status.CANCELLED: "danger",
}

UNASSIGNED = "Unassigned"
# An account that has since gone, or druks' own system actor: the row still
# reads, it just carries no name.
UNATTRIBUTED = "Unattributed"


def _project_options(projects: list[Project]) -> list[ui.Option]:
    """Every namespace a ticket can be minted into. No blank entry: a ticket
    without a project could not be named."""
    return [ui.Option(project.name, value=str(project.id)) for project in projects]


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


def _new_ticket_action(projects: list[Project], accounts: list[Account]) -> ui.Action:
    """Creation is a control on the board, not a destination: a page that lists
    nothing is not where a ticket gets written."""
    return ui.Action(
        label="New ticket",
        operation="issues_create_ticket",
        tone="primary",
        fields=[
            ui.TextField(name="title", label="Title", is_required=True),
            ui.SelectField(
                name="project_id",
                label="Project",
                options=_project_options(projects),
                is_required=True,
                help_text="The namespace the identifier is minted from.",
            ),
            ui.TextAreaField(name="description", label="Description"),
            ui.SelectField(
                name="status",
                label="Status",
                options=_status_options(),
                value=Status.TODO.value,
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
    )


def _new_project_action() -> ui.Action:
    return ui.Action(
        label="New project",
        operation="issues_create_project",
        fields=[
            ui.TextField(name="name", label="Name", is_required=True),
            ui.TextField(
                name="prefix",
                label="Prefix",
                is_required=True,
                help_text="2-6 letters, A-Z — the first half of every identifier it mints.",
            ),
        ],
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
        controls=[
            ui.Link("Open", page="ticket", arguments={"identifier": ticket.identifier}),
        ],
    )


def _ticket_row(
    ticket: Ticket,
    project_names: dict[int, str],
    account_names: dict[str, str],
) -> ui.TableRow:
    return ui.TableRow(
        [
            ui.TextValue(
                ticket.identifier,
                link=ui.Link(
                    ticket.identifier,
                    page="ticket",
                    arguments={"identifier": ticket.identifier},
                ),
            ),
            ui.TextValue(ticket.title),
            ui.TextValue(PRIORITY_LABELS[Priority(ticket.priority)]),
            ui.TextValue(_assignee_name(ticket.assignee_id, account_names)),
            ui.TextValue(project_names.get(ticket.project_id, "")),
            ui.TimeValue(ticket.updated_at),
        ]
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


@ui.page("/")
async def board():
    tickets = await Ticket.list_board()
    projects = await Project.list()
    accounts = await Account.list_non_system()
    account_names = {account.id: account.username for account in accounts}
    return ui.Page(
        "Board",
        description="What this install is working on, a column to a status.",
        # Built from the projects and accounts alone, so an empty install still
        # offers both: the board is where a first ticket gets written.
        controls=[_new_ticket_action(projects, accounts), _new_project_action()],
        blocks=[
            ui.Columns(
                [
                    ui.Section(
                        title=status.label,
                        blocks=[
                            ui.Cards(
                                # One read of the board, grouped here: the
                                # column is the status, and the model already
                                # answered in updated_at order.
                                cards=[
                                    _ticket_card(ticket, account_names)
                                    for ticket in tickets
                                    if ticket.status == status
                                ],
                                empty=ui.EmptyState(
                                    "Nothing here",
                                    description=f"No ticket is in {status.label}.",
                                ),
                            )
                        ],
                    )
                    for status in BOARD_STATUSES
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

    projects = await Project.list()
    accounts = await Account.list_non_system()
    project_names = {project.id: project.name for project in projects}
    account_names = {account.id: account.username for account in accounts}
    status = Status(found.status)
    comments = await found.list_comments()
    thread = _comment_blocks(comments, account_names) or [
        ui.EmptyState("No comments yet", description="Say something about this ticket.")
    ]

    return ui.Page(
        found.title,
        description=found.identifier,
        # The whole page follows the ticket, so a status write from anywhere —
        # Software Factory included — redraws it without a navigation.
        follows=found,
        controls=[
            ui.Action(
                label="Move",
                operation="issues_set_status",
                arguments={"identifier": found.identifier},
                fields=[
                    ui.SelectField(
                        name="status",
                        label="Status",
                        options=_status_options(),
                        value=status.value,
                        is_required=True,
                    )
                ],
            )
        ],
        blocks=[
            ui.Markdown(found.description or "_No description._"),
            ui.Facts(
                [
                    ui.Fact(
                        "Status", value=ui.StatusValue(status.label, tone=STATUS_TONES[status])
                    ),
                    ui.Fact(
                        "Priority",
                        value=ui.TextValue(PRIORITY_LABELS[Priority(found.priority)]),
                    ),
                    ui.Fact(
                        "Assignee",
                        value=ui.TextValue(_assignee_name(found.assignee_id, account_names)),
                    ),
                    ui.Fact(
                        "Project",
                        value=ui.TextValue(project_names.get(found.project_id, "")),
                    ),
                    ui.Fact("Identifier", value=ui.TextValue(found.identifier)),
                ],
                title="Details",
            ),
            ui.Form(
                title="Edit",
                fields=[
                    ui.TextField(name="title", label="Title", value=found.title, is_required=True),
                    ui.TextAreaField(
                        name="description", label="Description", value=found.description
                    ),
                    ui.SelectField(
                        name="priority",
                        label="Priority",
                        options=_priority_options(),
                        value=found.priority,
                    ),
                    ui.SelectField(
                        name="assignee_id",
                        label="Assignee",
                        options=_assignee_options(accounts),
                        value=found.assignee_id or "",
                    ),
                    ui.SelectField(
                        name="project_id",
                        label="Project",
                        options=_project_options(projects),
                        value=str(found.project_id),
                    ),
                ],
                action=ui.Action(
                    label="Save",
                    operation="issues_update_ticket",
                    arguments={"identifier": found.identifier},
                ),
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
                        fields=[ui.TextAreaField(name="body", label="Comment", is_required=True)],
                        action=ui.Action(
                            label="Comment",
                            operation="issues_add_comment",
                            arguments={"identifier": found.identifier},
                            tone="primary",
                            refresh="region",
                        ),
                    ),
                ],
            ),
        ],
    )


# Declared last: the name is the page's name, and binding it shadows the
# builtin for the rest of the module.
@ui.page("/list")
async def list():
    projects = await Project.list()
    accounts = await Account.list_non_system()
    project_names = {project.id: project.name for project in projects}
    account_names = {account.id: account.username for account in accounts}
    sections = []
    for status in LIST_STATUSES:
        rows = await Ticket.list_for_status(status)
        sections.append(
            ui.Table(
                title=status.label,
                columns=[
                    ui.TableColumn("Identifier"),
                    ui.TableColumn("Title"),
                    ui.TableColumn("Priority"),
                    ui.TableColumn("Assignee"),
                    ui.TableColumn("Project"),
                    ui.TableColumn("Updated", align="end"),
                ],
                rows=[_ticket_row(row, project_names, account_names) for row in rows],
                empty_text=f"No ticket is in {status.label}.",
            )
        )
    return ui.Page(
        "List",
        description="Every ticket, worked-on first and cancelled last.",
        blocks=[ui.Stack(sections)],
    )
