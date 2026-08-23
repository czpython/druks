from druks.apps import App


class Core(App):
    """The platform's home app: the webhook providers and the platform
    chores (token refresh, stale-call and sandbox-host reaping) — the package
    walk discovers them like any app's capabilities."""

    name = "core"
    icon = "hexagon"
    description = "The platform's own capabilities — chores and webhooks."
    builtin = True
