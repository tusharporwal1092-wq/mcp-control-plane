"""Sliding-window rate limiting middleware.

`SlidingWindowRateLimiter` is the reusable, Redis-backed algorithm; `rate_limit`
is the thin ASGI adapter that main.py registers as middleware. It runs
*after* app/middleware/auth.py in the stack, so `request.state.agent` is
already populated by the time it reads `agent.id` / `agent.rate_limit_rpm`.
Only paths in RATE_LIMITED_PATHS are checked - everything else passes
through untouched.

To change the limiting algorithm later (token bucket, fixed window, etc.),
swap the body of SlidingWindowRateLimiter without touching main.py.
"""
import logging
import time
import uuid
from dataclasses import dataclass

import redis.asyncio as redis
from fastapi import Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMITED_PATHS = {"/mcp"}


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset: int
    retry_after: int | None = None


class SlidingWindowRateLimiter:
    """Redis-backed sliding window rate limiter, keyed by agent_id.

    Uses a sorted set per agent (score = call timestamp) so the window slides
    continuously rather than resetting on a fixed boundary.
    """

    def __init__(self, redis_client: "redis.Redis", window_seconds: int = RATE_LIMIT_WINDOW_SECONDS):
        self.redis = redis_client
        self.window_seconds = window_seconds

    async def check(self, agent_id: str, limit: int) -> RateLimitResult:
        key = f"ratelimit:{agent_id}"
        now = time.time()
        window_start = now - self.window_seconds

        try:
            await self.redis.zremrangebyscore(key, 0, window_start)
            count = await self.redis.zcard(key)

            if count >= limit:
                oldest = await self.redis.zrange(key, 0, 0, withscores=True)
                reset_at = oldest[0][1] + self.window_seconds if oldest else now + self.window_seconds
                return RateLimitResult(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    reset=int(reset_at),
                    retry_after=max(1, int(reset_at - now)),
                )

            member = f"{now}:{uuid.uuid4().hex}"
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.zadd(key, {member: now})
                pipe.expire(key, self.window_seconds)
                await pipe.execute()

            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=max(0, limit - count - 1),
                reset=int(now + self.window_seconds),
            )
        except redis.RedisError:
            # Redis being unavailable shouldn't take the whole gateway down;
            # fail open and let the request through.
            logger.exception("Rate limiter Redis error for agent %s; failing open", agent_id)
            return RateLimitResult(allowed=True, limit=limit, remaining=limit, reset=int(now + self.window_seconds))


def _rpc_error(rpc_id, code: int, message: str, data: dict | None = None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


async def rate_limit(request: Request, call_next):
    """ASGI middleware entrypoint: enforce the caller's per-minute quota.

    Registered in app/main.py via `app.add_middleware(..., dispatch=rate_limit)`.
    Reads the shared limiter instance off `request.app.state.rate_limiter`
    (created at startup in main.py so it's built once, not per-request).
    """
    if request.url.path not in RATE_LIMITED_PATHS:
        return await call_next(request)

    agent = request.state.agent
    limiter: SlidingWindowRateLimiter = request.app.state.rate_limiter
    limit_result = await limiter.check(str(agent.id), agent.rate_limit_rpm)

    if not limit_result.allowed:
        return JSONResponse(
            _rpc_error(
                None,
                -32029,
                "Rate limit exceeded",
                {"limit": limit_result.limit, "retry_after": limit_result.retry_after},
            ),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={
                "X-RateLimit-Limit": str(limit_result.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(limit_result.reset),
                "Retry-After": str(limit_result.retry_after),
            },
        )

    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(limit_result.limit)
    response.headers["X-RateLimit-Remaining"] = str(limit_result.remaining)
    response.headers["X-RateLimit-Reset"] = str(limit_result.reset)
    return response
