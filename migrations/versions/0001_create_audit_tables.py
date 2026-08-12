"""create audit_log and approvals tables

Revision ID: 0001
Revises:
Create Date: 2026-08-11

"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Durable counterpart to the Redis pending-approval state in
    # app/approvals.py (which has a 15-minute TTL - this table doesn't).
    # Not yet written to by the app (docs/roadmap.md Phase 5 only asks for
    # the audit_log side); it exists so audit_log.approval_id has something
    # to logically point at, per docs/architecture.md S3.5's schema.
    op.execute(
        """
        CREATE TABLE approvals (
            id UUID PRIMARY KEY,
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments JSONB NOT NULL,
            reason TEXT,
            status TEXT NOT NULL,
            requested_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            decided_by TEXT,
            decided_at TIMESTAMPTZ
        )
        """
    )

    # The append-only, hash-chained audit trail app/audit.py writes to.
    # `seq` (not `id`) is the primary key and what the hash chain orders on,
    # since it's a simple monotonic integer Postgres assigns - `id` is a
    # separate stable UUID identifier for a row, independent of insert order.
    op.execute(
        """
        CREATE TABLE audit_log (
            seq BIGSERIAL PRIMARY KEY,
            id UUID NOT NULL UNIQUE,
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args JSONB NOT NULL,
            policy_decision JSONB,
            approval_id UUID,
            result_status TEXT NOT NULL,
            result_summary JSONB,
            duration_ms INTEGER,
            otel_trace_id TEXT,
            row_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # No FK from audit_log.approval_id to approvals(id): the audit writer's
    # uptime must never depend on the approvals table being consistent (an
    # audit row has to be writable even if the approvals table has a bug or
    # gets dropped/rebuilt) - a deliberate soft reference, not an oversight.

    # One index per column app/audit.py's query_audit_log() filters on
    # (agent_id, tool, result_status) or sorts/ranges by (created_at) -
    # GET /admin/audit's four filter params, matched 1:1.
    op.execute("CREATE INDEX idx_audit_log_agent_id ON audit_log (agent_id)")
    op.execute("CREATE INDEX idx_audit_log_tool_name ON audit_log (tool_name)")
    op.execute("CREATE INDEX idx_audit_log_created_at ON audit_log (created_at)")
    op.execute("CREATE INDEX idx_audit_log_result_status ON audit_log (result_status)")


def downgrade() -> None:
    # Reverse order of upgrade() - even though there's no FK forcing this,
    # it keeps the migration symmetric with how the tables were created.
    op.execute("DROP TABLE audit_log")
    op.execute("DROP TABLE approvals")
