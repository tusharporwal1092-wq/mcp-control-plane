"""End-to-end integration tests against the real app + middleware stack
(auth -> rate_limit -> route handler), via TestClient with a FakeRedis
backing the rate limiter so no real Redis server is required.
"""


def rpc(method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return body


def test_tool_call_with_missing_api_key_returns_401(client):
    response = client.post("/mcp", json=rpc("tools/call", {"name": "list_pods"}))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == -32001


def test_tool_call_with_invalid_api_key_returns_401(client):
    response = client.post(
        "/mcp",
        json=rpc("tools/call", {"name": "list_pods"}),
        headers={"x-api-key": "wrong_key"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == -32001


def test_tool_call_with_valid_key_but_disallowed_tool_returns_403(client):
    # "test_key" isn't scoped to trigger_jenkins_job - see API_KEYS in app/middleware/auth.py.
    response = client.post(
        "/mcp",
        json=rpc("tools/call", {"name": "trigger_jenkins_job"}),
        headers={"x-api-key": "test_key"},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == -32003
    assert "not allowed to call" in body["error"]["data"]["error"]
    assert body["error"]["data"]["policy_decision"] == "forbidden"


def test_tool_call_requiring_approval_returns_403_without_executing(client, monkeypatch):
    from app import main as app_main
    from app.authz.opa import PolicyDecision

    async def _require_approval(context):
        return PolicyDecision(allow=True, require_approval=True, reason="destructive action in prod")

    monkeypatch.setattr(app_main, "evaluate_policy", _require_approval)

    response = client.post(
        "/mcp",
        json=rpc("tools/call", {"name": "list_pods", "arguments": {}}),
        headers={"x-api-key": "test_key"},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == -32010
    assert body["error"]["data"]["policy_decision"] == "require_approval"


def test_tool_call_with_valid_key_and_allowed_tool_succeeds(client, monkeypatch):
    # Every registered tool now calls a real downstream API (Phase 3 read
    # tools, Phase 4 write tools) or validates real arguments, so this test
    # stubs the dispatch table entry directly instead of relying on a tool
    # that happens to still be a placeholder - it only needs to cover the
    # golden path's plumbing: authn -> rate limit -> interceptor -> OPA ->
    # executor -> audit -> 200 response, not any tool's business logic.
    from app import main as app_main

    monkeypatch.setitem(app_main.TOOLS, "restart_deployment", lambda arguments: {"status": "success"})

    response = client.post(
        "/mcp",
        json=rpc("tools/call", {"name": "restart_deployment", "arguments": {}}),
        headers={"x-api-key": "test_key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == {"status": "success"}
