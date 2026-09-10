from druks.apps import App


class Chat(App):
    name = "chat"
    icon = "message-square"
    description = "Operator conversations this appliance owns — several threads, one account each."
    # Every table this app owns carries the ``chat_`` prefix, so the thread's
    # schema can never collide with core's or another app's.
    prefix_tables = True
