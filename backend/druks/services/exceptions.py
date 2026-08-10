class ServiceNotConnectedError(Exception):
    """No identity is connected for this service — the appliance has nothing to
    act as there. The message names the service so any refusal is actionable."""

    def __init__(self, service: str) -> None:
        self.service = service
        super().__init__(f"{service} is not connected — connect it in Settings → Services.")


class ServiceConnectError(Exception):
    """A rejected connect. The message is authored by the service's ``verify``
    and safe to show; it never quotes anything the operator pasted."""
