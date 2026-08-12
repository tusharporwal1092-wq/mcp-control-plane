"""Redis-backed pending-approval store for the human-in-the-loop gate.

A `require_approval` policy decision (app/authz/opa.py) suspends the tool
call here instead of executing it immediately: the pending approval is
persisted to Redis with a TTL (docs/roadmap.md Phase 4). Per
docs/threat-model.md T-07, approval state lives in Redis - not in whatever
the incoming Slack callback claims - so a forged callback can only trigger a
lookup by id, never a direct state write. `POST /admin/approvals/{id}/decide`
(app/main.py) is the only thing that resolves a pending approval.
"""
import json
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import redis.asyncio as redis

APPROVAL_TTL_SECONDS = 15 * 60
_KEY_PREFIX = "approval:"


@dataclass
class Approval:
    id: str
    agent_id: str
    role: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str
    status: str  # "pending" | "approved" | "denied"
    requested_at: float
    expires_at: float
    decided_by: str | None = None
    decided_at: float | None = None


def _key(approval_id: str) -> str:
    return f"{_KEY_PREFIX}{approval_id}"


async def create_pending_approval(
    redis_client: "redis.Redis", *, agent_id: str, role: str, tool_name: str, arguments: dict, reason: str
) -> Approval:
    now = time.time()
    approval = Approval(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        role=role,
        tool_name=tool_name,
        arguments=arguments,
        reason=reason,
        status="pending",
        requested_at=now,
        expires_at=now + APPROVAL_TTL_SECONDS,
    )
    await redis_client.set(_key(approval.id), json.dumps(asdict(approval)), ex=APPROVAL_TTL_SECONDS)
    return approval


async def get_approval(redis_client: "redis.Redis", approval_id: str) -> Approval | None:
    raw = await redis_client.get(_key(approval_id))
    if raw is None:
        return None
    return Approval(**json.loads(raw))


async def decide_approval(
    redis_client: "redis.Redis", approval_id: str, *, decision: str, decided_by: str
) -> tuple[Approval | None, str]:
    """Resolve a pending approval.

    Returns `(approval, outcome)` where `outcome` is:
      - "decided": approval.status is now "approved" or "denied" - act on it.
      - "not_found": the id is unknown, or its TTL already elapsed (Redis
        can't tell those apart once the key is gone, so neither can we -
        both are a 410 to the caller).
      - "already_decided": a replayed/duplicate callback for an id that was
        already resolved - the caller should return 409, not act again.
    """
    approval = await get_approval(redis_client, approval_id)
    if approval is None:
        return None, "not_found"
    if approval.status != "pending":
        return approval, "already_decided"
    if time.time() > approval.expires_at:
        return approval, "not_found"

    approval.status = "approved" if decision == "approve" else "denied"
    approval.decided_by = decided_by
    approval.decided_at = time.time()
    # keepttl: a decided record only needs to survive long enough to answer
    # a duplicate callback with 409, not forever - it can expire on the same
    # 15-minute clock the pending approval was already running on.
    await redis_client.set(_key(approval_id), json.dumps(asdict(approval)), keepttl=True)
    return approval, "decided"
