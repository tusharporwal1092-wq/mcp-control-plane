"""Policy decision point.

Calls the OPA sidecar (see docker-compose.yaml `opa` service and
policies/authz.rego) over HTTP with `ToolCallContext.to_opa_input()` as
input, and reads back `allow` / `require_approval` from the `authz` package.
Fails **closed**: any OPA error (unreachable, timeout, bad response) denies
the call rather than falling back to an allow-all stub - unlike the rate
limiter, which fails open, an unavailable policy engine must not become an
authorization bypass.
"""
import logging
import os
from dataclasses import dataclass

import httpx
from opentelemetry.trace import Status, StatusCode

from ..interceptor import ToolCallContext
from ..otel import policy_denials_total, tracer

logger = logging.getLogger(__name__)

OPA_URL = os.getenv("OPA_URL", "http://localhost:8181/v1/data/authz")


@dataclass
class PolicyDecision:
    allow: bool
    require_approval: bool
    reason: str


async def evaluate_policy(context: ToolCallContext) -> PolicyDecision:
    """Evaluate a tool call against the OPA `authz` policy."""
    with tracer.start_as_current_span("policy_eval") as span:
        span.set_attribute("tool_name", context.tool_name)
        span.set_attribute("agent_id", context.agent_id)
        span.set_attribute("environment", context.environment)

        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.post(OPA_URL, json={"input": context.to_opa_input()})
                response.raise_for_status()
            result = response.json().get("result", {})
        except (httpx.HTTPError, ValueError) as exc:
            logger.exception("OPA request failed for tool %s; denying by default", context.tool_name)
            # Without this, the trace only ever shows policy_decision=deny -
            # indistinguishable from a real "denied by OPA policy" outcome.
            # record_exception + an Error status is what puts "ConnectTimeout"
            # (or whatever actually went wrong) on the span in Tempo.
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.set_attribute("policy_decision", "deny")
            policy_denials_total.add(1, {"tool_name": context.tool_name, "role": context.role})
            return PolicyDecision(allow=False, require_approval=False, reason="policy engine unavailable")

        allow = bool(result.get("allow", False))
        require_approval = bool(result.get("require_approval", False))

        if require_approval:
            reason = f"'{context.tool_name}' is a destructive action in prod and requires approval"
        elif allow:
            reason = "allowed by OPA policy"
        else:
            reason = "denied by OPA policy"
            policy_denials_total.add(1, {"tool_name": context.tool_name, "role": context.role})

        span.set_attribute(
            "policy_decision", "require_approval" if require_approval else ("allow" if allow else "deny")
        )
        return PolicyDecision(allow=allow, require_approval=require_approval, reason=reason)
