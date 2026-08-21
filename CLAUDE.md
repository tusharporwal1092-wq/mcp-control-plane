# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A hardened FastAPI gateway that sits between AI agents (LLM clients) and real infrastructure APIs
(Kubernetes, Terraform Cloud, Jenkins, Prometheus, PagerDuty/Jira), speaking MCP JSON-RPC. Every
tool call is authenticated, rate-limited, policy-checked via OPA/Rego, and audited before it
reaches an executor. **The model is treated as an untrusted caller — the policy engine, not the
prompt, is the security boundary.** Full target-state design lives in `docs/` (architecture,
threat model, API design, tool spec, roadmap); `docs/roadmap.md` tracks what's actually built vs.
still planned, phase by phase — check it before assuming a described feature exists.

## Commands

```bash
uv sync --extra export                                           # install deps (the `export` extra pulls in boto3 - tests/test_export_script.py imports scripts/export_audit_to_s3.py unconditionally, so a bare `uv sync` leaves the full pytest run unable to even collect)
uv run alembic upgrade head                                      # apply audit_log/approvals schema (needs Postgres reachable)
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload  # run gateway alone
docker compose -f docker-compose.yaml up                         # gateway + Redis + Postgres + OPA
docker compose run --rm gateway alembic upgrade head              # apply schema against Compose's Postgres

uv run pytest                                                     # full test suite
uv run pytest tests/test_approvals.py                             # one file
uv run pytest tests/test_approvals.py::test_forged_signature_is_rejected_with_401  # one test
uv run pytest -k "approval"                                       # by name pattern

docker run --rm -v "$(pwd)/policies:/policies" openpolicyagent/opa:latest test /policies -v  # Rego tests

uv run alembic revision -m "description"                         # new migration (hand-write the SQL, no autogenerate - see migrations/env.py)
```

CI (`.github/workflows/ci.yml`) currently only runs `opa test policies -v`; the Python test suite
is not yet wired into CI — run `uv run pytest` manually before relying on it. A separate scheduled
workflow (`.github/workflows/audit-export.yml`) runs `scripts/export_audit_to_s3.py` daily but
no-ops unless `AUDIT_LOG_*` secrets are configured (no deployed environment exists yet).

Two test files spin up real Docker containers and are skipped automatically if Docker isn't
running — they're the only suites that hit real external services; everything else fakes/stubs
Redis, Postgres, OPA, Kubernetes, and Slack (see "Testing patterns" below):
- `tests/test_opa_docker_integration.py` — real `openpolicyagent/opa` against `policies/`
- `tests/test_audit_docker_integration.py` — real `postgres:16-alpine`, migrated with the actual
  `alembic upgrade head` (not a hand-copied schema), exercising the hash chain end-to-end

No linter is configured/installed in this environment (`ruff` is not available) — don't assume
`ruff check` works.

## Architecture

Request flow through the middleware stack and `/mcp` handler:

```
Agent → POST /mcp → [log_requests] → [authenticate] → [rate_limit] → interceptor → OPA → executor → audit log
```

Starlette runs the *last-added* middleware first, so registration order in `app/main.py` (bottom
of the file) is deliberately `rate_limit`, then `authenticate`, then `log_requests` — producing
`log_requests → authenticate → rate_limit → handler` at request time. `authenticate` must run
before `rate_limit` because the limiter reads `request.state.agent`.

Key modules, each owning one concern (`app/main.py` is HTTP wiring only — the logic is delegated):

- `app/middleware/auth.py` — resolves `x-api-key` → `Agent_data` (id, role, allowed_tools,
  rate_limit_rpm), attached to `request.state.agent`. `API_KEYS` is a static in-memory dict (the
  real identity store is a later phase). `PUBLIC_PATHS`/`PUBLIC_PATH_PREFIXES` bypass this check
  for health endpoints and `/admin/approvals/*` (which authenticate differently — see below).
- `app/middleware/rate_limit.py` — Redis sorted-set sliding window, keyed by `agent_id`. Fails
  **open** (lets the request through) if Redis errors — availability over strict enforcement here.
- `app/interceptor.py` — turns raw `tools/call` params into a `ToolCallContext`, the single object
  every downstream consumer (OPA input, audit log, executor args) derives from. Also infers
  `environment` (`prod`/`staging`/`dev`/`unknown`) from a `namespace` argument prefix.
- `app/authz/opa.py` — POSTs `ToolCallContext.to_opa_input()` to the OPA sidecar, returns
  `PolicyDecision(allow, require_approval, reason)`. Fails **closed** (denies) if OPA is
  unreachable — unlike the rate limiter, an unavailable policy engine must never become a bypass.
- `app/tools/tools_spec.py` — one function per MCP tool, registered in `app/main.py`'s `TOOLS`
  dict. Each validates its own args and raises `ExecutorError(error_type, message)` rather than
  leaking raw client exceptions; `app/main.py` maps `error_type` (`validation_error`, `not_found`,
  `permission_denied`, `executor_timeout`, `upstream_error`) to an HTTP status and MCP error shape.
  Missing downstream config (e.g. no `JENKINS_URL`) raises `upstream_error` instead of crashing.
- `app/tools/k8s_client.py` — cached `CoreV1Api`/`AppsV1Api` clients (in-cluster config in prod,
  `KUBECONFIG`/`~/.kube/config` locally) and the shared `run()` wrapper that maps K8s
  `ApiException`/timeout to `ExecutorError`.
- `app/approvals.py`, `app/slack.py`, `app/sse_hub.py` — the human-in-the-loop approval gate for
  `require_approval` policy decisions (destructive actions in prod). A pending approval is
  persisted to Redis with a 15-minute TTL; Slack gets a notification via Incoming Webhook;
  `POST /admin/approvals/{id}/decide` is the Slack callback target, authenticated by HMAC
  (`X-Slack-Signature` + timestamp replay check in `slack.verify_signature`) rather than the agent
  API-key middleware. On approval the executor runs synchronously inside that request and the
  result is pushed to the agent's open `/mcp/sse` connection via the in-process `sse_hub`
  (single-process only — a multi-pod deployment would need this backed by Redis pub/sub instead).
- `app/db.py` — shared asyncpg connection pool (`app.state.db`, created at startup same as the
  Redis client). `DATABASE_URL` env var, no ORM — schema is raw SQL in `migrations/`.
- `app/audit.py` — the hash-chained audit log: `record_tool_call` (async, writes one row per tool
  call), `query_audit_log`/`verify_rows` (`GET /admin/audit`'s filters + `integrity_check`), and
  `export_audit_log` (streaming NDJSON, backs both `GET /admin/audit/export` and
  `scripts/export_audit_to_s3.py`). Every row's `row_hash` chains off the previous row's hash
  (`prev_hash + id + agent_id + tool + args + result + timestamp`, SHA-256); writes are serialized
  with a single Postgres advisory lock (`CHAIN_LOCK_KEY`) so concurrent writers can't both read the
  same `prev_hash`. `migrations/versions/0001_create_audit_tables.py` (Alembic, raw SQL, no ORM
  models — `target_metadata = None` in `migrations/env.py`) creates `audit_log` and a
  currently-unused `approvals` table (schema-only, for `audit_log.approval_id` to conceptually
  point at — nothing writes to it; approval state still lives only in Redis, see above).
- `policies/authz.rego` + `policies/data.json` — the actual Rego policy (role → allowed_tools /
  allowed_environments / allowed_namespaces) and its OPA test suite (`authz_test.rego`), tested
  independently of the Python suite via `opa test`.

### Two authorization layers, deliberately redundant

`app/middleware/auth.py`'s `Agent_data.allowed_tools` is a coarse pre-OPA gate (rejects a tool the
agent's key was never scoped to before any policy evaluation); `policies/authz.rego` is the
fine-grained, environment/namespace-aware check. Keep both in sync when adding a tool or role —
the comment in `auth.py` calls out that its `allowed_tools` lists should mirror `policies/data.json`.

### Error/response shape conventions

MCP responses are hand-built JSON-RPC dicts (`_rpc_result`/`_rpc_error` in `app/main.py`), not a
schema library. `docs/api-design.md` and `docs/tool-spec.md` are the source of truth for exact
field names/types/status codes — when a response shape doesn't obviously follow from the code,
check the docs before guessing, and if you change a shape, update the doc in the same change.

### Testing patterns

- `tests/fakes.py::FakeRedis` implements just the Redis surface the code actually uses (sorted-set
  ops for rate limiting, get/set with `ex`/`keepttl` for approvals) — extend it rather than mocking
  `redis.asyncio.Redis` wholesale.
- `tests/conftest.py`'s `client` fixture wires a `TestClient` with `FakeRedis`, a stub
  `evaluate_policy` that allows everything, and a no-op `create_db_pool`/`record_tool_call` (no
  real Postgres) by default; tests that care about a specific policy outcome or audit content
  override `app_main.evaluate_policy`/`app_main.record_tool_call` themselves (OPA's own rule logic
  and real audit persistence are covered separately in the two `*_docker_integration.py` files).
  A patched `create_db_pool`/`create_redis_client` must be an async function returning the fake,
  not a plain lambda — `startup()` awaits it.
- Tool executor tests (`tests/test_tools_executors.py`) fake the downstream client at the boundary
  the executor actually calls (`kubernetes` client methods, `httpx.Client.get/post`) so the real
  error-mapping logic is exercised, not just the happy path.
- Approval-gate tests (`tests/test_approvals.py`) sign Slack callback bodies with a monkeypatched
  `SLACK_SIGNING_SECRET` and read the in-process `sse_hub` queue directly to assert on the push,
  rather than driving a real streaming HTTP connection.
