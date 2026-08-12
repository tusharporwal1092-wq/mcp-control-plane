import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main as app_main  # noqa: E402
from app.authz.opa import PolicyDecision  # noqa: E402
from fakes import FakeRedis  # noqa: E402

VALID_API_KEY = "test_key"


@pytest.fixture
def fake_redis():
    return FakeRedis()


class _NoopDbPool:
    """Stand-in for asyncpg.Pool so app startup/shutdown don't need a real
    Postgres: app.state.db just has to exist and support `.close()`."""

    async def close(self):
        pass


@pytest.fixture
def client(monkeypatch, fake_redis):
    """TestClient wired to a FakeRedis (no real Redis needed), a stub
    evaluate_policy that allows everything (no real OPA needed), and a no-op
    audit writer (no real Postgres needed). Tests that care about actual
    policy decisions should override `app_main.evaluate_policy` themselves
    (OPA's own rule behavior is covered in test_opa.py); tests that care
    about audit content should override `app_main.record_tool_call`
    themselves (real Postgres persistence/hash-chaining is covered in
    test_audit_docker_integration.py)."""
    monkeypatch.setattr(app_main, "create_redis_client", lambda: fake_redis)

    # Must be an async function (not a plain lambda) returning the fake -
    # app/main.py's startup() does `await create_db_pool()`.
    async def _create_noop_db_pool():
        return _NoopDbPool()

    monkeypatch.setattr(app_main, "create_db_pool", _create_noop_db_pool)

    async def _allow_all(context):
        return PolicyDecision(allow=True, require_approval=False, reason="test stub: allow")

    async def _noop_record_tool_call(pool, event):
        pass

    monkeypatch.setattr(app_main, "evaluate_policy", _allow_all)
    monkeypatch.setattr(app_main, "record_tool_call", _noop_record_tool_call)
    with TestClient(app_main.app) as test_client:
        yield test_client
