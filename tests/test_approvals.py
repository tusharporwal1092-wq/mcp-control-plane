"""Tests for the approval gate (docs/roadmap.md Phase 4): the require_approval
policy branch in app/main.py's /mcp handler, the Slack HMAC-verified decide
endpoint, and the SSE push on decision. Uses the same `client`/`fake_redis`
fixtures as tests/test_mcp_integration.py (conftest.py), so the approval
store rides on the same FakeRedis the rate limiter already uses - no real
Redis or Slack needed.
"""
import dataclasses
import hashlib
import hmac
import json
import time

import pytest

from app import approvals
from app import main as app_main
from app import slack as slack_module
from app import sse_hub

SIGNING_SECRET = "test-signing-secret"


def _sign(body: bytes, timestamp: str, secret: str = SIGNING_SECRET) -> str:
    basestring = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


def _decide_request(approval_id, decision, decided_by="alice@example.com"):
    body = json.dumps({"decision": decision, "decided_by": decided_by}).encode()
    timestamp = str(int(time.time()))
    headers = {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": _sign(body, timestamp),
        "content-type": "application/json",
    }
    return body, headers


def rpc(method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return body


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setattr(slack_module, "SLACK_SIGNING_SECRET", SIGNING_SECRET)


@pytest.fixture(autouse=True)
def _no_slack_notification(monkeypatch):
    async def _noop(approval):
        return None

    monkeypatch.setattr(app_main.slack, "send_approval_request", _noop)


def _stub_require_approval(monkeypatch, reason="prod destructive action"):
    from app.authz.opa import PolicyDecision

    async def _require_approval(context):
        return PolicyDecision(allow=True, require_approval=True, reason=reason)

    monkeypatch.setattr(app_main, "evaluate_policy", _require_approval)


async def test_full_approval_flow_restart_deployment_in_prod(client, monkeypatch):
    """restart_deployment in prod -> approval pending -> approve -> executor
    called -> audit logged -> pushed to the agent's SSE connection."""
    _stub_require_approval(monkeypatch)
    calls = []
    monkeypatch.setitem(
        app_main.TOOLS,
        "restart_deployment",
        lambda arguments: calls.append(arguments) or {"status": "restart_initiated"},
    )
    # conftest.py's `client` fixture already no-ops record_tool_call (no real
    # Postgres in this test file); override it again here, this time
    # capturing each AuditEvent instead of discarding it, so this test can
    # assert on what got logged (real DB persistence is proven separately in
    # test_audit_docker_integration.py).
    audit_events = []

    async def _capture_record_tool_call(pool, event):
        audit_events.append(event)

    monkeypatch.setattr(app_main, "record_tool_call", _capture_record_tool_call)

    async with sse_hub.subscribe("agent01") as queue:
        response = client.post(
            "/mcp",
            json=rpc(
                "tools/call",
                {
                    "name": "restart_deployment",
                    "arguments": {"namespace": "prod-payments", "deployment": "checkout-api", "reason": "fixing pool exhaustion"},
                },
            ),
            headers={"x-api-key": "test_key"},
        )
        assert response.status_code == 202
        approval_id = response.json()["result"]["_meta"]["approval_id"]
        assert calls == []  # not executed while pending

        body, headers = _decide_request(approval_id, "approve")
        decide_response = client.post(f"/admin/approvals/{approval_id}/decide", content=body, headers=headers)

        assert decide_response.status_code == 200
        assert decide_response.json()["status"] == "approved"
        assert calls == [{"namespace": "prod-payments", "deployment": "checkout-api", "reason": "fixing pool exhaustion"}]

        decisions = [e.decision for e in audit_events]
        assert "pending_approval" in decisions
        assert "allowed" in decisions

        event = queue.get_nowait()
        assert event["approval_id"] == approval_id
        assert event["status"] == "approved"

    # a replayed callback for the now-resolved approval must not re-execute.
    body, headers = _decide_request(approval_id, "approve")
    replay_response = client.post(f"/admin/approvals/{approval_id}/decide", content=body, headers=headers)
    assert replay_response.status_code == 409
    assert len(calls) == 1


async def test_denial_is_audited_and_does_not_execute(client, monkeypatch):
    _stub_require_approval(monkeypatch)
    monkeypatch.setitem(
        app_main.TOOLS,
        "restart_deployment",
        lambda arguments: (_ for _ in ()).throw(AssertionError("must not execute on denial")),
    )
    audit_events = []

    async def _capture_record_tool_call(pool, event):
        audit_events.append(event)

    monkeypatch.setattr(app_main, "record_tool_call", _capture_record_tool_call)

    response = client.post(
        "/mcp",
        json=rpc(
            "tools/call",
            {
                "name": "restart_deployment",
                "arguments": {"namespace": "prod-payments", "deployment": "checkout-api", "reason": "fixing pool exhaustion"},
            },
        ),
        headers={"x-api-key": "test_key"},
    )
    approval_id = response.json()["result"]["_meta"]["approval_id"]

    body, headers = _decide_request(approval_id, "deny", decided_by="bob@example.com")
    decide_response = client.post(f"/admin/approvals/{approval_id}/decide", content=body, headers=headers)

    assert decide_response.status_code == 200
    assert decide_response.json()["status"] == "denied"
    assert any(e.decision == "approval_denied" for e in audit_events)


def test_forged_signature_is_rejected_with_401(client):
    body = json.dumps({"decision": "approve", "decided_by": "eve@example.com"}).encode()
    headers = {
        "x-slack-request-timestamp": str(int(time.time())),
        "x-slack-signature": "v0=" + "0" * 64,
        "content-type": "application/json",
    }
    response = client.post("/admin/approvals/some-id/decide", content=body, headers=headers)
    assert response.status_code == 401


def test_stale_timestamp_is_rejected_with_401(client):
    # A valid signature computed over a >5-minute-old timestamp: the HMAC
    # matches, but the request must still be rejected as a replay.
    body = json.dumps({"decision": "approve", "decided_by": "eve@example.com"}).encode()
    stale_timestamp = str(int(time.time()) - 600)
    headers = {
        "x-slack-request-timestamp": stale_timestamp,
        "x-slack-signature": _sign(body, stale_timestamp),
        "content-type": "application/json",
    }
    response = client.post("/admin/approvals/some-id/decide", content=body, headers=headers)
    assert response.status_code == 401


async def test_expired_approval_returns_410(client, fake_redis):
    approval = await approvals.create_pending_approval(
        fake_redis, agent_id="agent01", role="sre1", tool_name="restart_deployment", arguments={}, reason="prod"
    )
    # Simulate TTL elapsed by back-dating expires_at directly - waiting out
    # the real 15-minute TTL in a test isn't worth it, and this exercises
    # the same expiry check decide_approval runs regardless of the source.
    expired = dataclasses.replace(approval, expires_at=time.time() - 1)
    await fake_redis.set(approvals._key(approval.id), json.dumps(dataclasses.asdict(expired)), keepttl=True)

    body, headers = _decide_request(approval.id, "approve")
    response = client.post(f"/admin/approvals/{approval.id}/decide", content=body, headers=headers)
    assert response.status_code == 410


async def test_unknown_approval_id_returns_410(client):
    body, headers = _decide_request("does-not-exist", "approve")
    response = client.post("/admin/approvals/does-not-exist/decide", content=body, headers=headers)
    assert response.status_code == 410
