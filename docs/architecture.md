# MCP Control Plane — System Architecture

## 1. Overview

The MCP Control Plane is a hardened gateway between AI agents (LLM clients) and real infrastructure APIs. It enforces policy-based access control, rate limiting, human-in-the-loop approval for destructive actions, and an immutable audit trail — treating the LLM as an untrusted caller rather than a trusted principal.

The system is designed around one key assumption: **the model will occasionally try to do something wrong, either by mistake or because it has been prompt-injected.** The policy engine — not the prompt — is the actual security boundary.

---

## 2. System Diagram

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                              LLM Client / Agent                                │
│              (Claude, GPT-4, or any MCP-compatible client)                     │
└────────────────────────────────┬───────────────────────────────────────────────┘
                                 │  MCP Protocol (JSON-RPC over HTTP/SSE)
                                 │  API Key in Authorization header
                                 ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│                          ALB (AWS Application Load Balancer)                   │
│                         TLS termination, health checks                         │
└────────────────────────────────┬───────────────────────────────────────────────┘
                                 │
                                 ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│                           MCP Gateway (FastAPI)                                │
│                                                                                │
│   ┌───────────────┐   ┌───────────────┐   ┌──────────────────────────────┐   │
│   │  Authn Layer  │   │  Rate Limiter  │   │    OTel Trace Middleware      │   │
│   │  (API key →   │──▶│  (Redis,       │──▶│  (span per tool call)        │   │
│   │   agent ID)   │   │   per-agent)   │   │                              │   │
│   └───────────────┘   └───────────────┘   └──────────────────────────────┘   │
│                                 │                                              │
│                                 ▼                                              │
│   ┌─────────────────────────────────────────────────────────────────────────┐ │
│   │                      Tool Call Interceptor                              │ │
│   │   Deserializes MCP tool-call, extracts: tool_name, args, agent_id,     │ │
│   │   context metadata (env, resource target, caller IP)                   │ │
│   └─────────────────────────────────┬───────────────────────────────────── ┘ │
└─────────────────────────────────────┼──────────────────────────────────────── ┘
                                      │
                                      ▼
┌────────────────────────────────────────────────────────────────────────────────┐
│                         Policy Engine (OPA / Rego)                             │
│                                                                                │
│   Input:  { agent_id, role, tool_name, args, target_resource, environment }   │
│   Output: { allow: bool, require_approval: bool, reason: string }              │
│                                                                                │
│   Policy bundles loaded from S3 / OCI registry                                │
│   Evaluated in-process via OPA Go library / HTTP sidecar                      │
└───────────────────────┬────────────────────────┬───────────────────────────── ┘
                        │ allow=true             │ allow=false
                        │ require_approval=false │
                        ▼                        ▼
         ┌──────────────────────┐    ┌──────────────────────────────────┐
         │  Tool Executor Layer │    │  Deny response + audit log entry  │
         │                      │    └──────────────────────────────────┘
         │ ┌──────────────────┐ │
         │ │  K8s Executor    │ │ ← IRSA role: eks-tools-ro / eks-tools-rw
         │ │  (pod logs,      │ │
         │ │   restarts,      │ │
         │ │   scale, status) │ │
         │ └──────────────────┘ │
         │ ┌──────────────────┐ │
         │ │  Terraform       │ │ ← IRSA role: tfc-reader
         │ │  Executor        │ │
         │ │  (plan query)    │ │
         │ └──────────────────┘ │
         │ ┌──────────────────┐ │
         │ │  Jenkins         │ │ ← Jenkins API token (Secrets Manager)
         │ │  Executor        │ │
         │ │  (trigger job,   │ │
         │ │   read status)   │ │
         │ └──────────────────┘ │
         │ ┌──────────────────┐ │
         │ │  Prometheus      │ │ ← Internal VPC endpoint
         │ │  Executor        │ │
         │ │  (metrics query) │ │
         │ └──────────────────┘ │
         │ ┌──────────────────┐ │
         │ │  Ticketing       │ │ ← PagerDuty / Jira API key
         │ │  Executor        │ │
         │ │  (open, read)    │ │
         │ └──────────────────┘ │
         └──────────┬───────────┘
                    │
                    ▼
     ┌──────────────────────────────┐
     │    Approval Gate (async)     │  ← only when require_approval=true
     │    Slack webhook / PD event  │
     │    Human approves/denies     │
     │    Result written to Redis   │
     └──────────────────────────────┘
                    │
                    ▼
     ┌──────────────────────────────┐
     │        Audit Log             │
     │   PostgreSQL (append-only)   │
     │   SHA-256 chained hash       │
     │   Fields: agent_id, tool,    │
     │   args, policy_decision,     │
     │   result, timestamp, hash    │
     └──────────────────────────────┘
```

---

## 3. Component Breakdown

### 3.1 MCP Gateway (FastAPI)

The entry point for all agent requests. Handles:

- **Protocol**: MCP JSON-RPC 2.0 over HTTP (POST) and SSE (Server-Sent Events for streaming).
- **Authentication**: Validates Bearer API keys against a per-agent registry stored in AWS Secrets Manager / environment config. Resolves key → `agent_id` + `role` + `allowed_tools`.
- **Rate Limiting**: Redis-backed sliding window counter keyed by `agent_id`. Configurable per-agent limits (default: 60 tool calls/minute). Returns `429` with `Retry-After` on breach.
- **Tool Call Interception**: Before dispatching any tool, the gateway extracts `tool_name`, `arguments`, and injects context metadata (environment, timestamp, caller IP) before policy evaluation.
- **OTel Middleware**: Every inbound request opens a trace span. Child spans are created for the policy evaluation and executor phases. Traces are exported to an OTel collector sidecar.

### 3.2 Policy Engine (OPA)

Open Policy Agent evaluates every tool call before execution. The gateway calls OPA via its HTTP API (sidecar on `localhost:8181`) or embeds the Rego evaluation in-process.

**Input document:**
```json
{
  "agent": {
    "id": "sre-agent-prod-01",
    "role": "sre",
    "environment": "prod"
  },
  "tool": {
    "name": "restart_deployment",
    "args": {
      "namespace": "payments",
      "deployment": "checkout-api"
    }
  },
  "resource": {
    "namespace": "payments",
    "environment": "prod"
  }
}
```

**Output document:**
```json
{
  "allow": false,
  "require_approval": true,
  "reason": "restart_deployment in prod requires human approval"
}
```

Policy bundles are versioned and stored in S3. OPA polls for updates every 60 seconds (or receives a webhook push on bundle update). Policies are tested via `opa test` in CI before deployment.

### 3.3 Tool Executor Layer

Each tool runs in an isolated executor class with its own credentials and least-privilege IAM role (IRSA on EKS). Executors are responsible for:

- Calling the downstream API (K8s, Terraform Cloud, Jenkins, Prometheus, ticketing system).
- Translating structured MCP tool arguments to the target API's call format.
- Returning a structured result (or structured error) back to the gateway.
- Never storing secrets in memory beyond the duration of the call.

### 3.4 Approval Gate

For destructive actions (e.g., `restart_deployment` in prod, `scale_deployment` to 0), the policy engine returns `require_approval: true`. The gateway:

1. Persists the pending tool call to Redis with a TTL (default: 15 minutes).
2. Sends a Slack message (or webhook to PagerDuty) with the action details and an approve/deny link.
3. Holds the agent's SSE connection open (or returns a `202 Pending` with a polling endpoint).
4. Executes or rejects the tool call once a human responds, then logs the approval event.

### 3.5 Audit Log (PostgreSQL)

An append-only table with row-level SHA-256 chaining to make tampering evident. Each row contains:

| Column | Type | Description |
|--------|------|-------------|
| `id` | `uuid` | Primary key |
| `seq` | `bigint` | Monotonic sequence number |
| `agent_id` | `text` | Resolved from API key |
| `tool_name` | `text` | MCP tool name |
| `args` | `jsonb` | Sanitized arguments |
| `policy_decision` | `jsonb` | OPA output |
| `approval_id` | `uuid` | FK to approvals table (nullable) |
| `result_status` | `text` | `success` / `denied` / `error` |
| `result_summary` | `jsonb` | Truncated result or error |
| `duration_ms` | `int` | End-to-end latency |
| `otel_trace_id` | `text` | Correlation to OTel trace |
| `row_hash` | `text` | SHA-256(`prev_hash` + row data) |
| `created_at` | `timestamptz` | Immutable insert time |

The `row_hash` chain allows offline verification that no rows have been modified or deleted since initial write.

---

## 4. Deployment Architecture (AWS EKS)

```
AWS Account
│
├── VPC
│   ├── Public Subnets  → ALB
│   └── Private Subnets → EKS node groups, RDS, Redis, OTel collector
│
├── EKS Cluster
│   └── Namespace: mcp-control-plane
│       ├── Deployment: mcp-gateway (2–3 replicas)
│       │   └── IRSA: mcp-gateway-role (Secrets Manager read, S3 read)
│       ├── Deployment: opa-sidecar (1 per gateway pod)
│       └── Deployment: otel-collector (1 replica)
│
├── RDS PostgreSQL  → audit log
├── ElastiCache Redis → rate limiting + approval state
├── S3 Bucket → OPA policy bundles
├── Secrets Manager → per-tool API keys, DB credentials
└── IAM IRSA Roles (per tool executor):
    ├── k8s-reader-role  (get pods, logs)
    ├── k8s-writer-role  (restart, scale) — only bound in staging by default
    ├── tfc-reader-role  (Terraform Cloud read token)
    └── mcp-gateway-role (Secrets Manager, S3)
```

---

## 5. Data Flow: Single Tool Call (Happy Path)

```
1. Agent sends:  POST /mcp  {tool: "get_pod_logs", args: {namespace: "payments", pod: "checkout-api-xyz"}}
2. ALB routes to gateway pod
3. Gateway validates API key → resolves agent_id="sre-agent-01", role="sre"
4. Rate limiter checks Redis counter → within limit, increments
5. OTel span opened: trace_id generated
6. Tool call interceptor builds OPA input document
7. Gateway POSTs to OPA sidecar → policy returns {allow: true, require_approval: false}
8. Gateway invokes K8s Executor → calls K8s API, returns log lines
9. Audit log row written (async, non-blocking)
10. OTel span closed with success status
11. Gateway returns MCP tool result to agent
```

---

## 6. Observability Stack

| Signal | Tool | What it captures |
|--------|------|-----------------|
| Traces | OTel + Tempo | Per-tool-call spans: authn, policy eval, executor, audit write |
| Metrics | Prometheus + Grafana | Tool-call volume, denial rate, latency p50/p95/p99 by tool, rate-limit hits |
| Logs | Structured JSON → CloudWatch | Gateway access log, OPA decision log, executor errors |
| Alerts | Grafana alerts | Denial rate spike, latency > 5s, error rate > 5%, approval timeout |

---

## 7. Technology Stack Summary

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| API Framework | FastAPI |
| MCP Protocol | Official MCP Python SDK |
| Policy Engine | OPA (Open Policy Agent) + Rego |
| K8s Client | `kubernetes` Python client |
| Rate Limiting | Redis (via `redis-py`) |
| Audit Store | PostgreSQL 15 (RDS) via `asyncpg` |
| Observability | OpenTelemetry SDK, Prometheus, Grafana, Tempo |
| Infrastructure | Terraform (EKS, RDS, Redis, IAM, ALB) |
| CI/CD | GitHub Actions |
| Secrets | AWS Secrets Manager |
| Container Registry | Amazon ECR |
