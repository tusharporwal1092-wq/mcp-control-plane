Tech Stack: Python, FastAPI, MCP SDK, OPA/Rego, AWS EKS, Terraform, PostgreSQL (audit log), Redis (rate limiting)

High-Level Architecture: LLM client → MCP gateway (authn/authz) → policy engine (OPA) → scoped tool executors (K8s API, Terraform Cloud API, Jenkins API, incident/ticketing API) → immutable audit log. Every tool call is intercepted, policy-checked, rate-limited, and logged before execution.

Core Components:

MCP server exposing 8–12 real tools (get pod logs, restart deployment, query Terraform plan, trigger a Jenkins job, read Prometheus metrics, open/read tickets)
Policy engine defining per-role, per-tool, per-resource permissions (e.g., "agent can restart pods in staging, can only read in prod")
Approval workflow for destructive actions (Slack/webhook-based human-in-the-loop gate)
Full audit trail: who/what agent, what tool, what args, what policy decision, what result

AI Architecture: The LLM is the caller, not the trust boundary — the system assumes the model will occasionally try to do something dumb or be prompt-injected, and the policy layer is the actual security control, not the prompt.

Cloud & Kubernetes Design: Runs as a small EKS deployment (2–3 pods), fronted by an ALB, with the tool executors running with least-privilege IRSA roles scoped per tool.

Infrastructure as Code: 100% Terraform — EKS cluster, IRSA roles, RDS/Postgres for audit log, all environment config. Separate apply/destroy workflows so you're not paying for idle infra.

CI/CD: GitHub Actions — lint/test/security-scan on PR, build+push image, Terraform plan on PR / apply on merge to main, with a manual approval gate for prod.

Observability: OTel traces across every tool call (agent request → policy decision → executor → result), Grafana dashboard showing tool-call volume, denial rate, and latency by tool.

Security: OPA policy-as-code, per-agent API keys with scoped permissions, destructive-action approval gate, full audit logging, secrets in AWS Secrets Manager (never in env vars/code).

Key Engineering Challenges: Designing a policy model expressive enough for real RBAC without becoming unreadable; handling partial failures mid-tool-call safely; making the audit log tamper-evident.

Scalability & Reliability Considerations: Stateless gateway (horizontally scalable), audit log as append-only with retention policy, rate limiting per-agent to prevent runaway tool-call loops (a very real agentic-systems failure mode)