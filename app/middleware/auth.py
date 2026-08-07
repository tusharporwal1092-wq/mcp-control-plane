"""API key authentication middleware.

Resolves the `x-api-key` header against the static API_KEYS registry and
attaches the matching identity to `request.state.agent`. Every downstream
piece (rate limiter, /mcp handler) assumes `request.state.agent` is already
populated - this middleware is what guarantees that for every path except
PUBLIC_PATHS.

To back this with a real identity store later, replace the API_KEYS dict
lookup below with a database/secrets-manager call; nothing else needs to
change since callers only depend on the returned Agent_data shape.
"""
from fastapi import Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DEFAULT_RATE_LIMIT_RPM = 60

# Paths that don't require an API key (liveness/readiness probes).
PUBLIC_PATHS = {"/health/live", "/health/ready"}


class Agent_data(BaseModel):
    """Resolved caller identity: who is calling, what they may do, how fast."""

    id: str | int | None = None
    role: str
    allowed_tools: list[str]
    rate_limit_rpm: int = DEFAULT_RATE_LIMIT_RPM


# Registry mapping API keys to the agent identity they resolve to.
API_KEYS: dict[str, Agent_data] = {
    # Keep in sync with the "sre1" role's allowed_tools in policies/data.json:
    # this list is the gateway's own pre-OPA scope check, OPA is the second
    # (environment/namespace-aware) layer for whatever passes this one.
    "test_key": Agent_data(
        id="agent01",
        role="sre1",
        allowed_tools=[
            "get_pod_logs",
            "list_pods",
            "get_deployment_status",
            "restart_deployment",
            "scale_deployment",
            "read_prometheus_metrics",
        ],
    ),
}


def _rpc_error(rpc_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


async def authenticate(request: Request, call_next):
    """ASGI middleware entrypoint: reject unauthenticated requests, else attach identity.

    Registered in app/main.py via `app.add_middleware(..., dispatch=authenticate)`.
    """
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    api_key = request.headers.get("x-api-key")
    agent = API_KEYS.get(api_key)
    if agent is None:
        return JSONResponse(
            _rpc_error(None, -32001, "Missing or invalid API key"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    request.state.agent = agent
    return await call_next(request)
