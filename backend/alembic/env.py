"""Alembic migration environment.

Two deliberate choices:

1. The database URL is read from `DATABASE_URL_SYNC` at runtime, never stored in
   `alembic.ini`. Nothing secret is committed.
2. Migrations run on the *sync* psycopg driver even though the app is async.
   Alembic's runner is synchronous; mixing an asyncpg engine in adds an event
   loop for no benefit. Same database, different driver.

`target_metadata` points at the declarative Base so `alembic revision
--autogenerate` can diff models against the live schema. Every model module must
be imported (via `app.models`) or its table is invisible to autogenerate.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

# Importing the models package registers every table on Base.metadata.
import app.models  # noqa: F401  (side-effect import, required by autogenerate)
from alembic import context
from app.core.config import settings
from app.core.db import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — useful for reviewing a migration
    or handing DDL to a DBA."""
    context.configure(
        url=settings.database_url_sync,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations inside a transaction.

    Postgres has transactional DDL, so a failed migration rolls back completely
    rather than leaving the schema half-changed.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,  # detect column type changes
            compare_server_default=True,  # detect DEFAULT changes
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
