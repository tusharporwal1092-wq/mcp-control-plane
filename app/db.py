"""Shared PostgreSQL connection pool.

docs/architecture.md commits to asyncpg directly (not an ORM) for the audit
store; app/audit.py is the only caller today. Schema lives in migrations/
(Alembic, raw SQL - see migrations/versions/0001_create_audit_tables.py),
apply with `uv run alembic upgrade head` before the gateway can write audit
rows.
"""
import os

import asyncpg

# Connection string for the audit_log/approvals database. Same variable
# `migrations/env.py` reads when running Alembic, and `docker-compose.yaml`
# points it at the `postgres` service - one env var, one source of truth.
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/mcp_control_plane")


async def create_db_pool() -> asyncpg.Pool:
    """Build the app's Postgres connection pool. Called once from
    app/main.py's `startup()` and stashed on `app.state.db`; every audit
    read/write goes through that shared pool rather than opening its own
    connection."""
    return await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
