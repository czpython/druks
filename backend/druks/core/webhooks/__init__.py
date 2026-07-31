# Imported so the discovery walk registers the platform webhook handlers:
# autodiscover skips packages, but walk_packages imports this __init__ to recurse.
from . import github, slack  # noqa: F401
