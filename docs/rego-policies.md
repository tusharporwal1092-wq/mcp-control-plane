# Rego Policy Structure

How `policies/authz.rego` is organized, for anyone adding a role or a tool
constraint. For the target-state policy design (full RBAC model, argument
constraints per tool) see `docs/architecture.md` §3.2 and `docs/tool-spec.md`;
this doc covers the policy as actually implemented today.

## Files

| File | Purpose |
|------|---------|
| `policies/authz.rego` | The `authz` package: `allow` / `require_approval` rules |
| `policies/authz_test.rego` | `opa test` cases for `authz` |
| `policies/data.json` | Static role registry, loaded into `data.roles` / `data.destructive_tools` |

`data.json` is merged into the root of `data` because it's named exactly
`data.json` — that's an OPA convention, not something this project configured.
Renaming it would silently stop the roles from loading.

## Input document

Built by `ToolCallContext.to_opa_input()` in `app/interceptor.py`:

```json
{
  "agent": {"id": "agent01", "role": "sre1"},
  "tool": {"name": "restart_deployment", "args": {"namespace": "prod-payments"}},
  "resource": {"namespace": "prod-payments"},
  "environment": "prod"
}
```

`environment` is *inferred*, not passed by the caller: `_infer_environment()`
prefix-matches the `namespace` argument against `dev` / `staging` / `prod`,
and falls back to `"unknown"` for tools with no `namespace` argument
(Jenkins, ticketing, ...). There's no environment field in the MCP request —
if that stops being good enough (e.g. multiple namespaces don't share a
prefix convention), the fix is a real `environment` argument on the
tool call, not a smarter guesser here.

## Output document

```json
{"allow": true, "require_approval": false}
```

Read by `app/authz/opa.py`. `reason` is *not* part of OPA's output — the
gateway synthesizes it from `allow`/`require_approval` so Rego doesn't need
string-building. If per-rule reasons become necessary, add a `reason` rule to
`authz.rego` and have `opa.py` prefer it when present.

## Rule groups

Four independent conditions, all of which must hold for `allow`:

- **`tool_allowed`** — base policy. `input.tool.name` must be in the role's
  `allowed_tools` (from `data.roles`).
- **`environment_allowed`** — environment policy. `input.environment` must be
  in the role's `allowed_environments`, *unless* it's `"unknown"` (a tool with
  no namespace has nothing to check against, so it isn't blocked here).
- **`namespace_allowed`** — argument policy. `input.resource.namespace` must
  be in the role's `allowed_namespaces`, or that list contains `"*"`, or
  there's no namespace to check (same bypass logic as above).
- **`kube_system_restart_blocked`** (inverted: `not kube_system_restart_blocked`)
  — a hard override, not role-dependent: `restart_deployment` against
  `kube-system` is always denied, per `docs/tool-spec.md`.

`require_approval` only evaluates once `allow` is already true: it adds a
second condition (`input.tool.name in data.destructive_tools` and
`input.environment == "prod"`) on top. `allow: true, require_approval: true`
means "permitted, but gated on a human" — see the `-32010` branch in
`app/main.py`. The actual approval workflow (Slack, Redis TTL) is a later
phase; today the gateway just returns 403 with that decision surfaced.

## Adding a role

Add an entry to `data.roles` in `policies/data.json`:

```json
"my-role": {
  "allowed_tools": ["get_pod_logs"],
  "allowed_environments": ["dev", "staging"],
  "allowed_namespaces": ["dev-payments", "staging-payments"]
}
```

No Rego changes needed — the rules read `role_config` generically via
`data.roles[input.agent.role]`. An unrecognized role makes `role_config`
undefined, which makes every rule referencing it undefined, which is `false`
by the `default allow := false` — unknown roles fail closed automatically.

If you add a role here, an agent actually using it also needs a matching
`allowed_tools` entry in `API_KEYS` (`app/middleware/auth.py`) — that's a
separate, coarser pre-OPA scope check the gateway does before a call ever
reaches OPA. Whatever isn't in the agent's `allowed_tools` never reaches this
policy at all, regardless of what the role would permit.

## Running the tests

```bash
docker run --rm -v "$(pwd)/policies:/policies" openpolicyagent/opa:latest test /policies -v
```

`.github/workflows/ci.yml` runs this on every PR. `policies/authz_test.rego`
covers the full role × tool × environment matrix (generically, via `every`
over `data.roles`) plus the specific scenarios called out in the roadmap
(SRE prod access, approval-required, the `kube-system` override).
