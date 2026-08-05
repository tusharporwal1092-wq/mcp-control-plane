"""Tool call interceptor.

Sits between the raw MCP `tools/call` JSON-RPC params and everything else
(policy engine, executor, audit log). Its job is to validate the request
shape once and produce a single `ToolCallContext` that every downstream
consumer can rely on - so the OPA input format, the audit log fields, and
the executor arguments all derive from the same normalized object instead
of each re-parsing the raw request.
"""
import time
from dataclasses import dataclass, field
from typing import Any


_ENVIRONMENT_PREFIXES = ("prod", "staging", "dev")


def _infer_environment(arguments: dict) -> str:
    """Best-effort environment tag for OPA, derived from the K8s namespace
    naming convention (e.g. "prod-payments" -> "prod"). Tools with no
    namespace argument (Jenkins, tickets, ...) fall through to "unknown"."""
    namespace = arguments.get("namespace") or ""
    for env in _ENVIRONMENT_PREFIXES:
        if namespace.startswith(env):
            return env
    return "unknown"


class MissingParamError(Exception):
    """Raised when a required field is absent from a tools/call request."""

    def __init__(self, field_name: str, message: str):
        self.field_name = field_name
        self.message = message
        super().__init__(message)


@dataclass
class ToolCallContext:
    tool_name: str
    arguments: dict[str, Any]
    agent_id: str
    role: str
    requested_at: float = field(default_factory=time.time)

    def to_opa_input(self) -> dict:
        """Shape expected by the OPA policy input document (policies/authz.rego)."""
        return {
            "agent": {"id": self.agent_id, "role": self.role},
            "tool": {"name": self.tool_name, "args": self.arguments},
            "resource": {"namespace": self.arguments.get("namespace")},
            "environment": _infer_environment(self.arguments),
        }


def intercept_tool_call(params: dict, agent) -> ToolCallContext:
    """Extract tool name, args, and agent context from a tools/call request before dispatch."""
    tool_name = params.get("name")
    if not tool_name:
        raise MissingParamError("name", "missing required field 'name'")

    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise MissingParamError("arguments", "'arguments' must be an object")

    return ToolCallContext(
        tool_name=tool_name,
        arguments=arguments,
        agent_id=str(agent.id),
        role=agent.role,
    )
