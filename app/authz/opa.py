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

from ..interceptor import ToolCallContext

logger = logging.getLogger(__name__)

OPA_URL = os.getenv("OPA_URL", "http://localhost:8181/v1/data/authz")


@dataclass
class PolicyDecision:
    allow: bool
    require_approval: bool
    reason: str


async def evaluate_policy(context: ToolCallContext) -> PolicyDecision:
    """Evaluate a tool call against the OPA `authz` policy."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(OPA_URL, json={"input": context.to_opa_input()})
            response.raise_for_status()
        result = response.json().get("result", {})
    except (httpx.HTTPError, ValueError):
        logger.exception("OPA request failed for tool %s; denying by default", context.tool_name)
        return PolicyDecision(allow=False, require_approval=False, reason="policy engine unavailable")

    allow = bool(result.get("allow", False))
    require_approval = bool(result.get("require_approval", False))

    if require_approval:
        reason = f"'{context.tool_name}' is a destructive action in prod and requires approval"
    elif allow:
        reason = "allowed by OPA policy"
    else:
        reason = "denied by OPA policy"

    return PolicyDecision(allow=allow, require_approval=require_approval, reason=reason)
