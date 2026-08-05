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


@pytest.fixture
def client(monkeypatch, fake_redis):
    """TestClient wired to a FakeRedis (no real Redis needed) and a stub
    evaluate_policy that allows everything (no real OPA needed). Tests that
    care about actual policy decisions should override `app_main.evaluate_policy`
    themselves; OPA's own rule behavior is covered in test_opa.py."""
    monkeypatch.setattr(app_main, "create_redis_client", lambda: fake_redis)

    async def _allow_all(context):
        return PolicyDecision(allow=True, require_approval=False, reason="test stub: allow")

    monkeypatch.setattr(app_main, "evaluate_policy", _allow_all)
    with TestClient(app_main.app) as test_client:
        yield test_client
