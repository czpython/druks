from alembic import context
from sqlalchemy import engine_from_config, pool

from druks.models import Base
from druks.secrets.fields import _EncryptedColumn
from druks.settings import load_settings


def _render_item(type_, obj, autogen_context):
    # Render app TypeDecorators as their plain DDL type so migrations never
    # import extension code. _UtcDateTime only changes read-side tz coercion
    # (DDL is TIMESTAMPTZ); the encrypted columns store as LargeBinary (bytea).
    if type_ == "type" and obj.__class__.__name__ == "_UtcDateTime":
        return "sa.DateTime(timezone=True)"
    if type_ == "type" and isinstance(obj, _EncryptedColumn):
        return "sa.LargeBinary()"
    return False


def run_alembic_env(target_metadata=None) -> None:
    """Run the platform Alembic env against the target metadata. Core and every
    installed extension share one ``env.py``; the runner selects the scope by setting
    ``target_metadata`` and ``version_table`` in ``config.attributes``. Metadata
    falls back to the full ``Base.metadata`` (core's own scope, e.g. core's raw
    autogenerate). ``include_object`` keeps autogenerate within the scope: a table
    reflected from the DB but absent from the metadata belongs to another package,
    so it's never proposed for a drop."""
    config = context.config
    if target_metadata is None:
        target_metadata = config.attributes.get("target_metadata", Base.metadata)
    if not config.get_main_option("sqlalchemy.url"):
        config.set_main_option("sqlalchemy.url", load_settings().database_url)

    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table" and reflected and compare_to is None:
            return name in target_metadata.tables
        return True

    options = {
        "target_metadata": target_metadata,
        "render_item": _render_item,
        "include_object": include_object,
        # Each migration history tracks its head in its own version table, so an
        # installed extension's independent history never reads (and chokes on) core's
        # revision in the shared default ``alembic_version``.
        "version_table": config.attributes.get("version_table", "alembic_version"),
    }
    if context.is_offline_mode():
        context.configure(
            url=config.get_main_option("sqlalchemy.url"),
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            **options,
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, **options)
        with context.begin_transaction():
            context.run_migrations()
