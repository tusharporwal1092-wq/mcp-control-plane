# MCP Control Plane — Tool Specifications

## Overview

The MCP Control Plane exposes 10 tools across four domains: Kubernetes, Terraform, Jenkins, and Observability/Incident Management. Each tool section describes its purpose, input schema, output schema, policy constraints, audit fields, rate limit behavior, and failure modes.

All tools follow the MCP tool-call convention: input via `arguments` object, output via `content` array. Tools never return raw credentials, internal stack traces, or data outside their defined scope.

---

## Tool Permission Matrix

| Tool | sre | oncall | readonly | deploy-bot | destroy-requires-approval |
|------|-----|--------|----------|------------|--------------------------|
| `get_pod_logs` | ✓ | ✓ | ✓ | — | No |
| `list_pods` | ✓ | ✓ | ✓ | — | No |
| `get_deployment_status` | ✓ | ✓ | ✓ | ✓ | No |
| `restart_deployment` | staging only | staging only | — | staging only | **prod: yes** |
| `scale_deployment` | staging only | — | — | staging only | **prod: yes** |
| `query_terraform_plan` | ✓ | ✓ | ✓ | ✓ | No |
| `trigger_jenkins_job` | ✓ | — | — | ✓ | **prod: yes** |
| `get_jenkins_job_status` | ✓ | ✓ | ✓ | ✓ | No |
| `read_prometheus_metrics` | ✓ | ✓ | ✓ | — | No |
| `open_ticket` | ✓ | ✓ | — | — | No |
| `read_ticket` | ✓ | ✓ | ✓ | — | No |

---

## Kubernetes Tools

### Tool: `get_pod_logs`

**Purpose:** Retrieve recent stdout/stderr logs from a specific Kubernetes pod. The primary tool for diagnosing service errors, crash loops, and unexpected output.

**Executor:** K8s Python client, `CoreV1Api.read_namespaced_pod_log`
**IRSA Role:** `k8s-reader-role` (read-only: `get`, `list` on pods and pod/log)

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "namespace": {
      "type": "string",
      "description": "Kubernetes namespace the pod lives in.",
      "enum": ["payments", "orders", "auth", "notifications", "infra"]
    },
    "pod_name": {
      "type": "string",
      "description": "Full pod name (e.g. checkout-api-7d9f4b-xkp2q)."
    },
    "container": {
      "type": "string",
      "description": "Container name within the pod. Omit if the pod has a single container."
    },
    "tail_lines": {
      "type": "integer",
      "description": "Number of log lines to return from the end. Default 100, max 2000.",
      "default": 100,
      "minimum": 1,
      "maximum": 2000
    },
    "since_seconds": {
      "type": "integer",
      "description": "Return logs from the last N seconds. Mutually exclusive with tail_lines.",
      "minimum": 1,
      "maximum": 3600
    }
  },
  "required": ["namespace", "pod_name"],
  "additionalProperties": false
}
```

**Output:**
```
2026-07-24T10:15:01Z INFO  Request received path=/checkout
2026-07-24T10:15:01Z ERROR Failed to connect to DB: connection refused
...
```
Returned as a single `text/plain` content block. If the pod doesn't exist or is in a namespace not on the allowlist, returns a structured error.

**Policy Constraints:**
- `namespace` must be in the role's allowed namespace list (enforced in OPA).
- `tail_lines` > 2000 is rejected at schema validation before OPA.
- No approval required for any environment.

**Audit Log Fields:**
```json
{
  "tool": "get_pod_logs",
  "args": { "namespace": "payments", "pod_name": "checkout-api-7d9f4b-xkp2q", "tail_lines": 100 },
  "result_status": "success",
  "result_summary": { "lines_returned": 100 }
}
```

**Failure Modes:**
- Pod not found → `404` error returned with message
- Pod in `Pending` state (no logs yet) → empty result with note
- K8s API timeout (>5s) → `504` error; logged as `executor_timeout`

---

### Tool: `list_pods`

**Purpose:** List all pods in a namespace with their current status, restart count, and age. Used to identify crash-looping pods, pending pods, or recently restarted pods.

**Executor:** K8s Python client, `CoreV1Api.list_namespaced_pod`
**IRSA Role:** `k8s-reader-role`

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "namespace": {
      "type": "string",
      "enum": ["payments", "orders", "auth", "notifications", "infra"]
    },
    "label_selector": {
      "type": "string",
      "description": "Kubernetes label selector to filter pods (e.g. 'app=checkout-api')."
    },
    "field_selector": {
      "type": "string",
      "description": "Field selector (e.g. 'status.phase=Running')."
    }
  },
  "required": ["namespace"],
  "additionalProperties": false
}
```

**Output (text table):**
```
NAME                          READY   STATUS    RESTARTS   AGE
checkout-api-7d9f4b-xkp2q    1/1     Running   0          2d
checkout-api-7d9f4b-mn9kl     0/1     Error     14         45m
payment-svc-abc123-p7r2x      1/1     Running   0          2d
```

**Policy Constraints:** Same namespace allowlist as `get_pod_logs`. No approval required.

---

### Tool: `get_deployment_status`

**Purpose:** Retrieve the current status of a Kubernetes Deployment: desired vs ready replicas, image version, last rollout conditions, and recent events.

**Executor:** K8s Python client, `AppsV1Api.read_namespaced_deployment`
**IRSA Role:** `k8s-reader-role`

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "namespace": {
      "type": "string",
      "enum": ["payments", "orders", "auth", "notifications", "infra"]
    },
    "deployment": {
      "type": "string",
      "description": "Deployment name (e.g. 'checkout-api')."
    }
  },
  "required": ["namespace", "deployment"],
  "additionalProperties": false
}
```

**Output:**
```json
{
  "name": "checkout-api",
  "namespace": "payments",
  "desired_replicas": 3,
  "ready_replicas": 2,
  "available_replicas": 2,
  "image": "123456789.dkr.ecr.us-east-1.amazonaws.com/checkout-api:v1.4.2",
  "conditions": [
    { "type": "Available", "status": "True", "message": "Deployment has minimum availability." },
    { "type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable" }
  ],
  "last_updated": "2026-07-24T09:30:00Z"
}
```

**Policy Constraints:** Read-only; no approval required in any environment.

---

### Tool: `restart_deployment`

**Purpose:** Perform a rolling restart of a Kubernetes Deployment by patching its `restartedAt` annotation. Equivalent to `kubectl rollout restart`.

**Executor:** K8s Python client, `AppsV1Api.patch_namespaced_deployment`
**IRSA Role:** `k8s-writer-role` (patch on deployments) — bound only in staging by default. Prod requires policy override + approval.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "namespace": {
      "type": "string",
      "enum": ["payments", "orders", "auth", "notifications", "infra"]
    },
    "deployment": {
      "type": "string",
      "description": "Deployment name to restart."
    },
    "reason": {
      "type": "string",
      "description": "Human-readable reason for the restart. Required. Captured in audit log.",
      "minLength": 10,
      "maxLength": 500
    }
  },
  "required": ["namespace", "deployment", "reason"],
  "additionalProperties": false
}
```

**Output:**
```json
{
  "status": "restart_initiated",
  "deployment": "checkout-api",
  "namespace": "payments",
  "restarted_at": "2026-07-24T10:20:00Z",
  "message": "Rolling restart triggered. Monitor with get_deployment_status."
}
```

**Policy Constraints:**
- Allowed in staging for roles: `sre`, `deploy-bot`.
- Allowed in prod only with `require_approval: true` (human must approve via Slack).
- `reason` field is required and must be >= 10 characters.
- OPA rule: `restart_deployment` is never allowed in `kube-system` namespace regardless of role.

**Approval Flow:** If environment=prod, policy returns `require_approval: true`. The call is held, Slack message is sent with deployment details, and the executor only proceeds after human approval.

**Audit Log Fields:**
```json
{
  "tool": "restart_deployment",
  "args": { "namespace": "payments", "deployment": "checkout-api", "reason": "Fixing connection pool exhaustion" },
  "approval_id": "appr-7f3a...",
  "result_status": "success"
}
```

---

### Tool: `scale_deployment`

**Purpose:** Change the replica count of a Kubernetes Deployment. Used for scaling up under load or scaling down for cost optimization.

**Executor:** K8s Python client, `AppsV1Api.patch_namespaced_deployment_scale`
**IRSA Role:** `k8s-writer-role`

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "namespace": {
      "type": "string",
      "enum": ["payments", "orders", "auth", "notifications", "infra"]
    },
    "deployment": {
      "type": "string"
    },
    "replicas": {
      "type": "integer",
      "description": "Target replica count. Min 0, max 20.",
      "minimum": 0,
      "maximum": 20
    },
    "reason": {
      "type": "string",
      "minLength": 10,
      "maxLength": 500
    }
  },
  "required": ["namespace", "deployment", "replicas", "reason"],
  "additionalProperties": false
}
```

**Policy Constraints:**
- `replicas: 0` always requires approval (scales a deployment to zero = full outage).
- Scaling above 10 replicas in staging requires approval.
- All prod scaling requires approval.
- OPA rule: `scale_deployment` blocked in `kube-system`.

---

## Terraform Tools

### Tool: `query_terraform_plan`

**Purpose:** Retrieve the latest Terraform Cloud plan for a given workspace, including a summary of resources to add, change, and destroy. Read-only — does not trigger a new plan.

**Executor:** Terraform Cloud API (HTTPS), `GET /api/v2/workspaces/{workspace_id}/runs?filter[status]=planned`
**Credentials:** Terraform Cloud read-only team token, fetched from Secrets Manager at call time.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "workspace": {
      "type": "string",
      "description": "Terraform Cloud workspace name (e.g. 'mcp-control-plane-prod').",
      "enum": ["mcp-control-plane-staging", "mcp-control-plane-prod", "eks-cluster-prod", "rds-prod"]
    },
    "include_full_plan": {
      "type": "boolean",
      "description": "If true, return the full plan JSON (may be large). Default false returns summary only.",
      "default": false
    }
  },
  "required": ["workspace"],
  "additionalProperties": false
}
```

**Output (summary):**
```json
{
  "workspace": "mcp-control-plane-prod",
  "run_id": "run-abc123",
  "status": "planned",
  "created_at": "2026-07-24T09:45:00Z",
  "triggered_by": "github-actions",
  "changes": {
    "add": 0,
    "change": 2,
    "destroy": 0
  },
  "changed_resources": [
    { "type": "aws_ecs_service", "name": "mcp-gateway", "action": "update" },
    { "type": "aws_cloudwatch_metric_alarm", "name": "denial-rate", "action": "update" }
  ]
}
```

**Policy Constraints:**
- Read-only; no approval required in any environment.
- `include_full_plan: true` is allowed only for roles `sre` and `deploy-bot` (full plan JSON can be large and contain sensitive resource ARNs).

---

## Jenkins Tools

### Tool: `trigger_jenkins_job`

**Purpose:** Trigger a parameterized Jenkins pipeline job (e.g., a deploy pipeline, a smoke test suite, a DB migration job).

**Executor:** Jenkins REST API, `POST /job/{job_name}/buildWithParameters`
**Credentials:** Jenkins API token (per-environment), fetched from Secrets Manager.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "job_name": {
      "type": "string",
      "description": "Jenkins job name (full path, e.g. 'deploy/checkout-api').",
      "enum": [
        "deploy/checkout-api",
        "deploy/payment-svc",
        "test/smoke-staging",
        "test/smoke-prod",
        "maintenance/db-backup"
      ]
    },
    "parameters": {
      "type": "object",
      "description": "Key-value pairs of job parameters. Only parameters defined in the job's allowed_params list are accepted.",
      "additionalProperties": { "type": "string" }
    },
    "reason": {
      "type": "string",
      "description": "Reason for triggering (captured in audit log and Jenkins build description).",
      "minLength": 10,
      "maxLength": 500
    }
  },
  "required": ["job_name", "reason"],
  "additionalProperties": false
}
```

**Output:**
```json
{
  "status": "triggered",
  "job_name": "test/smoke-staging",
  "build_number": null,
  "queue_item_url": "https://jenkins.internal/queue/item/1234/",
  "message": "Build queued. Use get_jenkins_job_status to poll for completion."
}
```

**Policy Constraints:**
- Jobs with `deploy/` prefix require approval in prod.
- Parameters are validated against a per-job allowlist in OPA (prevents injection of unexpected build parameters).
- `maintenance/` jobs always require approval regardless of environment.

---

### Tool: `get_jenkins_job_status`

**Purpose:** Get the status of a Jenkins job's latest build (or a specific build number).

**Executor:** Jenkins REST API, `GET /job/{job_name}/{build_number}/api/json`

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "job_name": {
      "type": "string",
      "enum": ["deploy/checkout-api", "deploy/payment-svc", "test/smoke-staging", "test/smoke-prod", "maintenance/db-backup"]
    },
    "build_number": {
      "type": ["integer", "string"],
      "description": "Build number, or 'lastBuild' for the most recent.",
      "default": "lastBuild"
    }
  },
  "required": ["job_name"],
  "additionalProperties": false
}
```

**Output:**
```json
{
  "job_name": "test/smoke-staging",
  "build_number": 142,
  "status": "SUCCESS",
  "duration_ms": 87432,
  "started_at": "2026-07-24T10:10:00Z",
  "finished_at": "2026-07-24T10:11:27Z",
  "triggered_by": "sre-agent-staging-01",
  "url": "https://jenkins.internal/job/test/smoke-staging/142/"
}
```

**Policy Constraints:** Read-only; no approval required.

---

## Observability Tools

### Tool: `read_prometheus_metrics`

**Purpose:** Execute a PromQL query against the internal Prometheus instance and return the current metric values. Used for checking error rates, latency, pod resource usage, etc.

**Executor:** Prometheus HTTP API, `GET /api/v1/query` (instant) or `/api/v1/query_range` (range)
**Credentials:** Internal VPC endpoint; no auth required (access controlled at network level).

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "description": "PromQL expression (e.g. 'rate(http_requests_total{service=\"checkout-api\"}[5m])').",
      "maxLength": 1000
    },
    "time": {
      "type": "string",
      "description": "Evaluation timestamp in ISO 8601 or Unix seconds. Defaults to now."
    },
    "start": {
      "type": "string",
      "description": "Range start (use with end and step for range queries)."
    },
    "end": {
      "type": "string",
      "description": "Range end."
    },
    "step": {
      "type": "string",
      "description": "Query resolution (e.g. '1m', '5m')."
    }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

**Output:**
```json
{
  "query": "rate(http_requests_total{service=\"checkout-api\"}[5m])",
  "result_type": "vector",
  "result": [
    {
      "metric": { "service": "checkout-api", "method": "POST", "status": "200" },
      "value": [1753349160, "42.3"]
    }
  ]
}
```

**Policy Constraints:**
- PromQL queries are validated with a regex blocklist to reject queries that could return excessive data (e.g., unbounded range queries with very short steps).
- Maximum range duration: 24 hours.
- Minimum step for range queries: 1 minute.

---

## Incident / Ticketing Tools

### Tool: `open_ticket`

**Purpose:** Create a new incident or ticket in the ticketing system (PagerDuty incident or Jira issue). Used when the agent detects a problem that requires human tracking.

**Executor:** PagerDuty Events API v2 or Jira REST API.
**Credentials:** Per-system API key from Secrets Manager.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "system": {
      "type": "string",
      "enum": ["pagerduty", "jira"],
      "description": "Target ticketing system."
    },
    "title": {
      "type": "string",
      "description": "Short summary of the issue.",
      "minLength": 5,
      "maxLength": 255
    },
    "description": {
      "type": "string",
      "description": "Detailed description of the issue, findings, and context.",
      "maxLength": 10000
    },
    "severity": {
      "type": "string",
      "enum": ["critical", "high", "medium", "low"],
      "description": "Issue severity."
    },
    "service": {
      "type": "string",
      "description": "Affected service name (used to route PagerDuty alerts).",
      "enum": ["checkout-api", "payment-svc", "auth-service", "notifications", "infra"]
    },
    "labels": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Optional labels or Jira components.",
      "maxItems": 10
    }
  },
  "required": ["system", "title", "description", "severity", "service"],
  "additionalProperties": false
}
```

**Output:**
```json
{
  "system": "pagerduty",
  "ticket_id": "Q3WE8FXPZ2R",
  "url": "https://example.pagerduty.com/incidents/Q3WE8FXPZ2R",
  "status": "triggered",
  "created_at": "2026-07-24T10:25:00Z"
}
```

**Policy Constraints:**
- `severity: critical` with `system: pagerduty` requires approval to prevent false alarms paging on-call engineers unnecessarily.
- All other combinations: no approval required.

---

### Tool: `read_ticket`

**Purpose:** Retrieve the current state of an existing ticket, including its status, description, and recent activity.

**Executor:** PagerDuty REST API or Jira REST API.

**Input Schema:**
```json
{
  "type": "object",
  "properties": {
    "system": {
      "type": "string",
      "enum": ["pagerduty", "jira"]
    },
    "ticket_id": {
      "type": "string",
      "description": "Ticket/incident ID (e.g. 'Q3WE8FXPZ2R' for PD, 'INFRA-1234' for Jira)."
    }
  },
  "required": ["system", "ticket_id"],
  "additionalProperties": false
}
```

**Output:**
```json
{
  "system": "pagerduty",
  "ticket_id": "Q3WE8FXPZ2R",
  "status": "acknowledged",
  "title": "checkout-api error rate spike",
  "severity": "high",
  "created_at": "2026-07-24T10:25:00Z",
  "assigned_to": "alice@example.com",
  "last_activity": "2026-07-24T10:28:00Z",
  "notes_count": 2
}
```

**Policy Constraints:** Read-only; no approval required.

---

## Common Behaviors Across All Tools

### Timeout Handling

All executor calls have a configurable timeout (default: 10 seconds, max: 30 seconds). On timeout:
- The executor returns a `504` error to the gateway.
- The audit log entry is written with `result_status: executor_timeout`.
- The OTel span is closed with error status.
- No retry is attempted automatically (retries are left to the agent to decide, to avoid duplicate side effects).

### Argument Sanitization

Before passing arguments to any downstream API:
- String values are stripped of leading/trailing whitespace.
- Values are validated against the JSON Schema (type, enum, min/max).
- No shell interpolation or template expansion is performed on arguments.

### Error Response Format

All tool errors are returned as MCP error responses:
```json
{
  "jsonrpc": "2.0",
  "id": "42",
  "error": {
    "code": -32603,
    "message": "Executor error: pod 'xyz' not found in namespace 'payments'",
    "data": {
      "tool": "get_pod_logs",
      "error_type": "not_found",
      "audit_id": "a3f12c9d-...",
      "duration_ms": 45
    }
  }
}
```

The `error_type` field is one of: `not_found`, `permission_denied`, `executor_timeout`, `upstream_error`, `validation_error`, `policy_denied`, `approval_pending`, `approval_denied`.
