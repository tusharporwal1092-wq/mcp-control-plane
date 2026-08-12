"""In-process pub/sub from the approval-decide endpoint to an agent's open
`GET /mcp/sse` connection, so an approval resolved via Slack can be pushed
to the agent that's still waiting on it instead of requiring a poll.

ponytail: single-process only - a multi-pod gateway needs this backed by
Redis pub/sub instead (same Redis already used for rate limiting and
approvals), not built here since the app runs as one process today.
"""
import asyncio
import contextlib
from typing import Any

_subscribers: dict[str, list[asyncio.Queue]] = {}


@contextlib.asynccontextmanager
async def subscribe(agent_id: str):
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(agent_id, []).append(queue)
    try:
        yield queue
    finally:
        _subscribers[agent_id].remove(queue)
        if not _subscribers[agent_id]:
            del _subscribers[agent_id]


async def publish(agent_id: str, event: dict[str, Any]) -> None:
    for queue in _subscribers.get(agent_id, []):
        await queue.put(event)
