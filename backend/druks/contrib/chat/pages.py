from druks import ui
from druks.accounts import current_account_id
from druks.contrib.chat.enums import Autonomy, Role
from druks.contrib.chat.models import Conversation


@ui.page("/", label="Conversations")
async def list():
    threads = await Conversation.list_for_account(current_account_id.get())
    return ui.Page(
        "Conversations",
        controls=[ui.Link("New", page="new")],
        blocks=[
            ui.Cards(
                title="Threads",
                cards=[
                    ui.Card(
                        title=thread.title or thread.label,
                        controls=[
                            ui.Link(
                                "Open",
                                page="thread",
                                arguments={"conversation_id": str(thread.id)},
                            )
                        ],
                    )
                    for thread in threads
                ],
                empty=ui.EmptyState(
                    "No conversations yet",
                    description="Start one with a first message.",
                    controls=[ui.Link("New", page="new")],
                ),
            )
        ],
    )


@ui.page("/new")
async def new():
    return ui.Page(
        "New conversation",
        blocks=[
            ui.Form(
                title="New conversation",
                description="Title is optional. The first message starts Talk.",
                fields=[
                    ui.TextField(name="title", label="Title"),
                    ui.TextAreaField(
                        name="body",
                        label="Message",
                        is_required=True,
                        rows=4,
                    ),
                ],
                action=ui.Action(
                    label="Start",
                    operation="create_conversation",
                    tone="primary",
                    link=ui.Link("Conversations", page="list"),
                ),
            )
        ],
    )


@ui.page("/conversations/{conversation_id}")
async def thread(conversation_id: int):
    conversation = await Conversation.get_for_account(conversation_id, current_account_id.get())
    if conversation:
        status = await conversation.get_status()
        if status.is_parked:
            turn = [ui.GateControls(status.run)]
        elif status.is_running:
            turn = [ui.Text(status.agent or "The agent is running.")]
        else:
            turn = []
        cards = []
        for message in await conversation.list_messages():
            if message.role == Role.USER:
                speaker = "You"
            elif message.role == Role.ASSISTANT:
                speaker = "Assistant"
            else:
                speaker = "System"
            cards.append(ui.Card(title=speaker, blocks=[ui.Quote(message.body)]))
        return ui.Page(
            conversation.title or conversation.label,
            controls=[
                ui.Link(
                    "Settings",
                    page="settings",
                    arguments={"conversation_id": str(conversation.id)},
                ),
                ui.Link("This conversation", subject=conversation),
            ],
            blocks=[
                ui.Section(
                    name="thread",
                    follows=conversation,
                    blocks=[
                        ui.Cards(
                            cards=cards,
                            empty=ui.EmptyState("No messages yet"),
                        ),
                        *turn,
                    ],
                )
            ],
        )
    return ui.Page(
        f"Conversation {conversation_id}",
        blocks=[ui.EmptyState("No such conversation")],
    )


@thread.child("/settings")
async def settings(conversation_id: int):
    conversation = await Conversation.get_for_account(conversation_id, current_account_id.get())
    if conversation:
        return ui.Page(
            "Settings",
            blocks=[
                ui.Form(
                    title="Autonomy",
                    fields=[
                        ui.SelectField(
                            name="autonomy",
                            label="Autonomy",
                            value=conversation.autonomy,
                            options=[ui.Option(mode.capitalize(), value=mode) for mode in Autonomy],
                            is_required=True,
                        )
                    ],
                    action=ui.Action(
                        label="Save",
                        operation="set_autonomy",
                        arguments={"conversation_id": conversation.id},
                    ),
                )
            ],
        )
    return ui.Page("Settings", blocks=[ui.EmptyState("No such conversation")])
