"""Unit tests for app/middleware/rate_limit.py: the SlidingWindowRateLimiter
algorithm (against FakeRedis) and the `rate_limit` ASGI dispatch adapter.
"""
import json
import sys
import time
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.middleware.rate_limit import (  # noqa: E402
    RATE_LIMIT_WINDOW_SECONDS,
    SlidingWindowRateLimiter,
    rate_limit,
)
from fakes import FailingRedis  # noqa: E402

SENTINEL_RESPONSE = PlainTextResponse("ok")


# ---- SlidingWindowRateLimiter -------------------------------------------


async def test_check_allows_calls_under_the_limit(fake_redis):
    limiter = SlidingWindowRateLimiter(fake_redis)
    result = await limiter.check("agent01", limit=3)
    assert result.allowed is True
    assert result.remaining == 2


async def test_check_denies_once_limit_is_reached(fake_redis):
    limiter = SlidingWindowRateLimiter(fake_redis)
    for _ in range(3):
        result = await limiter.check("agent01", limit=3)
        assert result.allowed is True

    result = await limiter.check("agent01", limit=3)
    assert result.allowed is False
    assert result.remaining == 0
    assert result.retry_after is not None
    assert result.retry_after >= 1


async def test_check_tracks_agents_independently(fake_redis):
    limiter = SlidingWindowRateLimiter(fake_redis)
    for _ in range(2):
        await limiter.check("agent01", limit=2)

    denied = await limiter.check("agent01", limit=2)
    allowed = await limiter.check("agent02", limit=2)
    assert denied.allowed is False
    assert allowed.allowed is True


async def test_check_expires_entries_outside_the_window(fake_redis):
    limiter = SlidingWindowRateLimiter(fake_redis, window_seconds=RATE_LIMIT_WINDOW_SECONDS)
    key = "ratelimit:agent01"
    stale_time = time.time() - RATE_LIMIT_WINDOW_SECONDS - 5
    fake_redis._zsets[key] = {"stale-member": stale_time}

    result = await limiter.check("agent01", limit=1)

    assert result.allowed is True
    assert "stale-member" not in fake_redis._zsets[key]


async def test_check_fails_open_when_redis_errors():
    limiter = SlidingWindowRateLimiter(FailingRedis())
    result = await limiter.check("agent01", limit=1)
    assert result.allowed is True
    assert result.remaining == 1


# ---- rate_limit dispatch --------------------------------------------------


class _FakeAppState:
    def __init__(self, limiter):
        self.rate_limiter = limiter


class _FakeApp:
    def __init__(self, limiter):
        self.state = _FakeAppState(limiter)


def make_request(path, agent, limiter):
    scope = {
        "type": "http",
        "path": path,
        "headers": [],
        "method": "POST",
        "app": _FakeApp(limiter),
    }
    request = Request(scope)
    request.state.agent = agent
    return request


class _Agent:
    id = "agent01"
    rate_limit_rpm = 60


async def test_non_rate_limited_paths_skip_the_limiter_entirely(fake_redis):
    limiter = SlidingWindowRateLimiter(fake_redis)
    request = make_request("/health/live", _Agent(), limiter)

    async def call_next(_request):
        return SENTINEL_RESPONSE

    response = await rate_limit(request, call_next)
    assert response is SENTINEL_RESPONSE
    # limiter untouched: no keys were written for this agent
    assert fake_redis._zsets == {}


async def test_allowed_request_gets_rate_limit_headers(fake_redis):
    limiter = SlidingWindowRateLimiter(fake_redis)
    request = make_request("/mcp", _Agent(), limiter)

    async def call_next(_request):
        return PlainTextResponse("ok")

    response = await rate_limit(request, call_next)
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "60"
    assert response.headers["X-RateLimit-Remaining"] == "59"
    assert "X-RateLimit-Reset" in response.headers


async def test_denied_request_returns_429_without_calling_next(fake_redis):
    limiter = SlidingWindowRateLimiter(fake_redis)
    agent = _Agent()
    agent.rate_limit_rpm = 1
    request = make_request("/mcp", agent, limiter)

    async def call_next(_request):
        raise AssertionError("call_next should not run once the limit is exceeded")

    # Consume the only allowed call directly against the limiter.
    await limiter.check(str(agent.id), agent.rate_limit_rpm)

    response = await rate_limit(request, call_next)
    assert response.status_code == 429
    assert response.headers["Retry-After"]
    body = json.loads(response.body)
    assert body["error"]["code"] == -32029
