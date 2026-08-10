from pathlib import Path
from typing import Any

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined

_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
    undefined=StrictUndefined,
)


def render_page(template: str, **context: Any) -> HTMLResponse:
    """One of the operator-facing pages in ``core/templates`` — server-rendered
    browser stops (connect callbacks, the GitHub App manifest form) that share
    the dashboard's chrome."""
    return HTMLResponse(_templates.get_template(template).render(context))
