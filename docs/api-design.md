# MCP Control Plane — API Design

## 1. Overview

The MCP Control Plane exposes two API surfaces:

1. **MCP Protocol API** — the interface AI agents use to call tools, following the MCP JSON-RPC 2.0 specification.
2. **Control Plane Admin API** — a REST API for managing agents, viewing audit logs, and managing approvals. Not exposed to LLM clients.

Base URL (production): `https://mcp.internal.example.com`

---

## 2. Authentication

### 2.1 Agent Authentication (MCP API)

All MCP requests must include an API key in the `Authorization` header:

```
Authorization: Bearer <agent-api-key>
```

API keys are issued per agent identity and stored in AWS Secrets Manager. Each key resolves to:

```json
{
  "agent_id": "sre-agent-prod-01",
  "role": "sre",
  "environment_scope": "prod",
  "allowed_tools": ["get_pod_logs", "read_prometheus_metrics", "read_ticket"],
  "rate_limit_rpm": 60
}
```

Keys are rotated via the Admin API. Old keys have a 1-hour grace period after rotation.

### 2.2 Admin API Authentication

Admin endpoints require a separate short-lived JWT issued by an internal IdP (Okta / AWS Cognito). Admins authenticate out-of-band; the JWT is passed in `Authorization: Bearer <jwt>`.

Admin JWTs expire after 1 hour and cannot be refreshed — reauthentication is required.

---

## 3. MCP Protocol API

The gateway implements the MCP specification. All MCP endpoints are under `/mcp`.

### 3.1 Transport

| Transport | Endpoint | Use case |
|-----------|----------|----------|
| HTTP POST | `POST /mcp` | Single request-response tool calls |
| SSE | `GET /mcp/sse` | Streaming results, long-running tools, approval-pending callbacks |

### 3.2 Tool Listing

**Request:**
```
POST /mcp
Content-Type: application/json
Authorization: Bearer <key>

{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "tools/list",
  "params": {}
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "tools": [
      {
        "name": "get_pod_logs",
        "description": "Retrieve recent logs for a specific Kubernetes pod.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "namespace": { "type": "string" },
            "pod_name": { "type": "string" },
            "tail_lines": { "type": "integer", "default": 100 }
          },
          "required": ["namespace", "pod_name"]
        }
      }
    ]
  }
}
```

The tool list returned is filtered per agent: only tools in the agent's `allowed_tools` list are shown. This prevents tool enumeration by unauthorized agents.

### 3.3 Tool Call

**Request:**
```
POST /mcp
Content-Type: application/json
Authorization: Bearer <key>

{
  "jsonrpc": "2.0",
  "id": "42",
  "method": "tools/call",
  "params": {
    "name": "get_pod_logs",
    "arguments": {
      "namespace": "payments",
      "pod_name": "checkout-api-7d9f4b-xkp2q",
      "tail_lines": 200
    }
  }
}
```

**Response — Success:**
```json
{
  "jsonrpc": "2.0",
  "id": "42",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "2026-07-24T10:15:01Z INFO  Request received path=/checkout\n2026-07-24T10:15:01Z INFO  Payment processed order_id=ord-9182 ..."
      }
    ],
    "isError": false,
    "_meta": {
      "audit_id": "a3f12c9d-...",
      "policy_decision": "allow",
      "duration_ms": 234
    }
  }
}
```

**Response — Policy Denied:**
```json
{
  "jsonrpc": "2.0",
  "id": "42",
  "error": {
    "code": -32603,
    "message": "Tool call denied by policy",
    "data": {
      "reason": "restart_deployment is not permitted in environment=prod for role=readonly",
      "audit_id": "b9c44d1e-...",
      "policy_decision": "deny"
    }
  }
}
```

**Response — Approval Required (async flow):**
```json
{
  "jsonrpc": "2.0",
  "id": "42",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "This action requires human approval. Approval request sent to #sre-approvals in Slack. Approval ID: appr-7f3a..."
      }
    ],
    "isError": false,
    "_meta": {
      "approval_id": "appr-7f3a2b9c-...",
      "approval_status": "pending",
      "expires_at": "2026-07-24T10:30:00Z",
      "poll_url": "/admin/approvals/appr-7f3a2b9c-.../status"
    }
  }
}
```

The agent may poll `GET /admin/approvals/{approval_id}/status` or listen on the SSE stream for the completion event.

### 3.4 Error Codes

| HTTP Status | JSON-RPC Code | Meaning |
|-------------|--------------|---------|
| 401 | -32001 | Missing or invalid API key |
| 403 | -32003 | Tool not in agent's allowed_tools list |
| 429 | -32029 | Rate limit exceeded |
| 403 | -32603 | Policy denied |
| 202 | — | Approval pending (not an error) |
| 500 | -32603 | Executor error (downstream API failure) |
| 504 | -32603 | Executor timeout |

---

## 4. Admin REST API

All admin endpoints require `Authorization: Bearer <jwt>`.

### 4.1 Agent Management

#### List agents
```
GET /admin/agents
```
```json
{
  "agents": [
    {
      "agent_id": "sre-agent-prod-01",
      "role": "sre",
      "environment_scope": "prod",
      "allowed_tools": ["get_pod_logs", "read_prometheus_metrics"],
      "rate_limit_rpm": 60,
      "created_at": "2026-07-01T00:00:00Z",
      "last_used_at": "2026-07-24T10:14:55Z"
    }
  ]
}
```

#### Create agent
```
POST /admin/agents
Content-Type: application/json

{
  "agent_id": "sre-agent-staging-01",
  "role": "sre",
  "environment_scope": "staging",
  "allowed_tools": ["get_pod_logs", "restart_deployment", "read_prometheus_metrics"],
  "rate_limit_rpm": 120
}
```
**Response:** `201 Created` with `{ "api_key": "<generated-key>" }` — key shown once, then hashed.

#### Rotate agent key
```
POST /admin/agents/{agent_id}/rotate-key
```
**Response:** `200 OK` with new `api_key`. Old key valid for 1 hour.

#### Delete agent
```
DELETE /admin/agents/{agent_id}
```
Immediately revokes all keys for that agent. In-flight requests are rejected.

### 4.2 Audit Log

#### Query audit log
```
GET /admin/audit?agent_id=sre-agent-prod-01&tool=restart_deployment&from=2026-07-24T00:00:00Z&to=2026-07-24T23:59:59Z&result_status=denied&limit=50&offset=0
```

Query parameters:

| Parameter | Type | Description |
|-----------|------|-------------|
| `agent_id` | string | Filter by agent |
| `tool` | string | Filter by tool name |
| `from` | ISO 8601 | Start of time range |
| `to` | ISO 8601 | End of time range |
| `result_status` | string | `success`, `denied`, `error` |
| `limit` | int | Max rows (default 50, max 500) |
| `offset` | int | Pagination offset |

**Response:**
```json
{
  "total": 3,
  "rows": [
    {
      "id": "a3f12c9d-...",
      "seq": 10041,
      "agent_id": "sre-agent-prod-01",
      "tool_name": "restart_deployment",
      "args": { "namespace": "payments", "deployment": "checkout-api" },
      "policy_decision": { "allow": false, "reason": "prod requires approval" },
      "approval_id": null,
      "result_status": "denied",
      "result_summary": null,
      "duration_ms": 12,
      "otel_trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
      "created_at": "2026-07-24T10:15:03Z"
    }
  ],
  "integrity_check": "pass"
}
```

The `integrity_check` field re-validates the SHA-256 hash chain across the returned rows. If any row has been tampered with, it returns `"fail"` with the first broken `seq`.

#### Export audit log (for compliance)
```
GET /admin/audit/export?from=2026-07-01T00:00:00Z&to=2026-07-31T23:59:59Z
Accept: application/x-ndjson
```
Returns newline-delimited JSON, streaming. Includes all fields plus `row_hash` for offline chain verification.

### 4.3 Approval Management

#### Get approval status
```
GET /admin/approvals/{approval_id}/status
```
```json
{
  "approval_id": "appr-7f3a2b9c-...",
  "agent_id": "sre-agent-prod-01",
  "tool_name": "restart_deployment",
  "args": { "namespace": "payments", "deployment": "checkout-api" },
  "status": "pending",
  "requested_at": "2026-07-24T10:15:00Z",
  "expires_at": "2026-07-24T10:30:00Z",
  "decided_by": null,
  "decided_at": null
}
```

#### Approve or deny (webhook target for Slack)
```
POST /admin/approvals/{approval_id}/decide
Content-Type: application/json

{
  "decision": "approve",
  "decided_by": "alice@example.com",
  "note": "Approved for emergency hotfix"
}
```

This endpoint is called by the Slack interactive component callback. It is protected by a shared HMAC secret verified against the `X-Slack-Signature` header.

**Response:**
- `200 OK` — decision recorded, tool call proceeds or is cancelled
- `410 Gone` — approval expired (TTL elapsed)
- `409 Conflict` — already decided

### 4.4 Health & Readiness

```
GET /health/live    → 200 OK {"status": "ok"}
GET /health/ready   → 200 OK {"status": "ok", "checks": {"opa": "ok", "redis": "ok", "db": "ok"}}
```

`/health/ready` is used by the EKS readiness probe. If OPA, Redis, or the database are unreachable, it returns `503` and the pod is removed from the ALB target group.

---

## 5. Rate Limiting Details

Rate limiting is enforced at the gateway using a Redis sliding window (1-minute window).

- Counter key: `ratelimit:{agent_id}`
- On each tool call: `INCR` + `EXPIRE` (if new key)
- On breach: return `429` with headers:

```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1753348560
Retry-After: 23
```

Rate limits are configurable per agent (set at agent creation). A global hard cap of 300 RPM exists regardless of per-agent config, to protect downstream systems.

---

## 6. Request Validation

All tool arguments are validated against the tool's JSON Schema before OPA evaluation. Invalid arguments return immediately without touching OPA or the executor:

```json
{
  "jsonrpc": "2.0",
  "id": "42",
  "error": {
    "code": -32602,
    "message": "Invalid params",
    "data": {
      "field": "tail_lines",
      "error": "must be a positive integer, got -1"
    }
  }
}
```

This prevents garbage data from reaching downstream APIs and avoids unnecessary OPA evaluations.

---

## 7. Versioning

The MCP protocol version is negotiated during session initialization per the MCP spec (`initialize` method). The Admin API is versioned with a URL prefix: `/v1/admin/...`. Breaking changes bump the version; old versions remain live for 90 days.

---

## 8. OpenAPI Spec

The Admin API is documented via FastAPI's auto-generated OpenAPI 3.1 spec, available at:

```
GET /admin/openapi.json    (JSON)
GET /admin/docs            (Swagger UI, internal network only)
```

The MCP protocol endpoints are described by the MCP specification itself and do not emit an OpenAPI document.
