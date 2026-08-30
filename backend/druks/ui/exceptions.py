class PageRouteError(Exception):
    """An app's pages cannot make a route table. Raised at declaration for a
    nested child, and at boot for a missing landing page, a repeated page name,
    two routes a request cannot tell apart, a signature that does not match its
    route, or a navigation entry that is not a static top-level page."""
