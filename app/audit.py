"""Audit trail for tool calls.

Records every tool invocation - allowed, denied, or errored - so there is a
durable "who called what, when, with what result" trail independent of the
request/response cycle. This is a stub: it logs to stdout via the standard
logger. docs/architecture.md describes the eventual append-only,
SHA-256-chained PostgreSQL audit table (Phase 2+); swap the body of
`record_tool_call` for that write without touching any caller.
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("audit")


@dataclass
class AuditEvent:
    """One row of the (future) audit table."""

    agent_id: str
    role: str
    tool_name: str
    arguments: dict[str, Any]
    decision: str  # "allowed" | "denied" | "error"
    reason: str | None = None
    timestamp: float = field(default_factory=time.time)


def record_tool_call(event: AuditEvent) -> None:
    """Persist one audit event. Call this after every policy decision and
    after every tool execution (success or failure) in the /mcp handler."""
    logger.info(
        "tool_call agent=%s role=%s tool=%s decision=%s reason=%s",
        event.agent_id,
        event.role,
        event.tool_name,
        event.decision,
        event.reason,
    )
