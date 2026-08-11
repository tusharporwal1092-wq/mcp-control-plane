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

- MCP JSON-RPC gateway (`tools/list`, `tools/call`) over HTTP, plus an SSE heartbeat endpoint
- API key authentication, resolving each key to an agent identity (`id`, `role`, `allowed_tools`)
- Redis-backed sliding-window rate limiting, per agent
- Tool call interception, normalization, and an OPA policy check (allow / deny / require_approval)
- 7 read-only tool executors calling real downstream APIs (Kubernetes, Terraform Cloud, Jenkins,
  Prometheus, PagerDuty/Jira) — `docs/roadmap.md` Phase 3. The 3 write tools
  (`restart_deployment`, `scale_deployment`, `trigger_jenkins_job`) are still stubs, pending
  Phase 4's approval gate.
- Structured audit logging to stdout (durable, hash-chained Postgres storage is a later phase)

Not yet built: the 3 write tool executors, the human-in-the-loop approval workflow, persistent
audit storage, and observability (OTel/Grafana) — see the roadmap for sequencing.

## Architecture

```
Agent → POST /mcp → [log] → [authn] → [rate limit] → interceptor → OPA → executor → audit log
```

- `app/main.py` — FastAPI app, route handlers, middleware wiring
- `app/middleware/auth.py` — API key → agent identity
- `app/middleware/rate_limit.py` — Redis sliding-window limiter
- `app/interceptor.py` — validates/normalizes `tools/call` params into an OPA input document
- `app/authz/opa.py` — calls the OPA sidecar, maps its response to allow/deny/require_approval
- `app/tools/tools_spec.py` — tool executors (7 read-only tools call real APIs; 3 write tools are still stubs)
- `app/tools/k8s_client.py` — cached K8s API clients + ApiException/timeout -> `ExecutorError` mapping
- `app/tools/errors.py`, `app/tools/config.py` — shared `ExecutorError` type and the 10s/30s executor timeout
- `app/audit.py` — audit trail of every tool call
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
uv sync
```

### Run

Everything (gateway + Redis + Postgres + OPA) via Docker Compose:

```bash
docker compose up
```

Or the gateway alone against local dependencies:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

The app reads `REDIS_URL` (default `redis://localhost:6379`) and `OPA_URL` (default
`http://localhost:8181/v1/data/authz`) from the environment; Compose sets both to point
at the sibling containers.

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
request handling (see `tests/`). A subset (`tests/test_opa_docker_integration.py`)
spins up the real `openpolicyagent/opa` image against `policies/` and is skipped
automatically if Docker isn't available.

Rego policy tests run separately, via `opa test`:

```bash
docker run --rm -v "$(pwd)/policies:/policies" openpolicyagent/opa:latest test /policies -v
```

Both suites run in CI on every PR (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## Documentation

| Doc | Contents |
|-----|----------|
| [`docs/roadmap.md`](docs/roadmap.md) | Phased build plan and current progress |
| [`docs/architecture.md`](docs/architecture.md) | Full target-state system design |
| [`docs/api-design.md`](docs/api-design.md) | MCP + admin API surface |
| [`docs/tool-spec.md`](docs/tool-spec.md) | Per-tool input/output schemas and policy constraints |
| [`docs/threat-model.md`](docs/threat-model.md) | STRIDE threat model and mitigations |
| [`docs/rego-policies.md`](docs/rego-policies.md) | Rego policy structure, for policy authors |
