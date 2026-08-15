class BrowserSessionUnknownError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"Browser session {name!r} does not exist.")


class BrowserSessionNotReadyError(Exception):
    def __init__(self, name: str, status: str) -> None:
        super().__init__(f"Browser session {name!r} is {status}; log in before borrowing it.")


class BrowserLaunchError(Exception):
    def __init__(self, name: str, detail: str) -> None:
        super().__init__(f"Browser session {name!r} did not open: {detail}")


class BrowserExportError(Exception):
    def __init__(self, name: str, detail: str) -> None:
        super().__init__(f"Browser session {name!r} export failed: {detail}")


class BrowserSessionWriterLockedError(Exception):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Browser session {session_id!r} already has a persisting writer.")


class BrowserClientMissingError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"{name}.browser() drives the session with playwright, which the extension "
            "supplies — add playwright to its dependencies, or use .cdp() with another client."
        )
