"""FastAPI application entrypoint.

This module is deliberately kept to HTTP concerns only: route handlers,
request/response shapes, and wiring the app together at startup. The actual
logic lives in reusable modules that can be tested and evolved on their own:

    app/middleware/auth.py        - resolves API key -> agent identity
    app/middleware/rate_limit.py  - Redis-backed per-agent rate limiting
    app/middleware/logging.py     - structured request/response logging
    app/interceptor.py            - validates + normalizes tools/call params
    app/authz/opa.py              - policy decision point (stub -> real OPA)
    app/audit.py                  - audit trail of every tool call
    app/redis_client.py           - shared Redis connection factory

Run locally with `uv run uvicorn app.main:app --reload` or
`uv run python -m app.main` from the project root (the `app.` prefix on
imports below requires the project root - not this file's own directory -
to be on the Python path, which is what running as a module gives you).
"""
import asyncio
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .audit import AuditEvent, record_tool_call
from .authz.opa import evaluate_policy
from .interceptor import MissingParamError, intercept_tool_call
from .middleware.auth import authenticate
from .middleware.logging import log_requests
from .middleware.rate_limit import RATE_LIMIT_WINDOW_SECONDS, SlidingWindowRateLimiter, rate_limit
from .redis_client import create_redis_client
from .tools import tools_spec as tools

# INFO is the level app/middleware/logging.py and app/audit.py log at; without
# this, Python's default root level (WARNING) would silently swallow them.
logging.basicConfig(level=logging.INFO)

# Create the server instance.
app = FastAPI()

# For logging the request.
logger = logging.getLogger(__name__)


@app.on_event("startup")
async def startup():
    app.state.redis = create_redis_client()
    app.state.rate_limiter = SlidingWindowRateLimiter(
        app.state.redis, window_seconds=RATE_LIMIT_WINDOW_SECONDS
    )


@app.on_event("shutdown")
async def shutdown():
    await app.state.redis.aclose()


TOOLS = {
    "list_pods": tools.list_pods,
    "get_pod_logs": tools.get_pod_logs,
    "get_deployment_status": tools.get_deployment_status,
    "restart_deployment": tools.restart_deployment,
    "scale_deployment": tools.scale_deployment,
    "query_terraform_plan": tools.query_terraform_plan,
    "trigger_jenkins_job": tools.trigger_jenkins_job,
    "get_jenkins_job_status": tools.get_jenkins_job_status,
    "read_prometheus_metrics": tools.read_prometheus_metrics,
    "open_ticket": tools.open_ticket,
    "read_ticket": tools.read_ticket,
}


# Make a class for tool calling format.
class ToolCallParams(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


# Make a class for the request format.
class MCPRequest(BaseModel):
    jsonrpc: str
    id: int | str | None = None
    method: str
    params: ToolCallParams | None = None


# Function for returning the success message response.
def _rpc_result(rpc_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


# Function for returning the error message response.
def _rpc_error(rpc_id, code: int, message: str, data: dict | None = None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


# API config for /mcp.
@app.post("/mcp")
async def mcp(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)

    if not isinstance(body, dict):
        return JSONResponse(_rpc_error(None, -32600, "Invalid Request"), status_code=400)

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if not method:
        return JSONResponse(
            _rpc_error(rpc_id, -32600, "Invalid Request: missing method"),
            status_code=400,
        )

    # Set by middleware/auth.py's `authenticate` middleware.
    agent = request.state.agent

    if method == "tools/list":
        allowed = [name for name in TOOLS if name in agent.allowed_tools]
        return _rpc_result(rpc_id, {"tools": [{"name": name} for name in allowed]})

    if method == "tools/call":
        try:
            context = intercept_tool_call(params, agent)
        except MissingParamError as exc:
            return JSONResponse(
                _rpc_error(
                    rpc_id,
                    -32602,
                    "Invalid params",
                    {"field": exc.field_name, "error": exc.message},
                ),
                status_code=400,
            )

        tool_name = context.tool_name
        arguments = context.arguments

        if tool_name not in TOOLS:
            return JSONResponse(
                _rpc_error(
                    rpc_id,
                    -32602,
                    "Invalid params",
                    {"field": "name", "error": f"unknown tool '{tool_name}'"},
                ),
                status_code=400,
            )

        if tool_name not in agent.allowed_tools:
            return JSONResponse(
                _rpc_error(
                    rpc_id,
                    -32003,
                    "Forbidden",
                    {"field": "name", "error": f"agent '{agent.id}' is not allowed to call '{tool_name}'"},
                ),
                status_code=403,
            )

        decision = await evaluate_policy(context)
        if decision.require_approval:
            record_tool_call(
                AuditEvent(
                    agent_id=context.agent_id,
                    role=context.role,
                    tool_name=tool_name,
                    arguments=arguments,
                    decision="pending_approval",
                    reason=decision.reason,
                )
            )
            return JSONResponse(
                _rpc_error(
                    rpc_id,
                    -32010,
                    "Approval required",
                    {"reason": decision.reason, "policy_decision": "require_approval"},
                ),
                status_code=403,
            )

        if not decision.allow:
            record_tool_call(
                AuditEvent(
                    agent_id=context.agent_id,
                    role=context.role,
                    tool_name=tool_name,
                    arguments=arguments,
                    decision="denied",
                    reason=decision.reason,
                )
            )
            return JSONResponse(
                _rpc_error(
                    rpc_id,
                    -32603,
                    "Tool call denied by policy",
                    {"reason": decision.reason, "policy_decision": "deny"},
                ),
                status_code=403,
            )

        try:
            result = TOOLS[tool_name](arguments)
        except Exception as exc:
            logger.exception("Executor error for tool %s", tool_name)
            record_tool_call(
                AuditEvent(
                    agent_id=context.agent_id,
                    role=context.role,
                    tool_name=tool_name,
                    arguments=arguments,
                    decision="error",
                    reason=str(exc),
                )
            )
            return JSONResponse(
                _rpc_error(rpc_id, -32603, "Executor error", {"reason": str(exc)}),
                status_code=500,
            )

        record_tool_call(
            AuditEvent(
                agent_id=context.agent_id,
                role=context.role,
                tool_name=tool_name,
                arguments=arguments,
                decision="allowed",
                reason=decision.reason,
            )
        )
        return _rpc_result(rpc_id, result)

    return JSONResponse(
        _rpc_error(rpc_id, -32601, f"Method not found: {method}"), status_code=404
    )


# API for SSE communication.
@app.get("/mcp/sse")
async def sse(request: Request):
    async def event_generator():
        while not await request.is_disconnected():
            yield {"event": "message", "data": "heartbeat"}
            await asyncio.sleep(5)

    return EventSourceResponse(event_generator())


# Health endpoints (no auth required, used by liveness/readiness probes).
@app.get("/health/live")
async def health_live():
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    return {"status": "ok"}


# Middleware stack. Starlette runs the *last-added* middleware first (it
# wraps the others), so this registration order produces, per request:
#   log_requests -> authenticate -> rate_limit -> route handler
# i.e. every request is logged (even 401s/429s), auth runs before rate
# limiting so `request.state.agent` exists when rate_limit reads it, and
# rate limiting only ever reaches an authenticated caller.
app.add_middleware(BaseHTTPMiddleware, dispatch=rate_limit)
app.add_middleware(BaseHTTPMiddleware, dispatch=authenticate)
app.add_middleware(BaseHTTPMiddleware, dispatch=log_requests)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
