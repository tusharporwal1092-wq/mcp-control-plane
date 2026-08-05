"""Unit tests for app/middleware/auth.py, exercising `authenticate` directly
(no HTTP client, no app startup) against hand-built Starlette Request objects.
"""
import sys
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.middleware.auth import API_KEYS, authenticate  # noqa: E402

SENTINEL_RESPONSE = PlainTextResponse("ok")


def make_request(path="/mcp", api_key=None):
    headers = []
    if api_key is not None:
        headers.append((b"x-api-key", api_key.encode()))
    scope = {"type": "http", "path": path, "headers": headers, "method": "POST"}
    return Request(scope)


async def call_next_ok(request):
    return SENTINEL_RESPONSE


async def call_next_fails(request):
    raise AssertionError("call_next should not be invoked when auth is rejected")


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
async def test_public_paths_bypass_auth_entirely(path):
    request = make_request(path=path, api_key=None)
    response = await authenticate(request, call_next_ok)
    assert response is SENTINEL_RESPONSE


async def test_missing_api_key_returns_401():
    request = make_request(path="/mcp", api_key=None)
    response = await authenticate(request, call_next_fails)
    assert response.status_code == 401


async def test_invalid_api_key_returns_401():
    request = make_request(path="/mcp", api_key="not-a-real-key")
    response = await authenticate(request, call_next_fails)
    assert response.status_code == 401


async def test_401_body_is_jsonrpc_shaped_error():
    request = make_request(path="/mcp", api_key=None)
    response = await authenticate(request, call_next_fails)
    import json

    body = json.loads(response.body)
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == -32001
    assert "API key" in body["error"]["message"]


async def test_valid_api_key_attaches_agent_and_calls_next():
    request = make_request(path="/mcp", api_key="test_key")
    response = await authenticate(request, call_next_ok)
    assert response is SENTINEL_RESPONSE
    assert request.state.agent == API_KEYS["test_key"]


async def test_valid_api_key_agent_has_expected_identity():
    request = make_request(path="/mcp", api_key="test_key")
    await authenticate(request, call_next_ok)
    agent = request.state.agent
    assert agent.id == "agent01"
    assert agent.role == "sre1"
    assert agent.allowed_tools == ["get_pod_logs", "list_pods", "get_deployment_status"]
