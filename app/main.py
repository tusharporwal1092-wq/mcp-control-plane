"""FastAPI application entrypoint.

This module is deliberately kept to HTTP concerns only: route handlers,
request/response shapes, and wiring the app together at startup. The actual
logic lives in reusable modules that can be tested and evolved on their own:

    app/middleware/auth.py        - resolves API key -> agent identity
    app/middleware/rate_limit.py  - Redis-backed per-agent rate limiting
    app/middleware/logging.py     - structured request/response logging
    app/interceptor.py            - validates + normalizes tools/call params
    app/authz/opa.py              - policy decision point (stub -> real OPA)
    app/redis_client.py           - shared Redis connection factory
    app/approvals.py              - Redis-backed pending-approval store (human-in-the-loop gate)
    app/slack.py                  - Slack notification + HMAC callback verification
    app/sse_hub.py                - in-process pub/sub: approval decision -> agent's open SSE connection
    app/db.py                     - shared asyncpg (Postgres) connection pool
    app/audit.py                  - hash-chained audit log: write / query / verify / export

Run locally with `uv run uvicorn app.main:app --reload` or
`uv run python -m app.main` from the project root (the `app.` prefix on
imports below requires the project root - not this file's own directory -
to be on the Python path, which is what running as a module gives you).
"""
import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import approvals, audit, slack, sse_hub
from .audit import AuditEvent, record_tool_call
from .authz.opa import evaluate_policy
from .db import create_db_pool
from .interceptor import MissingParamError, intercept_tool_call
from .middleware.auth import authenticate
from .middleware.logging import log_requests
from .middleware.rate_limit import RATE_LIMIT_WINDOW_SECONDS, SlidingWindowRateLimiter, rate_limit
from .redis_client import create_redis_client
from .tools import tools_spec as tools
from .tools.errors import ExecutorError

# error_type -> HTTP status, per docs/tool-spec.md's Error Response Format.
# executor_timeout gets its own 504 (the doc calls this out explicitly);
# everything else the executor raises deliberately is a 4xx client-shaped
# error, and anything unexpected (an unhandled exception, not ExecutorError)
# falls back to 500 "upstream_error" below since we don't know its shape.
_ERROR_TYPE_STATUS = {
    "validation_error": 400,
    "not_found": 404,
    "permission_denied": 403,
    "executor_timeout": 504,
    "upstream_error": 502,
}

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
    # Postgres pool for the audit log (app/audit.py) - every handler reads
    # it back off `request.app.state.db`.
    app.state.db = await create_db_pool()


@app.on_event("shutdown")
async def shutdown():
    await app.state.redis.aclose()
    await app.state.db.close()


TOOLS = {
    "list_pods": tools.list_pods,
    "get_pod_logs": tools.get_pod_logs,
    "get_deployment_status": tools.get_deployment_status,
    "restart_deployment": tools.restart_deployment,
    "scale_deployment": tools.scale_deployment,
    "exec_into_pod": tools.exec_into_pod,
    "apply_k8s_manifest": tools.apply_k8s_manifest,
    "query_terraform_plan": tools.query_terraform_plan,
    "trigger_jenkins_job": tools.trigger_jenkins_job,
    "get_jenkins_job_status": tools.get_jenkins_job_status,
    "read_prometheus_metrics": tools.read_prometheus_metrics,
    "open_ticket": tools.open_ticket,
    "read_ticket": tools.read_ticket,
}


@dataclass
class ExecutionOutcome:
    """Result of running a tool executor, before it's shaped into either a
    synchronous JSON-RPC response (`/mcp`) or an async SSE push (approval
    resume in `decide_approval`) - the two call sites this backs."""

    kind: str  # "success" | "executor_error" | "unhandled_error"
    result: dict | None = None
    error_type: str | None = None
    message: str | None = None


def _execute_tool(tool_name: str, arguments: dict) -> ExecutionOutcome:
    try:
        result = TOOLS[tool_name](arguments)
    except ExecutorError as exc:
        logger.warning("Executor error for tool %s: %s (%s)", tool_name, exc, exc.error_type)
        return ExecutionOutcome(kind="executor_error", error_type=exc.error_type, message=str(exc))
    except Exception as exc:
        logger.exception("Unhandled executor error for tool %s", tool_name)
        return ExecutionOutcome(kind="unhandled_error", message=str(exc))
    return ExecutionOutcome(kind="success", result=result)


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
    # Handler-entry timestamp, so every AuditEvent recorded below can carry
    # how long this request took (audit_log.duration_ms) - same
    # time.perf_counter() pattern app/middleware/logging.py already uses.
    start = time.perf_counter()

    def _duration_ms() -> int:
        return int((time.perf_counter() - start) * 1000)

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
                    {
                        "field": "name",
                        "error": f"agent '{agent.id}' is not allowed to call '{tool_name}'",
                        "policy_decision": "forbidden",
                    },
                ),
                status_code=403,
            )

        decision = await evaluate_policy(context)
        if decision.require_approval:
            approval = await approvals.create_pending_approval(
                request.app.state.redis,
                agent_id=context.agent_id,
                role=context.role,
                tool_name=tool_name,
                arguments=arguments,
                reason=decision.reason,
            )
            await slack.send_approval_request(approval)
            await record_tool_call(
                request.app.state.db,
                AuditEvent(
                    agent_id=context.agent_id,
                    role=context.role,
                    tool_name=tool_name,
                    arguments=arguments,
                    decision="pending_approval",
                    reason=decision.reason,
                    approval_id=approval.id,
                    duration_ms=_duration_ms(),
                ),
            )
            # Per docs/api-design.md S3.3: approval-pending is not a JSON-RPC
            # error - it's a result the agent should treat as "call is in
            # flight", with approval_id to poll or an open SSE connection to
            # wait on for the eventual approve/deny push (decide_approval below).
            return JSONResponse(
                _rpc_result(
                    rpc_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"This action requires human approval. "
                                    f"Approval request sent to Slack. Approval ID: {approval.id}"
                                ),
                            }
                        ],
                        "isError": False,
                        "_meta": {
                            "approval_id": approval.id,
                            "approval_status": "pending",
                            "expires_at": approval.expires_at,
                            "poll_url": f"/admin/approvals/{approval.id}/status",
                        },
                    },
                ),
                status_code=202,
            )

        if not decision.allow:
            await record_tool_call(
                request.app.state.db,
                AuditEvent(
                    agent_id=context.agent_id,
                    role=context.role,
                    tool_name=tool_name,
                    arguments=arguments,
                    decision="denied",
                    reason=decision.reason,
                    duration_ms=_duration_ms(),
                ),
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

        outcome = _execute_tool(tool_name, arguments)
        if outcome.kind == "executor_error":
            # Deliberate failure the executor already classified (bad args,
            # not found, timeout, ...) - map its error_type to the status
            # code docs/tool-spec.md specifies, and surface error_type in
            # the response so the calling agent can distinguish "retry me"
            # (executor_timeout) from "don't retry, fix the args" (validation_error).
            status_code = _ERROR_TYPE_STATUS[outcome.error_type]
            # executor_timeout gets its own audit decision (per doc: "audit
            # log entry is written with result_status: executor_timeout")
            # so it's distinguishable from an ordinary executor error.
            decision_label = "executor_timeout" if outcome.error_type == "executor_timeout" else "error"
            await record_tool_call(
                request.app.state.db,
                AuditEvent(
                    agent_id=context.agent_id,
                    role=context.role,
                    tool_name=tool_name,
                    arguments=arguments,
                    decision=decision_label,
                    reason=outcome.message,
                    duration_ms=_duration_ms(),
                ),
            )
            return JSONResponse(
                _rpc_error(
                    rpc_id,
                    -32603,
                    f"Executor error: {outcome.message}",
                    {"reason": outcome.message, "error_type": outcome.error_type, "policy_decision": "allow"},
                ),
                status_code=status_code,
            )
        if outcome.kind == "unhandled_error":
            await record_tool_call(
                request.app.state.db,
                AuditEvent(
                    agent_id=context.agent_id,
                    role=context.role,
                    tool_name=tool_name,
                    arguments=arguments,
                    decision="error",
                    reason=outcome.message,
                    duration_ms=_duration_ms(),
                ),
            )
            return JSONResponse(
                _rpc_error(
                    rpc_id,
                    -32603,
                    "Executor error",
                    {"reason": outcome.message, "error_type": "upstream_error", "policy_decision": "allow"},
                ),
                status_code=500,
            )

        await record_tool_call(
            request.app.state.db,
            AuditEvent(
                agent_id=context.agent_id,
                role=context.role,
                tool_name=tool_name,
                arguments=arguments,
                decision="allowed",
                reason=decision.reason,
                result=outcome.result,
                duration_ms=_duration_ms(),
            ),
        )
        return _rpc_result(rpc_id, outcome.result)

    return JSONResponse(
        _rpc_error(rpc_id, -32601, f"Method not found: {method}"), status_code=404
    )


class ApprovalDecisionRequest(BaseModel):
    decision: str  # "approve" | "deny"
    decided_by: str
    note: str | None = None


# API for the Slack interactive-callback target (docs/api-design.md S4.3).
# Auth is HMAC (verify_signature below), not the agent x-api-key middleware -
# see PUBLIC_PATH_PREFIXES in app/middleware/auth.py.
@app.post("/admin/approvals/{approval_id}/decide")
async def decide_approval(approval_id: str, request: Request):
    # Same duration-tracking pattern as the /mcp handler above - this
    # request's own AuditEvent rows (denied, or the resumed-executor result)
    # get a duration_ms too.
    start = time.perf_counter()
    #to calculate how long the approval request took to processs in ms.
    def _duration_ms() -> int:
        return int((time.perf_counter() - start) * 1000)

    body = await request.body()
    if not slack.verify_signature(
        request.headers.get("x-slack-request-timestamp"),
        body,
        request.headers.get("x-slack-signature"),
    ):
        return JSONResponse({"error": "invalid or missing Slack signature"}, status_code=401)

    try:
        payload = ApprovalDecisionRequest.model_validate_json(body)
    except ValidationError as exc:
        return JSONResponse({"error": f"invalid request body: {exc}"}, status_code=400)
    if payload.decision not in ("approve", "deny"):
        return JSONResponse({"error": "decision must be 'approve' or 'deny'"}, status_code=400)

    approval, outcome = await approvals.decide_approval(
        request.app.state.redis, approval_id, decision=payload.decision, decided_by=payload.decided_by
    )
    if outcome == "not_found":
        return JSONResponse({"error": "approval not found or expired"}, status_code=410)
    if outcome == "already_decided":
        return JSONResponse({"error": "approval already decided", "status": approval.status}, status_code=409)

    if approval.status == "denied":
        reason = f"denied by {payload.decided_by}"
        if payload.note:
            reason += f": {payload.note}"
        await record_tool_call(
            request.app.state.db,
            AuditEvent(
                agent_id=approval.agent_id,
                role=approval.role,
                tool_name=approval.tool_name,
                arguments=approval.arguments,
                decision="approval_denied",
                reason=reason,
                approval_id=approval.id,
                duration_ms=_duration_ms(),
            ),
        )
        await sse_hub.publish(
            approval.agent_id,
            {"approval_id": approval.id, "tool_name": approval.tool_name, "status": "denied", "reason": reason},
        )
        return {"approval_id": approval.id, "status": "denied"}

    # Approved: resume the tool call - run the executor now and log the result.
    exec_outcome = _execute_tool(approval.tool_name, approval.arguments)
    if exec_outcome.kind == "success":
        await record_tool_call(
            request.app.state.db,
            AuditEvent(
                agent_id=approval.agent_id,
                role=approval.role,
                tool_name=approval.tool_name,
                arguments=approval.arguments,
                decision="allowed",
                reason=f"approved by {payload.decided_by}",
                result=exec_outcome.result,
                approval_id=approval.id,
                duration_ms=_duration_ms(),
            ),
        )
        await sse_hub.publish(
            approval.agent_id,
            {
                "approval_id": approval.id,
                "tool_name": approval.tool_name,
                "status": "approved",
                "result": exec_outcome.result,
            },
        )
    else:
        decision_label = "executor_timeout" if exec_outcome.error_type == "executor_timeout" else "error"
        await record_tool_call(
            request.app.state.db,
            AuditEvent(
                agent_id=approval.agent_id,
                role=approval.role,
                tool_name=approval.tool_name,
                arguments=approval.arguments,
                decision=decision_label,
                reason=exec_outcome.message,
                approval_id=approval.id,
                duration_ms=_duration_ms(),
            ),
        )
        await sse_hub.publish(
            approval.agent_id,
            {
                "approval_id": approval.id,
                "tool_name": approval.tool_name,
                "status": "error",
                "error": exec_outcome.message,
            },
        )

    return {"approval_id": approval.id, "status": "approved"}


@app.get("/admin/approvals/{approval_id}/status")
async def approval_status(approval_id: str, request: Request):
    approval = await approvals.get_approval(request.app.state.redis, approval_id)
    if approval is None:
        return JSONResponse({"error": "approval not found or expired"}, status_code=410)
    return asdict(approval)


# Admin audit log query + export (docs/api-design.md S4.2). Same auth gap as
# /admin/approvals/*: no admin JWT layer exists in this codebase yet (see the
# comment on PUBLIC_PATH_PREFIXES in app/middleware/auth.py), so these two
# routes are exempted from the agent x-api-key middleware there too, rather
# than either requiring an agent key (wrong audience) or silently pretending
# JWT auth is in place.
@app.get("/admin/audit")
async def get_audit_log(
    request: Request,
    agent_id: str | None = None,
    tool: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    result_status: str | None = None,
    limit: int = audit.DEFAULT_QUERY_LIMIT,
    offset: int = 0,
):
    # Query params are plain strings over the wire; parse the two date
    # filters to datetimes here so app/audit.py's functions only ever deal
    # in real datetime objects, never ISO strings.
    total, rows, integrity_check, first_broken_seq = await audit.query_audit_log(
        request.app.state.db,
        agent_id=agent_id,
        tool=tool,
        from_=datetime.fromisoformat(from_) if from_ else None,
        to=datetime.fromisoformat(to) if to else None,
        result_status=result_status,
        limit=limit,
        offset=offset,
    )
    response = {
        "total": total,
        "rows": [audit.row_to_json(row) for row in rows],
        "integrity_check": integrity_check,
    }
    # first_broken_seq only makes sense (and is only present) when the
    # chain actually failed - omitted entirely on a clean "pass".
    if integrity_check == "fail":
        response["first_broken_seq"] = first_broken_seq
    return response


# `from`/`to` are required (Query(...)) - unlike the query endpoint above,
# there's no sensible default range for a bulk export.
@app.get("/admin/audit/export")
async def export_audit_log_endpoint(
    request: Request,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    from_dt = datetime.fromisoformat(from_)
    to_dt = datetime.fromisoformat(to)

    # StreamingResponse pulls from this generator chunk by chunk, so rows
    # are sent to the client as they come off the DB cursor rather than
    # all being buffered in memory first (see app/audit.py::export_audit_log).
    async def ndjson():
        async for row in audit.export_audit_log(request.app.state.db, from_=from_dt, to=to_dt):
            yield json.dumps(row) + "\n"

    return StreamingResponse(ndjson(), media_type="application/x-ndjson")


# API for SSE communication. Doubles as the approval-result channel: while
# this connection is open, a decision made via decide_approval() above is
# pushed here instead of requiring the agent to poll /admin/approvals/{id}/status.
@app.get("/mcp/sse")
async def sse(request: Request):
    agent = request.state.agent

    async def event_generator():
        async with sse_hub.subscribe(str(agent.id)) as queue:
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield {"event": "approval_result", "data": json.dumps(event)}
                except asyncio.TimeoutError:
                    yield {"event": "message", "data": "heartbeat"}

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
