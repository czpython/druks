from logging.config import fileConfig

import druks.accounts.models  # noqa: F401
import druks.build.models  # noqa: F401
import druks.durable.models  # noqa: F401
import druks.harnesses.models  # noqa: F401
import druks.mcp.models  # noqa: F401
import druks.notifications.models  # noqa: F401
import druks.skills.models  # noqa: F401
import druks.user_settings.models  # noqa: F401
from alembic import context
from druks.alembic_support import run_alembic_env

if context.config.config_file_name is not None:
    # In-process upgrades (init-db, the migration tests) must not mute the
    # app's already-created loggers.
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)

run_alembic_env()
