class PageRouteError(Exception):
    """An app's pages cannot make a route table. Raised at declaration for a
    nested child, and at boot for a missing landing page, a repeated page name,
    two routes a request cannot tell apart, a signature that does not match its
    route, or a navigation entry that is not a static top-level page."""


class PageReadError(Exception):
    """A page could not be read. The message names the app and the page;
    whatever the app's own code said stays in the process log, because it can
    carry a query, a URL, or a credential."""

    def __init__(self, app: str, page: str, detail: str) -> None:
        super().__init__(f"app {app!r} page {page!r} could not be read: {detail}")
        self.app = app
        self.page = page


class PageContractError(PageReadError):
    """The page a function returned breaks the contract. Druks wrote this
    message, so the dashboard shows it whole."""
