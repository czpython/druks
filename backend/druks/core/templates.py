from pathlib import Path
from typing import Any

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined

_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
    undefined=StrictUndefined,
)


def render_page(template: str, *, status_code: int = 200, **context: Any) -> HTMLResponse:
    """An operator-facing page from ``core/templates`` — a server-rendered
    browser stop that shares the dashboard's chrome."""
    return HTMLResponse(_templates.get_template(template).render(context), status_code=status_code)
