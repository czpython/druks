import re
from collections.abc import Awaitable, Callable
from inspect import Parameter, signature
from itertools import count

from druks.apps.registry import pages

from .exceptions import PageRouteError
from .schemas import Page

PageFunction = Callable[..., Awaitable[Page]]

# A route parameter — ``{note_id}``, or the catch-all ``{rest:path}``.
_PARAMETER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(:path)?\}")

# The order the app declared its pages in, which sets tab order. A parent's
# children all come from one ``pages.py``, and a module runs top to bottom, so
# the count follows the source.
_declared = count()

# The parameter kinds FastAPI can fill by name. A positional-only or variadic
# parameter would take the route value only by position, and the endpoint is
# called with keywords.
_CALLABLE_BY_NAME = frozenset({Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY})


class PageRoute:
    """One page an app declares: the route it answers, the function that
    projects it, and the parent it hangs under."""

    def __init__(
        self,
        path: str,
        function: PageFunction,
        *,
        label: str = "",
        parent: "PageRoute | None" = None,
    ) -> None:
        self.path = path
        self.function = function
        self.parent = parent
        self.name = function.__name__
        self.module = function.__module__
        self.label = label or self.name.replace("_", " ")
        self.order = next(_declared)

    def child(self, path: str, *, label: str = "") -> Callable[[PageFunction], "PageRoute"]:
        """Declare a page under this one, at ``path`` relative to this route."""
        if self.parent:
            raise PageRouteError(
                f"page {self.name!r} is already a child of {self.parent.name!r}, and one "
                f"child level is allowed. Declare {path!r} with @page instead."
            )

        def declare(function: PageFunction) -> PageRoute:
            return pages.register(PageRoute(path, function, label=label, parent=self))

        return declare

    @property
    def key(self) -> str:
        """What the registry files this page under. Module, name, and route
        together, so two pages that share a name both reach the boot check."""
        return f"{self.module}.{self.name}.{self.route}"

    @property
    def children(self) -> list["PageRoute"]:
        """The pages declared under this one, in the order the app declared them.
        The static ones are this page's tabs."""
        return sorted(
            (page_route for page_route in pages.all() if page_route.parent is self),
            key=lambda page_route: page_route.order,
        )

    @property
    def route(self) -> str:
        """The page's whole path — a child joins its parent's route."""
        if self.parent:
            return f"{self.parent.route.rstrip('/')}{self.path}"
        return self.path

    @property
    def is_static(self) -> bool:
        """Whether the page's own path segment carries no parameter. A static
        child renders as a tab; a parameterized one is a detail page a Link reaches."""
        return "{" not in self.path

    def check(self, app_name: str) -> None:
        """Everything this page decides on its own: its catch-all sits last, and
        it takes one name-callable parameter for each parameter of its route. A
        child inherits its parent's; an extra one comes from the child path."""
        if any(":path}" in segment for segment in self.route.split("/")[:-1]):
            raise PageRouteError(
                f"app {app_name!r} routes {self.name!r} at {self.route!r}, and its catch-all "
                "is not the last segment, so it would swallow every route under it. Put the "
                "catch-all last."
            )
        route_parameters = {name for name, _ in _PARAMETER.findall(self.route)}
        declared = signature(self.function).parameters
        by_name = {
            name for name, parameter in declared.items() if parameter.kind in _CALLABLE_BY_NAME
        }
        if set(declared) == route_parameters and by_name == route_parameters:
            return
        raise PageRouteError(
            f"app {app_name!r} page {self.name!r} takes {sorted(declared)}, and its route "
            f"{self.route!r} carries {sorted(route_parameters)}. Take one parameter for "
            "each route parameter, each one callable by name."
        )

    @property
    def match_key(self) -> tuple[tuple[int, str], ...]:
        """Sort key that puts literal segments before parameters and catch-alls
        at every depth, so a match never follows the order an app declared in."""
        key: list[tuple[int, str]] = []
        for segment in self.route.strip("/").split("/"):
            if "{" not in segment:
                key.append((0, segment))
            elif ":path}" in segment:
                key.append((2, ""))
            else:
                key.append((1, ""))
        return tuple(key)


def page(path: str, *, label: str = "") -> Callable[[PageFunction], PageRoute]:
    """Declare a top-level page at ``path``. The label defaults to the function
    name with its underscores as spaces."""

    def declare(function: PageFunction) -> PageRoute:
        return pages.register(PageRoute(path, function, label=label))

    return declare


def list_pages_for_app(app_name: str, package: str) -> list[PageRoute]:
    """The pages ``package`` declares, in route-match order: literal segments
    before parameters before catch-alls, at every depth. What an app declared
    first never decides a match. Raises ``PageRouteError`` on a table a request
    could not resolve."""
    declared = [
        page_route for page_route in pages.all() if page_route.module.startswith(f"{package}.")
    ]
    if not declared:
        return []

    landing = [page_route for page_route in declared if page_route.route == "/"]
    if len(landing) != 1:
        named = sorted(page_route.name for page_route in landing)
        raise PageRouteError(
            f"app {app_name!r} declares {len(landing)} pages at '/' ({named}). Declare "
            "exactly one: it is the page the app opens on."
        )

    by_name: dict[str, PageRoute] = {}
    by_shape: dict[str, PageRoute] = {}
    for page_route in declared:
        page_route.check(app_name)
        clash = by_name.get(page_route.name)
        if clash:
            raise PageRouteError(
                f"app {app_name!r} declares two pages named {page_route.name!r} "
                f"({clash.module} and {page_route.module}). A Link and a navigation "
                "entry address a page by name, so rename one."
            )
        by_name[page_route.name] = page_route
        # Drop each parameter's name but keep the catch-all marker: a catch-all
        # spans segments, so it is not the same shape as a plain parameter.
        shape = _PARAMETER.sub(r"{\2}", page_route.route)
        clash = by_shape.get(shape)
        if clash:
            raise PageRouteError(
                f"app {app_name!r} routes {clash.route!r} and {page_route.route!r} to the "
                "same shape, so no request could tell them apart. Give one a literal segment."
            )
        by_shape[shape] = page_route

    return sorted(declared, key=lambda page_route: page_route.match_key)
