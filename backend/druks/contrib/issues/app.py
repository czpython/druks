from druks.apps import App


class Issues(App):
    name = "issues"
    icon = "layers"
    description = "A local issue board — projects, tickets, and comments the appliance owns."
    # Every table this app owns carries the ``issues_`` prefix, so the board's
    # schema can never collide with core's or another app's.
    prefix_tables = True
