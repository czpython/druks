from druks.exceptions import DruksError


class PageRouteError(DruksError):
    """An app's pages cannot make a route table. Raised at declaration for a
    nested child, and at boot for two landing pages, a repeated page name,
    two routes a request cannot tell apart, a signature that does not match its
    route, or a navigation entry that is not a static top-level page."""


class PageReadError(DruksError):
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


class NoTextFormError(DruksError):
    """A block that the operator acts through has no text form. Raised by its
    ``to_markdown()``, and by ``review`` for a context that holds one."""

    def __init__(self, block: str) -> None:
        super().__init__(
            f"{block} has no text form, so a review context cannot hold it. "
            f"Take the {block} out of the context, and show it on a page instead."
        )
        self.block = block
