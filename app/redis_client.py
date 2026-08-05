"""Shared Redis client factory.

Every component that needs Redis (rate limiter today, approval-gate state
later per docs/roadmap.md) should get its connection from here instead of
calling `redis.from_url(...)` itself, so the URL/config lives in one place.
"""
import os

import redis.asyncio as redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


def create_redis_client() -> redis.Redis:
    """Build a new async Redis client from the REDIS_URL env var."""
    return redis.from_url(REDIS_URL)
