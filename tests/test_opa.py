"""Unit tests for app/authz/opa.py: the HTTP call to the OPA sidecar and how
its `{"result": {...}}` response maps to a PolicyDecision. Uses
httpx.MockTransport so no real OPA server is needed.
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.authz import opa as opa_module  # noqa: E402
from app.interceptor import ToolCallContext  # noqa: E402


def make_context(tool_name="get_pod_logs", role="sre1", arguments=None):
    return ToolCallContext(
        tool_name=tool_name, arguments=arguments or {}, agent_id="agent01", role=role
    )


_RealAsyncClient = httpx.AsyncClient


def mock_opa(monkeypatch, handler):
    """Point opa.py's httpx.AsyncClient at a MockTransport running `handler`."""
    monkeypatch.setattr(
        opa_module.httpx,
        "AsyncClient",
        lambda *a, **kw: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_allow_response_maps_to_allowed_decision(monkeypatch):
    mock_opa(monkeypatch, lambda req: httpx.Response(200, json={"result": {"allow": True}}))
    decision = await opa_module.evaluate_policy(make_context())
    assert decision.allow is True
    assert decision.require_approval is False


async def test_deny_response_maps_to_denied_decision(monkeypatch):
    mock_opa(monkeypatch, lambda req: httpx.Response(200, json={"result": {"allow": False}}))
    decision = await opa_module.evaluate_policy(make_context())
    assert decision.allow is False


async def test_require_approval_is_passed_through(monkeypatch):
    mock_opa(
        monkeypatch,
        lambda req: httpx.Response(200, json={"result": {"allow": True, "require_approval": True}}),
    )
    decision = await opa_module.evaluate_policy(make_context(tool_name="restart_deployment"))
    assert decision.allow is True
    assert decision.require_approval is True
    assert "approval" in decision.reason


async def test_unreachable_opa_fails_closed(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    mock_opa(monkeypatch, handler)
    decision = await opa_module.evaluate_policy(make_context())
    assert decision.allow is False
    assert decision.require_approval is False
    assert decision.reason == "policy engine unavailable"


async def test_malformed_opa_response_fails_closed(monkeypatch):
    mock_opa(monkeypatch, lambda req: httpx.Response(200, content=b"not json"))
    decision = await opa_module.evaluate_policy(make_context())
    assert decision.allow is False


async def test_input_sent_to_opa_matches_context_shape(monkeypatch):
    captured = {}

    def handler(request):
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": {"allow": True}})

    mock_opa(monkeypatch, handler)
    context = make_context(arguments={"namespace": "prod-payments"})
    await opa_module.evaluate_policy(context)

    assert captured["body"] == {"input": context.to_opa_input()}
    assert captured["body"]["input"]["environment"] == "prod"
