# MCP Control Plane — Engineering Roadmap

## Guiding Principles

- **Build the security skeleton first.** The policy engine and audit log are non-negotiable day-one requirements. Tools added before the control layer is hardened are a liability.
- **Real tools over mock tools.** Every tool should call a real API from the first day it exists. Mocked executors give a false sense of completeness and hide the hard problems (auth, error handling, partial failure).
- **Infrastructure as code from day one.** No manual cloud resources. If it can't be reproduced from a `terraform apply`, it doesn't count.
- **Shippable at the end of each phase.** Each phase produces something that can be demoed and inspected, not just code that compiles.

---

## Phase Overview

| Phase | Focus | Duration | Exit Criteria |
|-------|-------|----------|---------------|
| 1 | Core Gateway + Authn | Week 1–2 | Authenticated tool call reaches OPA and returns a deny |
| 2 | Policy Engine | Week 3–4 | Role-based allow/deny works for all 10 tools; OPA tests pass in CI |
| 3 | Tool Executors (read-only) | Week 5–6 | 7 read tools call real APIs and return real data |
| 4 | Destructive Tools + Approval Gate | Week 7 | `restart_deployment` in prod blocked until Slack approval |
| 5 | Audit Log | Week 8 | Every tool call logged; hash chain verified; export works |
| 6 | Observability | Week 9 | OTel traces in Grafana; denial-rate and latency dashboards live |
| 7 | Infrastructure as Code | Week 10–11 | `terraform apply` from scratch creates a working EKS deployment |
| 8 | CI/CD Pipeline | Week 12 | PRs run lint/test/scan; merge to main deploys to staging; prod requires approval |
| 9 | Hardening & Load Testing | Week 13 | Rate limiter tested; hash chain verified; pen test checklist complete |
| 10 | Stretch / Polish | Week 14+ | Multi-tenant, additional tools, Grafana SLO dashboards |

---

## Phase 1: Core Gateway + Authentication (Week 1–2)

### Goals
Stand up the FastAPI application with the MCP SDK integrated, implement API key authentication, and wire up the stub policy engine so that every tool call is intercepted before execution.

### Deliverables

**Week 1**
- [ ] Initialize Python project with `uv`, FastAPI, MCP SDK
- [ ] Implement `POST /mcp` endpoint that parses MCP JSON-RPC requests
- [ ] Implement `GET /mcp/sse` SSE transport
- [ ] Implement API key middleware: validate key, resolve `agent_id` + `role` + `allowed_tools`
- [ ] Implement tool listing (`tools/list`) filtered by agent's allowed tools
- [ ] Health endpoints: `GET /health/live` and `GET /health/ready`
- [ ] Dockerize: `Dockerfile` + `docker-compose.yml` for local dev (gateway + Redis + Postgres stub)

**Week 2**
- [ ] Implement Redis rate limiter: sliding window per `agent_id`
- [ ] Implement tool call interceptor: extract tool name, args, agent context before dispatch
- [ ] Wire stub OPA call (hardcoded allow/deny based on env variable for now)
- [ ] Return structured MCP error responses for: invalid key, rate limit exceeded, missing params
- [ ] Write unit tests for authn middleware and rate limiter
- [ ] Write integration test: tool call with invalid key → 401; valid key wrong tool → 403

**Exit Criteria:**
- A valid tool call with a valid key reaches the (stub) policy engine.
- A call with an invalid key or rate-limited agent returns the correct MCP error.
- `docker compose up` starts a working local gateway.

---

## Phase 2: Policy Engine (Week 3–4)

### Goals
Replace the stub policy check with a real OPA instance. Write Rego policies for all 10 tools covering role-based access, environment scope, and argument-level constraints.

### Deliverables

**Week 3**
- [ ] Add OPA as a Docker Compose service (sidecar for local dev)
- [ ] Define OPA input/output schema (agent, tool, args, resource, environment)
- [ ] Write Rego base policy: `allow` if tool in role's allowed_tools list
- [ ] Write Rego environment policy: tool+role combinations allowed per environment
- [ ] Write Rego argument policy: namespace allowlist for K8s tools
- [ ] Write Rego approval policy: `require_approval: true` for destructive actions in prod
- [ ] Gateway: replace stub with real OPA HTTP call; handle allow/deny/require_approval

**Week 4**
- [ ] Write OPA unit tests (`opa test`) for all roles × all tools × all environments
- [ ] CI: add `opa test` step to GitHub Actions PR workflow
- [ ] Write integration tests: SRE role allowed `get_pod_logs` in prod; `restart_deployment` in prod returns require_approval
- [ ] Document Rego policy structure in `docs/` for future policy authors
- [ ] Add `policy_decision` to all MCP error responses (transparency to the agent)

**Exit Criteria:**
- `opa test` passes with >90% rule coverage.
- All 10 tools have explicit policy rules covering role, environment, and argument constraints.
- A call that would be policy-denied never reaches the executor.

---

## Phase 3: Tool Executors — Read-Only Tools (Week 5–6)

### Goals
Implement the 7 read-only tool executors, each calling a real downstream API. Focus on error handling, timeout behavior, and structured output.

### Deliverables

**Week 5: K8s + Terraform tools**
- [ ] K8s executor: `get_pod_logs` — kubernetes Python client, namespace-scoped
- [ ] K8s executor: `list_pods` — with label/field selector support
- [ ] K8s executor: `get_deployment_status` — parse deployment conditions into structured output
- [ ] Terraform executor: `query_terraform_plan` — call Terraform Cloud API, parse plan summary
- [ ] Implement executor timeout handling (10s default, 30s max)
- [ ] Implement executor error mapping to MCP error types
- [ ] Local dev: configure `kubectl` context pointing to a local Kind/Minikube cluster

**Week 6: Jenkins + Prometheus + Ticketing read tools**
- [ ] Jenkins executor: `get_jenkins_job_status` — call Jenkins REST API
- [ ] Prometheus executor: `read_prometheus_metrics` — call Prometheus HTTP API, validate PromQL
- [ ] Ticketing executor: `read_ticket` — PagerDuty REST API or Jira REST API
- [ ] Write integration tests for each executor against real (or locally mocked) downstream APIs
- [ ] Add per-executor timeout metrics to the OTel span

**Exit Criteria:**
- All 7 read tools return real data from real (or local) downstream APIs.
- A timeout from any executor returns a structured error; the gateway does not hang.
- No secret is logged or returned in tool output.

---

## Phase 4: Destructive Tools + Approval Gate (Week 7)

### Goals
Implement the 3 write tools and the human-in-the-loop approval gate.

### Deliverables

- [ ] K8s executor: `restart_deployment` — patch `restartedAt` annotation
- [ ] K8s executor: `scale_deployment` — patch deployment scale subresource
- [ ] Jenkins executor: `trigger_jenkins_job` — `buildWithParameters` with parameter allowlist
- [ ] Implement approval gate:
  - [ ] Persist pending approval to Redis with TTL (15 min)
  - [ ] Send Slack message via Incoming Webhook (action details + approve/deny buttons)
  - [ ] `POST /admin/approvals/{id}/decide` endpoint (Slack interactive callback target)
  - [ ] Verify `X-Slack-Signature` HMAC on callback
  - [ ] On approval: resume tool call, call executor, write result to audit log
  - [ ] On denial: write `approval_denied` audit entry; return error to agent
  - [ ] SSE: push approval result to the agent's open SSE connection
- [ ] Integration test: `restart_deployment` in prod → approval pending → approve → executor called → audit logged

**Exit Criteria:**
- A prod restart call goes through the full approval flow end-to-end.
- A forged approval callback (bad HMAC) is rejected with 401.
- Approval expiry (TTL elapsed) correctly returns an error.

---

## Phase 5: Audit Log (Week 8)

### Goals
Implement the PostgreSQL-backed append-only audit log with SHA-256 hash chaining.

### Deliverables

- [ ] Database schema: `audit_log` table, `approvals` table (with migrations via Alembic)
- [ ] Async audit log writer: write entry after every tool call (success, deny, error)
- [ ] Implement SHA-256 `row_hash` chain: each row hashes `prev_hash + id + agent_id + tool + args + result + timestamp`
- [ ] Implement `GET /admin/audit` query endpoint with filters and pagination
- [ ] Implement `integrity_check` computation on query response
- [ ] Implement `GET /admin/audit/export` streaming NDJSON endpoint
- [ ] Write hash chain verification test: insert rows, modify one manually, verify `integrity_check` returns `fail`
- [ ] S3 export: schedule daily export of prior day's rows to S3 (append-only bucket)
- [ ] Document retention policy: 90 days in PostgreSQL, 7 years in S3 Glacier

**Exit Criteria:**
- Every test tool call in the integration suite has a corresponding audit row.
- Modifying any audit row causes the integrity check to fail.
- Export endpoint returns well-formed NDJSON with all required fields.

---

## Phase 6: Observability (Week 9)

### Goals
Add OpenTelemetry instrumentation across the entire request path and build the Grafana dashboards.

### Deliverables

- [ ] Add OTel SDK to gateway: auto-instrument FastAPI (traces + metrics)
- [ ] Add manual spans: `authn`, `policy_eval`, `executor`, `audit_write`
- [ ] Add span attributes: `agent_id`, `tool_name`, `environment`, `policy_decision`, `duration_ms`
- [ ] Add counter metrics: `tool_calls_total`, `policy_denials_total`, `approval_requests_total`, `rate_limit_hits_total`
- [ ] Add histogram metrics: `tool_call_duration_ms` (by tool)
- [ ] Deploy OTel collector via Docker Compose (local) / EKS DaemonSet (prod)
- [ ] Grafana dashboards:
  - [ ] **Tool Call Volume**: requests/min by tool, breakdown by result_status
  - [ ] **Denial Rate**: % denied by tool and role over time
  - [ ] **Latency**: p50/p95/p99 by tool
  - [ ] **Approval Queue**: pending approvals count, average approval time
  - [ ] **Rate Limit**: rate-limit hits by agent
- [ ] Alerts:
  - [ ] Denial rate > 20% sustained 5 minutes → PagerDuty
  - [ ] p95 latency > 5s for any tool → Slack alert
  - [ ] Approval queue > 10 pending → Slack alert
  - [ ] Audit log hash chain failure → PagerDuty (critical)

**Exit Criteria:**
- End-to-end trace visible in Grafana Tempo for a test tool call.
- All 5 dashboards populated with real data from integration tests.
- Alerts configured and test-fired.

---

## Phase 7: Infrastructure as Code (Week 10–11)

### Goals
100% Terraform — EKS cluster, RDS, Redis, IAM roles, ALB, S3, Secrets Manager. Must be reproducible from scratch with a single `terraform apply`.

### Deliverables

**Week 10: Core infra**
- [ ] Terraform module: VPC (public + private subnets, NAT GW, IGW)
- [ ] Terraform module: EKS cluster + managed node group (2–3 nodes, t3.medium)
- [ ] Terraform: ECR repository for gateway image
- [ ] Terraform: RDS PostgreSQL 15 (Multi-AZ in prod, single in staging)
- [ ] Terraform: ElastiCache Redis (single node in staging, cluster in prod)
- [ ] Terraform: ALB + target group + listener (HTTPS, ACM cert)

**Week 11: IAM + app config**
- [ ] Terraform: IRSA roles (mcp-gateway-role, k8s-reader-role, k8s-writer-role, tfc-reader-role)
- [ ] Terraform: Secrets Manager secrets (DB creds, Redis URL, Jenkins token, Slack signing secret)
- [ ] Terraform: S3 bucket (OPA bundles, audit log export) with Object Lock
- [ ] Kubernetes manifests (Helm chart): Deployment, Service, HPA, PodDisruptionBudget, OPA sidecar, OTel collector
- [ ] Terraform: Separate workspace per environment (staging / prod)
- [ ] `terraform destroy` workflow validated: can tear down and rebuild without data loss (audit log S3 is retained)

**Exit Criteria:**
- `terraform apply` from a fresh AWS account creates a working EKS deployment.
- `terraform destroy` removes all ephemeral resources without touching the audit S3 bucket.
- All secrets are in Secrets Manager; no secret appears in any Terraform state file.

---

## Phase 8: CI/CD Pipeline (Week 12)

### Goals
GitHub Actions pipeline: lint, test, security scan on PR; build + push image on merge; Terraform plan on PR / apply on merge to main; manual approval gate for prod.

### Deliverables

- [ ] **PR Checks workflow:**
  - [ ] `ruff` linting + `mypy` type checking
  - [ ] Unit tests (`pytest`)
  - [ ] Integration tests (against Docker Compose stack)
  - [ ] `opa test` for Rego policies
  - [ ] `trivy` image scan (fail on CRITICAL CVEs)
  - [ ] `checkov` scan on Terraform (fail on HIGH severity)
  - [ ] Terraform `plan` (non-destructive, posts plan summary as PR comment)
- [ ] **Merge to main workflow:**
  - [ ] Build + tag Docker image (`git sha` + `latest`)
  - [ ] Push to ECR
  - [ ] Terraform apply to staging (automatic)
  - [ ] Run smoke test against staging
  - [ ] Manual approval gate → Terraform apply to prod
- [ ] GitHub Environments: `staging` (auto-deploy) and `prod` (requires reviewer)
- [ ] Dependabot: weekly dependency updates for Python packages and Docker base image

**Exit Criteria:**
- A failing `opa test` blocks a PR merge.
- A merge to main triggers a staging deploy and smoke test within 10 minutes.
- Prod deploy requires explicit human approval in GitHub.

---

## Phase 9: Hardening & Load Testing (Week 13)

### Goals
Validate all security controls under adversarial and high-load conditions.

### Deliverables

- [ ] **Rate limiter test**: send 200 RPM from a single agent; verify 429 at 61 RPM and auto-suspend at 180 RPM
- [ ] **Prompt injection simulation**: craft tool arguments designed to escape namespace constraints; verify OPA blocks them
- [ ] **HMAC bypass test**: send fake approval callback without valid signature; verify 401
- [ ] **Hash chain integrity test**: manually modify an audit log row; verify `integrity_check` returns `fail`
- [ ] **Load test** (k6): 100 concurrent agents, 50 RPM each; verify gateway stays below 200ms p95, no OOM
- [ ] **HPA test**: drive CPU above 70%; verify new pods come up and traffic is distributed
- [ ] **Chaos test** (optional): kill OPA sidecar; verify gateway returns policy error (not a bypass)
- [ ] **Policy review**: second engineer reviews all Rego files for logic gaps
- [ ] **Dependency audit**: run `pip-audit`; patch any known CVEs

**Exit Criteria:**
- All adversarial tests blocked at the intended layer (policy, HMAC, rate limiter).
- Load test passes with p95 < 200ms at 5000 RPM total.
- No CVEs in `pip-audit` output at HIGH or CRITICAL.

---

## Phase 10: Stretch Goals (Week 14+)

These items are not required for a complete, shippable project but represent meaningful extensions.

| Item | Value | Complexity |
|------|-------|-----------|
| Multi-tenant agent namespacing | Allows multiple teams/orgs to share the gateway with full isolation | High |
| Additional tools: `exec_into_pod`, `apply_k8s_manifest` | Higher-value but higher-risk tools (require stricter policy + approval) | Medium |
| `tools/list` per-agent cache (Redis) | Reduces OPA load for agents that call `tools/list` frequently | Low |
| Grafana SLO dashboards | Error budget tracking for the control plane itself | Medium |
| OPA policy dry-run endpoint | Allows admins to test a proposed policy change against historical tool calls before deploying | High |
| Agent activity anomaly detection | Flag agents whose call patterns deviate significantly from their baseline | High |
| Short-lived API keys (1–7 day expiry) | Reduces blast radius of key compromise; requires auto-rotation integration | Medium |
| WebSocket transport for MCP | Lower latency for high-frequency tool-calling agents | Medium |

---

## Key Milestones

| Date (relative) | Milestone |
|-----------------|-----------|
| +2 weeks | Gateway running locally with authn + rate limiting |
| +4 weeks | OPA policy engine live; all 10 tools have Rego rules with tests passing |
| +6 weeks | All 7 read tools call real APIs |
| +7 weeks | Approval gate working end-to-end with Slack |
| +8 weeks | Audit log with hash chain |
| +9 weeks | Grafana dashboards live |
| +11 weeks | Infrastructure fully Terraform-managed, reproducible from scratch |
| +12 weeks | CI/CD pipeline: PR checks + staging auto-deploy + prod approval gate |
| +13 weeks | Hardened, load-tested, security-reviewed: **project complete** |
