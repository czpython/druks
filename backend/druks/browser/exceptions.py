from typing import ClassVar

from druks.browser.constants import SESSION_SIGNED_OUT_SIGNAL
from druks.durable.exceptions import FatalError


class BrowserApiError(Exception):
    # Raised from a browser route; the app maps it to this status with its
    # message as the detail, so routes stay a single line and never hand-map.
    status_code: ClassVar[int] = 500


class BrowserSessionUnknownError(BrowserApiError):
    status_code = 404

    def __init__(self, name: str) -> None:
        super().__init__(f"Browser session {name!r} does not exist.")


class BrowserSessionNotReadyError(Exception):
    def __init__(self, name: str, status: str) -> None:
        super().__init__(f"Browser session {name!r} is {status}; log in before borrowing it.")


class BrowserSessionSignedOutError(FatalError):
    # Raised by app code inside a borrow when the site bounced the login.
    # The door stamps which session; the run fails under this code and announces
    # the bounce, and a subscriber marks the session stale. Nothing to catch.
    code = "browser_session_signed_out"
    broadcast_topic = SESSION_SIGNED_OUT_SIGNAL
    session_name: str = ""

    @property
    def broadcast_facts(self) -> dict[str, str]:
        return {"session_name": self.session_name}


class BrowserLaunchError(BrowserApiError):
    status_code = 502

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(f"Browser session {name!r} did not open: {detail}")


class BrowserExportError(BrowserApiError):
    status_code = 502

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(f"Browser session {name!r} export failed: {detail}")


class BrowserSessionWriterLockedError(Exception):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Browser session {session_id!r} already has a persisting writer.")


class BrowserClientMissingError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"{name}.browser() drives the session with playwright, which the app "
            "supplies — add playwright to its dependencies, or use .cdp() with another client."
        )


class BrowserLoginWindowGoneError(BrowserApiError):
    status_code = 410

    def __init__(self) -> None:
        super().__init__("This login window is no longer open.")


class BrowserVncError(Exception):
    # A malformed VNC handshake on the bridge — the socket closes, no HTTP body.
    ...
