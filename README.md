# MCP Control Plane

A hardened gateway that sits between AI agents (LLM clients) and real infrastructure
APIs (Kubernetes, Terraform, Jenkins, Prometheus, ticketing). Every tool call is
authenticated, rate-limited, policy-checked via [OPA](https://www.openpolicyagent.org/),
and audited before it reaches an executor. The model is treated as an untrusted
caller — the policy engine, not the prompt, is the actual security boundary.

Full target architecture, threat model, and API design live in [`docs/`](docs/); this
README covers what's implemented today and how to run it.

## Status

Actively under development, tracked in [`docs/roadmap.md`](docs/roadmap.md). Currently
implemented:

- MCP JSON-RPC gateway (`tools/list`, `tools/call`) over HTTP, plus an SSE endpoint that also
  carries approval-decision pushes
- API key authentication, resolving each key to an agent identity (`id`, `role`, `allowed_tools`)
- Redis-backed sliding-window rate limiting, per agent
- Tool call interception, normalization, and an OPA policy check (allow / deny / require_approval)
- 7 read-only tool executors calling real downstream APIs (Kubernetes, Terraform Cloud, Jenkins,
  Prometheus, PagerDuty/Jira), plus the 3 write tools (`restart_deployment`, `scale_deployment`,
  `trigger_jenkins_job`) — `docs/roadmap.md` Phases 3-4.
- 2 higher-risk stretch tools, `exec_into_pod` and `apply_k8s_manifest` (`docs/roadmap.md` Phase 10):
  same Python `kubernetes` client, `sre1`-only, hard-blocked against `kube-system`, and gated behind
  approval in *every* environment (not just prod, unlike the write tools above).
- Human-in-the-loop approval gate for `require_approval` policy decisions: pending approvals are
  persisted to Redis with a 15-minute TTL, a Slack Incoming Webhook notifies with approve/deny
  buttons, and `POST /admin/approvals/{id}/decide` (HMAC-verified, replay-protected) resumes the
  tool call on approval or logs `approval_denied` on denial — see `docs/roadmap.md` Phase 4.
- PostgreSQL-backed, SHA-256 hash-chained audit log: every tool call (allowed, denied, errored,
  pending/decided approval) gets one append-only row via `app/audit.py`, schema-migrated with
  Alembic. `GET /admin/audit` queries it with filters/pagination and an `integrity_check` that
  recomputes the hash chain; `GET /admin/audit/export` streams the same data as NDJSON. A daily
  script/workflow exports the prior day's rows to S3 — see `docs/roadmap.md` Phase 5.
- OpenTelemetry traces + metrics across the whole request path (`authn` → `policy_eval` →
  `executor` → `audit_write` spans, `tool_calls_total`/`policy_denials_total`/
  `approval_requests_total`/`rate_limit_hits_total`/`tool_call_duration_ms` and friends), an OTel
  collector + Tempo + Prometheus + Grafana stack in `docker-compose.yaml`, 6 provisioned Grafana
  dashboards and 4 alert rules under `observability/` — see `docs/roadmap.md` Phase 6.
- Terraform for the full AWS deployment target (VPC, EKS + managed node group, ECR, RDS, ElastiCache
  Redis, ALB/ACM, IRSA IAM roles, Secrets Manager, an Object-Locked S3 bucket for the audit export)
  plus a Helm chart (`helm/mcp-control-plane/`) installed by Terraform itself, under `terraform/` —
  see `docs/roadmap.md` Phase 7, **including its "what's actually been checked" note**: this was
  written with no AWS account available, so it's `fmt`/reference/`init`-clean and the Helm chart
  is `lint`/`template`-verified, but has never been `plan`/`apply`/`destroy`-run against real AWS.

Not yet built: admin JWT auth in front of `/admin/approvals/*` and `/admin/audit*` (both currently
have no auth of their own beyond the approval-decide endpoint's Slack HMAC check), automated
retention enforcement (the 90-day Postgres deletion and S3-to-Glacier lifecycle rule are
documented but not scheduled anywhere), and the EKS DaemonSet variant of the OTel collector (Phase
6 only ships the Docker Compose one; `terraform/helm.tf` runs the collector as a regular in-cluster
Deployment, not a DaemonSet) — see the roadmap for sequencing.

## Architecture

```
Agent → POST /mcp → [log] → [authn] → [rate limit] → interceptor → OPA → executor → audit log
```

- `app/main.py` — FastAPI app, route handlers, middleware wiring
- `app/middleware/auth.py` — API key → agent identity
- `app/middleware/rate_limit.py` — Redis sliding-window limiter
- `app/interceptor.py` — validates/normalizes `tools/call` params into an OPA input document
- `app/authz/opa.py` — calls the OPA sidecar, maps its response to allow/deny/require_approval
- `app/tools/tools_spec.py` — tool executors, 7 read-only + 5 write (all call real downstream APIs)
- `app/tools/k8s_client.py` — cached K8s API clients (CoreV1Api/AppsV1Api + a generic DynamicClient
  for `apply_k8s_manifest`) + ApiException/timeout -> `ExecutorError` mapping
- `app/tools/errors.py`, `app/tools/config.py` — shared `ExecutorError` type and the 10s/30s executor timeout
- `app/approvals.py` — Redis-backed pending-approval store (create / decide, 15-minute TTL)
- `app/slack.py` — Slack Incoming Webhook notification + `X-Slack-Signature` HMAC verification
- `app/sse_hub.py` — in-process pub/sub pushing an approval decision to the agent's open `/mcp/sse` connection
- `app/db.py` — shared asyncpg (Postgres) connection pool
- `app/audit.py` — hash-chained audit log: async writer, filtered query, integrity check, NDJSON export
- `migrations/` — Alembic migrations (raw SQL, no ORM) for `audit_log` and `approvals`
- `scripts/export_audit_to_s3.py` — daily S3 export of the prior day's audit rows
- `policies/` — Rego policy (`authz.rego`) and role/tool data (`data.json`) loaded by OPA

See [`docs/architecture.md`](docs/architecture.md) for the full target-state design
(EKS, Terraform, Postgres audit log, approval gate, observability stack).

## Tech stack

Python 3.13, FastAPI, Redis, OPA/Rego, Docker Compose. See `pyproject.toml` for exact
dependency versions.

## Getting started

### Prerequisites

- Python 3.13+ and [`uv`](https://docs.astral.sh/uv/)
- Docker (for Redis/Postgres/OPA via Compose), or your own local instances

### Install

```bash
uv sync --extra export
```

(`--extra export` pulls in `boto3` — `tests/test_export_script.py` imports `scripts/export_audit_to_s3.py`
unconditionally, so a bare `uv sync` leaves the full `uv run pytest` run unable to even collect.)

### Run

Everything (gateway + Redis + Postgres + OPA + the OTel collector/Tempo/Prometheus/Grafana
observability stack) via Docker Compose:

```bash
docker compose up
```

Compose doesn't run the Alembic migration for you - apply it once, as a one-off container on the
same network (`postgres`'s port isn't published to the host by default, so this is simpler than
reaching it from outside Compose):

```bash
docker compose run --rm gateway alembic upgrade head
```

Grafana is at http://localhost:3000 (anonymous admin access, no login screen - local dev only).
The 6 dashboards and 4 alert rules under `observability/grafana/` are provisioned automatically.
The PagerDuty/Slack contact points (`observability/grafana/provisioning/alerting/contact-points.yaml`)
ship with placeholder values and will provision cleanly either way - replace them with a real
integration key/webhook URL before an alert needs to actually page anyone.

**Dashboards start empty** - they only show data once something sends real traffic through the
gateway *container* (the one `docker compose` starts; only it has `OTEL_EXPORTER_OTLP_ENDPOINT`
set - `uv run pytest` and a locally-run `uvicorn` deliberately don't export telemetry, see
`app/otel.py`). With the stack up and migrated, fire a few requests at it (each maps to a
different dashboard/panel) and wait ~60-90s for the metrics export + Prometheus scrape interval:

```bash
# Tool Call Volume + Latency - any tool call, success or failure, records these
curl -X POST http://127.0.0.1:8000/mcp -H "Content-Type: application/json" -H "x-api-key: test_key" \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"get_pod_logs","arguments":{"namespace":"payments","pod_name":"checkout-api-xyz"}}}'

# Denial Rate - has to actually reach OPA and be denied there; a tool the agent isn't
# scoped to at all (app/middleware/auth.py's allowed_tools) gets rejected before OPA/audit
# ever see it and won't show up here - this hits OPA's kube-system hard-block instead
curl -X POST http://127.0.0.1:8000/mcp -H "Content-Type: application/json" -H "x-api-key: test_key" \
  -d '{"jsonrpc":"2.0","id":"2","method":"tools/call","params":{"name":"restart_deployment","arguments":{"namespace":"kube-system","deployment_name":"coredns","reason":"testing denial rate"}}}'

# Approval Queue - require_approval (any write tool in a prod-* namespace)
curl -X POST http://127.0.0.1:8000/mcp -H "Content-Type: application/json" -H "x-api-key: test_key" \
  -d '{"jsonrpc":"2.0","id":"3","method":"tools/call","params":{"name":"restart_deployment","arguments":{"namespace":"prod-payments","deployment_name":"checkout-api","reason":"testing approval queue"}}}'

# Rate Limit - test_key's default limit is 60/min (DEFAULT_RATE_LIMIT_RPM), so fire more than that quickly
for i in $(seq 1 70); do
  curl -s -o /dev/null -X POST http://127.0.0.1:8000/mcp -H "Content-Type: application/json" -H "x-api-key: test_key" \
    -d '{"jsonrpc":"2.0","id":"'$i'","method":"tools/list","params":{}}'
done
```

Audit Chain Integrity is the one dashboard these won't touch - it only reports data when
`GET /admin/audit` actually finds a broken hash chain, so staying at zero is the correct/healthy
state, not something to artificially populate. On Windows, run the block above from PowerShell or
Git Bash, not `cmd.exe` - `cmd.exe` doesn't treat `'...'` as a string delimiter the way these
commands assume, so the JSON body arrives with literal quote characters in it and the gateway
correctly rejects it with `-32700 Parse error` (same failure mode `testing/testing.text` 3.8
tests on purpose, just not on purpose here).

Or the gateway alone against local dependencies (Redis, Postgres, OPA each running separately).
Apply the audit-log schema once before the first run:

```bash
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

The app reads `REDIS_URL` (default `redis://localhost:6379`), `DATABASE_URL` (default
`postgresql://postgres:postgres@localhost:5432/mcp_control_plane`), and `OPA_URL` (default
`http://localhost:8181/v1/data/authz`) from the environment; Compose sets all three to point
at the sibling containers. `alembic upgrade head` reads the same `DATABASE_URL`.

### Tool executor configuration

Each read-only executor talks to a real downstream API and reads its own credentials/URL
from the environment (all optional locally — a tool without its config set returns an
`upstream_error` rather than crashing):

| Tool(s) | Env vars |
|---------|----------|
| `get_pod_logs`, `list_pods`, `get_deployment_status` | none — uses in-cluster config, or `KUBECONFIG` / `~/.kube/config` locally |
| `query_terraform_plan` | `TFC_TOKEN`, `TFC_ORG`, `TFC_URL` (default `https://app.terraform.io/api/v2`) |
| `get_jenkins_job_status` | `JENKINS_URL`, `JENKINS_USER`, `JENKINS_API_TOKEN` |
| `read_prometheus_metrics` | `PROMETHEUS_URL` (default `http://localhost:9090`) |
| `read_ticket` (pagerduty) | `PAGERDUTY_API_TOKEN`, `PAGERDUTY_URL` (default `https://api.pagerduty.com`) |
| `read_ticket` (jira) | `JIRA_URL`, `JIRA_USER`, `JIRA_API_TOKEN` |
| all of the above | `EXECUTOR_TIMEOUT_SECONDS` (default `10`, clamped to a `30` max) |

**Local Kubernetes tools:** point `KUBECONFIG` at a local cluster (e.g. `kind create cluster`
or `minikube start`, then `kubectl config use-context kind-kind` / `minikube`) before calling
`get_pod_logs`, `list_pods`, or `get_deployment_status` — the gateway uses whatever context is
currently active, same as `kubectl`.

### Approval gate configuration

| Env var | Purpose |
|---------|---------|
| `SLACK_WEBHOOK_URL` | Incoming Webhook URL the approval notification is POSTed to. Unset locally: the notification is skipped (logged, not sent) rather than failing the call. |
| `SLACK_SIGNING_SECRET` | Shared secret verified against `X-Slack-Signature` on `POST /admin/approvals/{id}/decide`. Unset: every callback is rejected with 401 (fails closed, same as OPA being unreachable). |

The pending-approval record itself lives in the same Redis as the rate limiter (`REDIS_URL`), with
a 15-minute TTL — no separate store to configure.

### Audit log S3 export configuration

`scripts/export_audit_to_s3.py` (run daily by `.github/workflows/audit-export.yml`) needs:

| Env var | Purpose |
|---------|---------|
| `S3_BUCKET` | Target bucket. No default — the script refuses to run without one rather than silently exporting nowhere. |
| `DATABASE_URL` | Same variable `app/db.py` reads. |
| AWS credentials | Via boto3's standard chain (env vars, `~/.aws/credentials`, or an IAM role in CI). |

The GitHub Actions workflow reads these from `AUDIT_LOG_S3_BUCKET`/`AUDIT_LOG_DATABASE_URL`/etc.
repo secrets and no-ops if they're not configured — there's no deployed Postgres/S3 for this
project yet (Phase 7, Infrastructure as Code, hasn't started).

### Try it

All `/mcp` routes require an `x-api-key` header. The seeded dev key `test_key` resolves
to `agent01` (role `sre1`) with a handful of allowed tools — see `API_KEYS` in
`app/middleware/auth.py`.

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" -H "x-api-key: test_key" \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}'

curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" -H "x-api-key: test_key" \
  -d '{"jsonrpc":"2.0","id":"2","method":"tools/call","params":{"name":"get_pod_logs","arguments":{"namespace":"payments","pod_name":"checkout-api-xyz"}}}'

curl -s http://127.0.0.1:8000/health/live
```

More request/response examples (error cases, SSE) are in [`testing/testing.text`](testing/testing.text).

## Testing

```bash
uv run pytest
```

Covers the auth middleware, rate limiter, OPA integration, and end-to-end `/mcp`
request handling (see `tests/`). By default `record_tool_call`/`create_db_pool` are stubbed
(no real Postgres needed), the same way `evaluate_policy` is stubbed to allow everything
(no real OPA needed) — see `tests/conftest.py`. Two subsets need Docker and are skipped
automatically if it isn't available:

- `tests/test_opa_docker_integration.py` — real `openpolicyagent/opa` image against `policies/`
- `tests/test_audit_docker_integration.py` — real `postgres:16-alpine`, with the actual
  `alembic upgrade head` migration applied, exercising the hash chain (including a tamper/detect
  test) and the query/export endpoints for real

Rego policy tests run separately, via `opa test`:

```bash
docker run --rm -v "$(pwd)/policies:/policies" openpolicyagent/opa:latest test /policies -v
```

On Windows, run that from PowerShell, not Git Bash — Git Bash's MSYS layer auto-translates the
leading `/policies` in `-v "$(pwd)/policies:/policies"` into a bogus Windows path (`stat
/Program Files/Git/policies: no such file or directory`), which has nothing to do with the
policies themselves:

```powershell
docker run --rm -v "${PWD}\policies:/policies" openpolicyagent/opa:latest test /policies -v
```

`opa test` runs in CI on every PR (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml));
the Python test suite above is not currently wired into CI.

## Documentation

| Doc | Contents |
|-----|----------|
| [`docs/roadmap.md`](docs/roadmap.md) | Phased build plan and current progress |
| [`docs/architecture.md`](docs/architecture.md) | Full target-state system design |
| [`docs/api-design.md`](docs/api-design.md) | MCP + admin API surface |
| [`docs/tool-spec.md`](docs/tool-spec.md) | Per-tool input/output schemas and policy constraints |
| [`docs/threat-model.md`](docs/threat-model.md) | STRIDE threat model and mitigations |
| [`docs/rego-policies.md`](docs/rego-policies.md) | Rego policy structure, for policy authors |
