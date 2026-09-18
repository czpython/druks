from druks.apps import App


class Chat(App):
    """Live agent sessions per account, and the channels people reach them through.
    The package walk discovers each channel's services, webhooks, and routes."""

    name = "chat"
    icon = "messages-square"
    description = "Live agent conversations."
    builtin = True
