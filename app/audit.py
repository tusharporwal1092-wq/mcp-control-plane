"""Append-only, hash-chained audit trail (docs/roadmap.md Phase 5).

Every tool call - allowed, denied, pending/decided approval, or errored -
gets one row in Postgres's `audit_log` table (schema:
migrations/versions/0001_create_audit_tables.py). Each row's `row_hash`
chains off the previous row's hash (docs/architecture.md S3.5), so editing
or deleting any row breaks every hash computed after it; `verify_rows`
(used by `GET /admin/audit`) detects that by recomputing hashes rather than
needing a separate integrity log.

Writes are serialized with a Postgres advisory lock (see CHAIN_LOCK_KEY)
instead of `SELECT ... FOR UPDATE` on the last row, since there's no row to
lock on the very first write. ponytail: one global lock serializes every
audit write in the whole gateway - fine at this app's expected volume (one
row per tool call, not per request), would need sharding (e.g. one lock per
agent_id) if write throughput ever became the bottleneck.
"""
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import asyncpg
from opentelemetry import trace

from .otel import audit_chain_integrity_failures_total, tool_call_duration_ms, tool_calls_total, tracer

logger = logging.getLogger("audit")

GENESIS_HASH = "0" * 64
# Arbitrary constant fed to pg_advisory_xact_lock - any int works, it only
# has to be the same constant every caller uses, so they actually serialize
# against each other rather than each locking a different key.
CHAIN_LOCK_KEY = 727272
MAX_QUERY_LIMIT = 500
DEFAULT_QUERY_LIMIT = 50


@dataclass
class AuditEvent:
    """One row of the audit_log table, before it's assigned an id/hash/seq."""

    agent_id: str
    role: str
    tool_name: str
    arguments: dict[str, Any]
    decision: str  # "allowed" | "denied" | "error" | "pending_approval" | "executor_timeout" | "approval_denied"
    reason: str | None = None
    result: dict | None = None
    approval_id: str | None = None
    duration_ms: int | None = None
    timestamp: float = field(default_factory=time.time)


def _row_hash(prev_hash: str, row_id: str, agent_id: str, tool_name: str, args: dict, result: dict | None, created_at_iso: str) -> str:
    """SHA-256 of `prev_hash + id + agent_id + tool + args + result +
    timestamp` (docs/roadmap.md Phase 5). `args`/`result` are serialized
    with sort_keys so the hash is reproducible when recomputed later, even
    though Postgres's jsonb storage doesn't preserve key order."""
    payload = (
        prev_hash
        + row_id
        + agent_id
        + tool_name
        + json.dumps(args, sort_keys=True, default=str)
        + json.dumps(result, sort_keys=True, default=str)
        + created_at_iso
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _decode_jsonb(value):
    """asyncpg returns jsonb columns as raw text by default (no codec
    registered) - decode back to a dict/list/None for hashing or JSON output."""
    if value is None or not isinstance(value, str):
        return value
    return json.loads(value)


def _current_trace_id() -> str | None:
    """Hex trace id of whatever span is active when this is called (the
    request's auto-instrumented FastAPI span, by the time record_tool_call
    runs) - lets an audit_log row be looked up straight from its Tempo
    trace. None outside any span (e.g. a test that calls this directly)."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x")


async def record_tool_call(pool: asyncpg.Pool, event: AuditEvent) -> None:
    """Persist one audit event as a new, chained row. Call this after every
    policy decision and after every tool execution (success or failure)."""
    with tracer.start_as_current_span("audit_write") as span:
        span.set_attribute("agent_id", event.agent_id)
        span.set_attribute("tool_name", event.tool_name)
        span.set_attribute("policy_decision", event.decision)
        if event.duration_ms is not None:
            span.set_attribute("duration_ms", event.duration_ms)

        # This row's own identity, independent of where it lands in the chain.
        row_id = str(uuid.uuid4())
        created_at = datetime.fromtimestamp(event.timestamp, tz=timezone.utc)
        created_at_iso = created_at.isoformat()
        otel_trace_id = _current_trace_id()

        async with pool.acquire() as conn, conn.transaction():
            # Serializes the read-prev-hash-then-insert sequence across
            # concurrent requests, so two writers can never chain off the same
            # prev_hash - released automatically when the transaction ends.
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", CHAIN_LOCK_KEY)

            # Chain off whatever the current last row's hash is - GENESIS_HASH
            # if this is the very first row the table has ever seen.
            prev_hash = await conn.fetchval("SELECT row_hash FROM audit_log ORDER BY seq DESC LIMIT 1")
            if prev_hash is None:
                prev_hash = GENESIS_HASH

            row_hash = _row_hash(prev_hash, row_id, event.agent_id, event.tool_name, event.arguments, event.result, created_at_iso)

            # Single INSERT writes the row and its hash together, still inside
            # the advisory-locked transaction, so no other writer can slip a row
            # in between "we read prev_hash" and "we wrote our row".
            await conn.execute(
                """
                INSERT INTO audit_log
                    (id, agent_id, role, tool_name, args, policy_decision, approval_id,
                     result_status, result_summary, duration_ms, otel_trace_id, row_hash, created_at)
                VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6::jsonb, $7::uuid, $8, $9::jsonb, $10, $11, $12, $13)
                """,
                row_id,
                event.agent_id,
                event.role,
                event.tool_name,
                json.dumps(event.arguments, default=str),
                json.dumps({"reason": event.reason}),
                event.approval_id,
                event.decision,
                json.dumps(event.result, default=str) if event.result is not None else None,
                event.duration_ms,
                otel_trace_id,
                row_hash,
                created_at,
            )

        # tool_calls_total/tool_call_duration_ms: this is the one call site
        # every outcome (allowed, denied, pending_approval, error, ...)
        # already routes through, so it's also the one place that needs to
        # know about the Phase 6 metrics - no per-outcome instrumentation
        # scattered across app/main.py.
        tool_calls_total.add(
            1, {"tool_name": event.tool_name, "result_status": event.decision, "role": event.role}
        )
        if event.duration_ms is not None:
            tool_call_duration_ms.record(event.duration_ms, {"tool_name": event.tool_name})

    # Keep the old stdout log line too - cheap, human-grep-able, and doesn't
    # depend on the DB write having succeeded to be useful during an outage.
    logger.info(
        "tool_call agent=%s role=%s tool=%s decision=%s reason=%s",
        event.agent_id,
        event.role,
        event.tool_name,
        event.decision,
        event.reason,
    )


def row_to_json(row: dict) -> dict:
    """Convert one audit_log row (asyncpg Record/dict - jsonb columns as raw
    text, uuid/timestamptz as native uuid.UUID/datetime objects) into a
    JSON-serializable dict for the admin API (docs/api-design.md S4.2)."""
    return {
        "id": str(row["id"]),
        "seq": row["seq"],
        "agent_id": row["agent_id"],
        "role": row["role"],
        "tool_name": row["tool_name"],
        "args": _decode_jsonb(row["args"]),
        "policy_decision": _decode_jsonb(row["policy_decision"]),
        "approval_id": str(row["approval_id"]) if row["approval_id"] else None,
        "result_status": row["result_status"],
        "result_summary": _decode_jsonb(row["result_summary"]),
        "duration_ms": row["duration_ms"],
        "otel_trace_id": row["otel_trace_id"],
        "row_hash": row["row_hash"],
        "created_at": row["created_at"].isoformat(),
    }


async def verify_rows(pool: asyncpg.Pool, rows: list[dict]) -> tuple[str, int | None]:
    """Recompute each row's hash against its *true* predecessor (by seq,
    fetched fresh - not just the previous item in `rows`, since `rows` may
    be a filtered page that skips seq numbers) and compare to the stored
    row_hash.

    Returns `("pass", None)` or `("fail", seq)` for the first row whose
    stored hash doesn't match - i.e. that row was edited, or its true
    predecessor was (a tampered predecessor changes what prev_hash *should*
    have been, so it flows forward automatically without re-checking the
    whole table on every query).
    """
    async with pool.acquire() as conn:
        # Ascending seq order: each row's predecessor is always seq - 1, so
        # walking oldest-to-newest lets that lookup stay a single simple query.
        for row in sorted(rows, key=lambda r: r["seq"]):
            if row["seq"] == 1:
                prev_hash = GENESIS_HASH
            else:
                prev_hash = await conn.fetchval("SELECT row_hash FROM audit_log WHERE seq = $1", row["seq"] - 1)
                if prev_hash is None:
                    # The predecessor row is missing entirely (deleted) -
                    # the chain is broken regardless of this row's own hash.
                    return "fail", row["seq"]

            # Recompute this row's hash from its own stored fields + the
            # predecessor's hash, and compare to what's actually stored.
            expected = _row_hash(
                prev_hash,
                str(row["id"]),
                row["agent_id"],
                row["tool_name"],
                _decode_jsonb(row["args"]),
                _decode_jsonb(row["result_summary"]),
                row["created_at"].isoformat(),
            )
            if expected != row["row_hash"]:
                return "fail", row["seq"]
    return "pass", None


def _build_filters(agent_id: str | None, tool: str | None, from_: datetime | None, to: datetime | None, result_status: str | None):
    """Turn GET /admin/audit's optional query params into a `WHERE ...`
    clause + matching positional params list - only the filters that were
    actually passed end up in the query, and every value is bound as a
    `$N` parameter (never string-interpolated) so this can't be SQL-injected
    via a crafted `agent_id`/`tool` filter."""
    conditions: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("agent_id", agent_id),
        ("tool_name", tool),
        ("result_status", result_status),
    ):
        if value:
            params.append(value)
            conditions.append(f"{column} = ${len(params)}")
    if from_:
        params.append(from_)
        conditions.append(f"created_at >= ${len(params)}")
    if to:
        params.append(to)
        conditions.append(f"created_at <= ${len(params)}")
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where_clause, params


async def query_audit_log(
    pool: asyncpg.Pool,
    *,
    agent_id: str | None = None,
    tool: str | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    result_status: str | None = None,
    limit: int = DEFAULT_QUERY_LIMIT,
    offset: int = 0,
) -> tuple[int, list[dict], str, int | None]:
    """Query audit_log with filters/pagination (docs/api-design.md S4.2),
    plus an `integrity_check` over the returned rows. Returns
    `(total, rows, integrity_check, first_broken_seq)`."""
    limit = max(1, min(limit, MAX_QUERY_LIMIT))  # never let a caller ask for more than MAX_QUERY_LIMIT rows at once
    offset = max(0, offset)
    where_clause, params = _build_filters(agent_id, tool, from_, to, result_status)

    async with pool.acquire() as conn:
        # Two queries: total count for pagination metadata (ignores
        # limit/offset), then the actual page of rows, newest-first.
        total = await conn.fetchval(f"SELECT COUNT(*) FROM audit_log {where_clause}", *params)
        records = await conn.fetch(
            f"SELECT * FROM audit_log {where_clause} ORDER BY seq DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}",
            *params,
            limit,
            offset,
        )

    # Verify the hash chain over just this page - see verify_rows' own
    # docstring for why that's still a meaningful check on a filtered page.
    rows = [dict(r) for r in records]
    status, first_broken_seq = await verify_rows(pool, rows)
    if status == "fail":
        logger.error("audit_log hash chain broken at seq=%s", first_broken_seq)
        audit_chain_integrity_failures_total.add(1)
    return total, rows, status, first_broken_seq


async def export_audit_log(pool: asyncpg.Pool, *, from_: datetime, to: datetime):
    """Stream every audit_log row in [from_, to] as dicts, ordered by seq,
    for the NDJSON export endpoint (docs/api-design.md S4.2). Uses an
    asyncpg server-side cursor so the whole range doesn't have to fit in
    memory at once - the pool connection stays checked out for the
    duration of the export, which is fine for an infrequent admin/compliance
    operation but would need a dedicated non-pooled connection if export
    became a high-concurrency path."""
    async with pool.acquire() as conn, conn.transaction():
        async for record in conn.cursor(
            "SELECT * FROM audit_log WHERE created_at >= $1 AND created_at <= $2 ORDER BY seq ASC",
            from_,
            to,
        ):
            yield row_to_json(dict(record))
