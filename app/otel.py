"""OpenTelemetry setup: tracing + metrics for the gateway (docs/roadmap.md Phase 6).

Single place that builds the TracerProvider/MeterProvider and the manual
instruments every other module imports, so nobody else creates their own
provider or duplicates a counter definition. Exports over OTLP/HTTP to the
collector sidecar (docker-compose.yaml `otel-collector` service locally; an
EKS DaemonSet in prod - either way the gateway only ever talks to
`localhost`/`otel-collector`, never Tempo/Prometheus directly).

`instrument_app()` wires up automatic request tracing + HTTP metrics
(http.server.duration, request/response sizes, ...) via
opentelemetry-instrumentation-fastapi; everything below is the *manual*
half - the `authn`/`policy_eval`/`executor`/`audit_write` spans and the
business counters/histogram - added at their one shared call site in each
owning module (app/middleware/auth.py, app/authz/opa.py, app/main.py,
app/audit.py, app/middleware/rate_limit.py, app/approvals.py) rather than
threaded through every caller.

Real OTLP export only turns on when `OTEL_EXPORTER_OTLP_ENDPOINT` is set
(docker-compose.yaml sets it to the `otel-collector` service; a real EKS
deployment would set it to the DaemonSet). Without it - `uv run uvicorn`/
`uv run pytest` outside `docker compose up` - spans and metrics are still
created (every call site below still runs, so it's still exercised by the
test suite), just never handed to a network exporter: tried the opposite
first (always build an OTLPSpanExporter, let it fail closed against
localhost:4318 like the rate limiter fails open against Redis) and measured
it adding 8-12s of dead time to `uv run pytest` - the exporter's own
retry/backoff loop blocks Python's interpreter shutdown until it gives up,
since its worker thread is stuck in blocking socket I/O the process can't
just kill out from under it. Not attempting the connection at all avoids
that failure mode entirely instead of tuning around it.
"""
import os

from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "mcp-gateway")
# Collector's OTLP/HTTP receiver (port 4318) - see docker-compose.yaml.
# opentelemetry-exporter-otlp-proto-http appends the /v1/traces and
# /v1/metrics signal paths itself, so this is just scheme+host+port. Unset
# (the local/test default) means "don't export at all" - see module
# docstring.
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

_resource = Resource.create({"service.name": SERVICE_NAME})

tracer_provider = TracerProvider(resource=_resource)
meter_provider = MeterProvider(resource=_resource)

if OTLP_ENDPOINT:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT}/v1/traces"))
    )
    meter_provider = MeterProvider(
        resource=_resource,
        metric_readers=[
            PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{OTLP_ENDPOINT}/v1/metrics"))
        ],
    )

trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("mcp_control_plane")
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("mcp_control_plane")

# --- Deliverable's counter/histogram metrics --------------------------------
# Names/labels chosen to match docs/roadmap.md Phase 6 and the Grafana
# dashboards in observability/grafana/dashboards/ 1:1 - changing a name here
# means updating the matching dashboard JSON's PromQL too.

tool_calls_total = meter.create_counter(
    "tool_calls_total",
    description="Every completed tool call, labeled by tool_name, result_status, and role",
)
policy_denials_total = meter.create_counter(
    "policy_denials_total",
    description="Tool calls denied by OPA policy (including fail-closed/unreachable), by tool_name and role",
)
approval_requests_total = meter.create_counter(
    "approval_requests_total",
    description="Tool calls that required human approval, by tool_name",
)
rate_limit_hits_total = meter.create_counter(
    "rate_limit_hits_total",
    description="Requests rejected for exceeding the per-agent rate limit, by agent_id",
)
tool_call_duration_ms = meter.create_histogram(
    "tool_call_duration_ms",
    description="End-to-end tool call duration (request receipt to audit write), by tool_name",
    unit="ms",
)

# Not in docs/roadmap.md Phase 6's named counter/histogram list, but needed
# to actually populate the "Approval Queue" dashboard it also asks for
# (pending count, average approval time) - approval_requests_total alone
# only tells you how many were ever opened, never how many are still open.
# ponytail: approvals_pending drifts high if a pending approval silently
# expires (15-min Redis TTL, app/approvals.py) without anyone ever calling
# decide_approval - nothing decrements it for an expiry nobody observed.
# Fine for a dashboard gauge at this app's approval volume; upgrade path is
# a periodic reconciliation against a live Redis SCAN of "approval:*" keys.
approvals_pending = meter.create_up_down_counter(
    "approvals_pending",
    description="Approvals currently awaiting a Slack decision (best-effort - see ponytail note in source)",
)
approval_decision_duration_ms = meter.create_histogram(
    "approval_decision_duration_ms",
    description="Time from an approval request being opened to it being approved/denied",
    unit="ms",
)
# Backs the roadmap's "audit log hash chain failure -> PagerDuty" alert.
# Piggybacks on the integrity check GET /admin/audit already runs on every
# query (app/audit.py::verify_rows) rather than adding a new background
# poller - something still has to call that endpoint on a schedule for this
# to actually catch a break promptly (a synthetic monitor/cron hitting
# GET /admin/audit periodically), which is outside Phase 6's scope here.
audit_chain_integrity_failures_total = meter.create_counter(
    "audit_chain_integrity_failures_total",
    description="Incremented whenever GET /admin/audit's hash-chain verification finds a broken row",
)


def instrument_app(app) -> None:
    """Auto-instrument the FastAPI app: one server span per request plus
    the standard http.server.* metrics. Call once, right after `FastAPI()`
    (app/main.py) - instrumenting twice raises."""
    FastAPIInstrumentor.instrument_app(app)
