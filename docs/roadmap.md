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
- [x] K8s executor: `get_pod_logs` — kubernetes Python client, namespace-scoped
- [x] K8s executor: `list_pods` — with label/field selector support
- [x] K8s executor: `get_deployment_status` — parse deployment conditions into structured output
- [x] Terraform executor: `query_terraform_plan` — call Terraform Cloud API, parse plan summary
- [x] Implement executor timeout handling (10s default, 30s max)
- [x] Implement executor error mapping to MCP error types
- [ ] Local dev: configure `kubectl` context pointing to a local Kind/Minikube cluster — see
      README "Local Kubernetes tools" (manual local setup step, not something the codebase does)

**Week 6: Jenkins + Prometheus + Ticketing read tools**
- [x] Jenkins executor: `get_jenkins_job_status` — call Jenkins REST API
- [x] Prometheus executor: `read_prometheus_metrics` — call Prometheus HTTP API, validate PromQL
- [x] Ticketing executor: `read_ticket` — PagerDuty REST API or Jira REST API
- [x] Write integration tests for each executor against real (or locally mocked) downstream APIs
- [ ] Add per-executor timeout metrics to the OTel span — deferred to Phase 6 (Observability),
      no OTel SDK wired up yet

**Exit Criteria:**
- All 7 read tools return real data from real (or local) downstream APIs.
- A timeout from any executor returns a structured error; the gateway does not hang.
- No secret is logged or returned in tool output.

---

## Phase 4: Destructive Tools + Approval Gate (Week 7)

### Goals
Implement the 3 write tools and the human-in-the-loop approval gate.

### Deliverables

- [x] K8s executor: `restart_deployment` — patch `restartedAt` annotation
- [x] K8s executor: `scale_deployment` — patch deployment scale subresource
- [x] Jenkins executor: `trigger_jenkins_job` — `buildWithParameters` with parameter allowlist
      (allowlist enforcement itself is OPA's job per docs/tool-spec.md, not the executor's)
- [x] Implement approval gate — a prod call that OPA flags `require_approval` is now persisted,
      notified, and resumed end-to-end instead of dead-ending at a 403 (app/approvals.py,
      app/slack.py, app/sse_hub.py, app/main.py):
  - [x] Persist pending approval to Redis with TTL (15 min) — `app/approvals.py::create_pending_approval`
  - [x] Send Slack message via Incoming Webhook (action details + approve/deny buttons) — `app/slack.py::send_approval_request`
  - [x] `POST /admin/approvals/{id}/decide` endpoint (Slack interactive callback target) — `app/main.py::decide_approval`
  - [x] Verify `X-Slack-Signature` HMAC on callback — `app/slack.py::verify_signature`
  - [x] On approval: resume tool call, call executor, write result to audit log
  - [x] On denial: write `approval_denied` audit entry; return error to agent (also rejects a
        replayed callback: `verify_signature` checks the timestamp, not just the HMAC)
  - [x] SSE: push approval result to the agent's open SSE connection — `app/sse_hub.py`, wired into `/mcp/sse`
- [x] Integration test: `restart_deployment` in prod → approval pending → approve → executor called → audit logged
      — `tests/test_approvals.py::test_full_approval_flow_restart_deployment_in_prod`

**Exit Criteria:**
- [x] A prod restart call goes through the full approval flow end-to-end.
- [x] A forged approval callback (bad HMAC) is rejected with 401.
- [x] Approval expiry (TTL elapsed) correctly returns an error.

  Not built (out of scope for this pass, called out explicitly rather than half-built):
  admin JWT auth in front of `/admin/approvals/*` (docs/api-design.md S2.2 describes it, no JWT
  layer exists anywhere in this codebase yet — the decide endpoint's HMAC check is its only auth);
  cross-pod SSE fan-out (`app/sse_hub.py` is in-process only, fine for the current single-process
  gateway, would need Redis pub/sub for multiple replicas); the PostgreSQL audit log itself (still
  the Phase 5 stdout stub in `app/audit.py` — approval decisions log through the same stub as
  every other tool call).

---

## Phase 5: Audit Log (Week 8)

### Goals
Implement the PostgreSQL-backed append-only audit log with SHA-256 hash chaining.

### Deliverables

- [x] Database schema: `audit_log` table, `approvals` table (with migrations via Alembic)
      — `migrations/versions/0001_create_audit_tables.py`, applied with `uv run alembic upgrade head`.
      `approvals` is schema-only in this pass (see the note in that migration file) - the app still
      writes approval state to Redis only (app/approvals.py, Phase 4); nothing populates the
      Postgres `approvals` table yet, and `audit_log.approval_id` has no FK to it (deliberate -
      see `app/audit.py`'s module docstring).
- [x] Async audit log writer: write entry after every tool call (success, deny, error)
      — `app/audit.py::record_tool_call`, called (awaited) from every `record_tool_call(...)` site
      in `app/main.py` (both `/mcp` and the approval-decide endpoint).
- [x] Implement SHA-256 `row_hash` chain: each row hashes `prev_hash + id + agent_id + tool + args + result + timestamp`
      — `app/audit.py::_row_hash`. Writes are serialized with a Postgres advisory lock
      (`CHAIN_LOCK_KEY`) so two concurrent writers can never chain off the same `prev_hash`.
- [x] Implement `GET /admin/audit` query endpoint with filters and pagination — `app/main.py::get_audit_log`
- [x] Implement `integrity_check` computation on query response — `app/audit.py::verify_rows`
      (recomputes each returned row's hash against its true predecessor by seq, not just the
      previous row in a filtered page - see that function's docstring for why)
- [x] Implement `GET /admin/audit/export` streaming NDJSON endpoint — `app/main.py::export_audit_log_endpoint`,
      backed by `app/audit.py::export_audit_log` (asyncpg server-side cursor, not loaded into memory at once)
- [x] Write hash chain verification test: insert rows, modify one manually, verify `integrity_check` returns `fail`
      — `tests/test_audit_docker_integration.py::test_hash_chain_tamper_is_detected` (real Postgres, Docker-gated)
- [x] S3 export: schedule daily export of prior day's rows to S3 (append-only bucket)
      — `scripts/export_audit_to_s3.py` + `.github/workflows/audit-export.yml` (daily cron). No-ops
      until `AUDIT_LOG_S3_BUCKET` etc. are configured as repo secrets - there's no deployed
      Postgres/S3 for this project yet (Phase 7, Infrastructure as Code, hasn't started), so this
      can't run for real until that exists; the workflow is honest about that rather than faking success.
- [x] Document retention policy: 90 days in PostgreSQL, 7 years in S3 Glacier — docs/architecture.md S3.5.
      Documented only: the Postgres-side 90-day deletion and the S3 lifecycle-to-Glacier rule are both
      *not automated* anywhere in this repo (would be a `pg_cron`/scheduled `DELETE` and a Terraform S3
      lifecycle rule respectively - Phase 7 again).

**Exit Criteria:**
- [x] Every test tool call in the integration suite has a corresponding audit row.
      (`test_every_tool_call_gets_an_audit_row`, Docker-gated - the rest of the suite stubs
      `record_tool_call` to a no-op by default, same as it already stubbed `evaluate_policy`,
      since audit persistence needs a real database to mean anything.)
- [x] Modifying any audit row causes the integrity check to fail.
- [x] Export endpoint returns well-formed NDJSON with all required fields.

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
| ~~Additional tools: `exec_into_pod`, `apply_k8s_manifest`~~ | **Done.** See below. | Medium |
| `tools/list` per-agent cache (Redis) | Reduces OPA load for agents that call `tools/list` frequently | Low |
| Grafana SLO dashboards | Error budget tracking for the control plane itself | Medium |
| OPA policy dry-run endpoint | Allows admins to test a proposed policy change against historical tool calls before deploying | High |
| Agent activity anomaly detection | Flag agents whose call patterns deviate significantly from their baseline | High |
| Short-lived API keys (1–7 day expiry) | Reduces blast radius of key compromise; requires auto-rotation integration | Medium |
| WebSocket transport for MCP | Lower latency for high-frequency tool-calling agents | Medium |

### `exec_into_pod` / `apply_k8s_manifest`

Implemented via the Python `kubernetes` client, same as every other K8s tool (`app/tools/tools_spec.py`,
`app/tools/k8s_client.py`):

- [x] `exec_into_pod` — `kubernetes.stream.stream()` against `CoreV1Api.connect_get_namespaced_pod_exec`.
      `command` must be a list of strings (no shell wrapping - the K8s exec API runs it directly, so
      there's no shell-metacharacter injection surface to guard against). Bounded by the same
      `get_executor_timeout()` (10s default / 30s max) every other K8s tool uses, threaded through to
      the exec websocket's own read loop - a stuck command gets its connection cut at the timeout
      instead of hanging the request indefinitely. Output capped at 4,000 characters
      (`EXEC_OUTPUT_MAX_CHARS`), same "don't become a bulk exfiltration channel" concern
      docs/threat-model.md T-08 already raises for `get_pod_logs`.
- [x] `apply_k8s_manifest` — server-side apply (`kubectl apply --server-side`'s underlying mechanism)
      via `kubernetes.dynamic.DynamicClient`, since arbitrary-kind apply needs generic resource
      discovery that the hardcoded `CoreV1Api`/`AppsV1Api` clients don't provide. Rejects cluster-scoped
      resources outright (namespaced only) and rejects a `manifest.metadata.namespace` that disagrees
      with the top-level `namespace` argument - both close a real gap where the namespace OPA actually
      checked and the namespace ultimately mutated could otherwise differ.
- [x] **Mandatory login for every action** — both tools go through the exact same
      authenticate → rate_limit → interceptor → OPA → executor → audit pipeline as every other tool;
      nothing tool-specific was needed since `x-api-key` auth is already unconditional for `/mcp`.
- [x] **Namespace restriction** — both tools take `namespace` as a required top-level argument, so the
      existing per-role `allowed_namespaces` allowlist (`policies/authz.rego`) applies automatically;
      no new namespace-checking code needed. `kube_system_blocked_tools` (`policies/data.json`) hard-blocks
      both against `kube-system` regardless of role, extending the same override `restart_deployment`
      already had.
- [x] **Stricter approval policy** — a new `always_require_approval_tools` list
      (`policies/data.json`/`policies/authz.rego`) gates both tools behind human approval in *every*
      environment, not just prod the way `restart_deployment`/`scale_deployment` are - their blast
      radius (arbitrary code execution; arbitrary resource creation/mutation) isn't bounded by
      environment the way a known deployment's restart is.
- [x] **Mandatory audit logging** — no new code needed here either: every `record_tool_call(...)` call
      site in `app/main.py` already covers every tool by name, unconditionally.
- [x] Scoped to the `sre1` role only (not `deploy-bot`/`readonly`) in both `policies/data.json` and
      `API_KEYS` (`app/middleware/auth.py`) - these are the two highest-risk tools in the tool set and
      CI/CD automation has no business running an interactive-shaped exec or an arbitrary manifest apply.

Tests: `tests/test_tools_executors.py` (executor logic, K8s client faked at the boundary),
`policies/authz_test.rego` (the stricter-than-`destructive_tools` policy, the `kube-system` block,
role scoping), `tests/test_opa_docker_integration.py` (real OPA end-to-end: approval required outside
prod, blocked in `kube-system`).

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
