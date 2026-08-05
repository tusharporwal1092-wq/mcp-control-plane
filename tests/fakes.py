"""Fakes shared across tests.

FakeRedis implements just the subset of the redis.asyncio.Redis surface that
app/middleware/rate_limit.py relies on (sorted-set ops + pipeline), backed by
an in-memory dict instead of a real Redis server.
"""
import redis.asyncio as redis


class FakeRedis:
    def __init__(self):
        self._zsets: dict[str, dict[str, float]] = {}

    async def zremrangebyscore(self, key, min_score, max_score):
        zset = self._zsets.get(key, {})
        to_remove = [m for m, s in zset.items() if min_score <= s <= max_score]
        for m in to_remove:
            del zset[m]

    async def zcard(self, key):
        return len(self._zsets.get(key, {}))

    async def zrange(self, key, start, end, withscores=False):
        zset = self._zsets.get(key, {})
        ordered = sorted(zset.items(), key=lambda kv: kv[1])
        sliced = ordered[start : end + 1] if end != -1 else ordered[start:]
        if withscores:
            return sliced
        return [m for m, _ in sliced]

    async def zadd(self, key, mapping):
        self._zsets.setdefault(key, {}).update(mapping)

    async def expire(self, key, seconds):
        pass

    async def aclose(self):
        pass

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, client: FakeRedis):
        self._client = client
        self._ops = []

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, mapping))

    def expire(self, key, seconds):
        self._ops.append(("expire", key, seconds))

    async def execute(self):
        for op, key, arg in self._ops:
            if op == "zadd":
                await self._client.zadd(key, arg)
            elif op == "expire":
                await self._client.expire(key, arg)
        self._ops = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FailingRedis(FakeRedis):
    """A FakeRedis whose first call always raises a RedisError, to exercise fail-open."""

    async def zremrangebyscore(self, key, min_score, max_score):
        raise redis.RedisError("connection refused")
