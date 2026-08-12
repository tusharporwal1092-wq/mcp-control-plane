"""Alembic environment.

Runs migrations asynchronously through SQLAlchemy's asyncio engine (the
`postgresql+asyncpg://` driver) so the migration path uses the same asyncpg
driver as the app itself (app/db.py) rather than pulling in a second,
sync-only Postgres driver just for Alembic.

Migrations are raw SQL (`op.execute(...)`) - docs/architecture.md commits to
asyncpg, not an ORM, for the audit store, so there are no SQLAlchemy models
here to autogenerate from. `target_metadata = None` disables autogenerate
accordingly; every migration is written by hand.
"""
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/mcp_control_plane"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
# asyncpg needs the `+asyncpg` driver qualifier that app/db.py's plain
# `postgresql://` DSN doesn't carry (asyncpg.create_pool() takes libpq-style
# URLs directly; SQLAlchemy's engine needs the dialect prefix).
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1))

target_metadata = None


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: no DB connection at all, just prints the
    SQL that would run. Not used by anything in this repo today (no CI step
    or script calls it), kept because it's Alembic's standard entrypoint."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """The actual migration run, given an already-open (sync-facing)
    connection - called via `connection.run_sync(...)` below since Alembic's
    migration runner itself is synchronous even though the engine is async."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """What `uv run alembic upgrade head` actually runs: open one async
    connection to the real database and run every pending migration through
    it. NullPool - this process runs once and exits, no benefit to pooling."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


# Alembic imports this module and expects migrations to have already run by
# the time the import finishes - so, unlike app/main.py, this isn't wrapped
# in a function Alembic calls; it just executes at module load time.
if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
