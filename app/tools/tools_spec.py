"""Tool executors.

Each function is the executor for one MCP tool, registered in the TOOLS dict
in app/main.py. Phase 3 (docs/roadmap.md) implements the 7 read-only
executors against real downstream APIs (K8s, Terraform Cloud, Jenkins,
Prometheus, PagerDuty/Jira). Phase 4 implements the 3 write executors
(`restart_deployment`, `scale_deployment`, `trigger_jenkins_job`) plus the
human-in-the-loop approval gate (app/approvals.py, app/slack.py): a prod call
that OPA flags `require_approval` is held pending in Redis and only reaches
these functions if a human approves via the Slack callback
(`POST /admin/approvals/{id}/decide` in app/main.py); a denial or expiry
never calls the executor at all. `open_ticket` remains a stub - it's outside
Phase 4's tool list (only the three above), not blocked on the gate itself.

Every executor:
- validates the arguments it needs and raises ExecutorError("validation_error", ...)
  if they're missing (full JSON-Schema validation per docs/tool-spec.md is the
  interceptor's job across all tools; this is just enough to call the API safely).
- bounds every downstream call with the shared executor timeout
  (app/tools/config.py: 10s default, 30s max).
- raises ExecutorError with a docs/tool-spec.md `error_type`
  (not_found / executor_timeout / upstream_error / validation_error) on
  failure instead of leaking the raw client exception; app/main.py maps that
  to the MCP error response's status code and `data.error_type`.
"""
import os
from datetime import datetime, timezone

import httpx

from . import k8s_client as k8s
from .config import get_executor_timeout
from .errors import ExecutorError
from .k8s_client import apps_v1_api, core_v1_api

# --- Kubernetes -------------------------------------------------------------


def _require_reason(arguments: dict) -> str:
    """restart_deployment, scale_deployment, and trigger_jenkins_job all
    require a `reason` (10-500 chars per docs/tool-spec.md) that ends up in
    the audit log - shared here instead of duplicated in each of the three."""
    reason = (arguments.get("reason") or "").strip()
    if not (10 <= len(reason) <= 500):
        raise ExecutorError("validation_error", "'reason' is required and must be 10-500 characters")
    return reason


def get_pod_logs(arguments: dict):
    namespace = arguments.get("namespace")
    pod_name = arguments.get("pod_name")
    if not namespace or not pod_name:
        raise ExecutorError("validation_error", "'namespace' and 'pod_name' are required")

    kwargs = {}
    if arguments.get("container"):
        kwargs["container"] = arguments["container"]
    # since_seconds and tail_lines are mutually exclusive per docs/tool-spec.md;
    # since_seconds wins if both are somehow given, tail_lines(100) is the default.
    if arguments.get("since_seconds"):
        kwargs["since_seconds"] = arguments["since_seconds"]
    else:
        kwargs["tail_lines"] = arguments.get("tail_lines", 100)

    logs = k8s.run(
        core_v1_api().read_namespaced_pod_log,
        f"pod '{pod_name}' in namespace '{namespace}'",
        name=pod_name,
        namespace=namespace,
        **kwargs,
    )

    # A pod with no logs yet (e.g. still Pending) returns "" rather than an
    # error - docs/tool-spec.md calls this out as a distinct failure mode
    # from "pod not found", so it's a normal (empty) result, not raised.
    lines = logs.splitlines() if logs else []
    return {
        "namespace": namespace,
        "pod_name": pod_name,
        "container": arguments.get("container"),
        "lines_returned": len(lines),
        "logs": logs,
    }


def list_pods(arguments: dict):
    namespace = arguments.get("namespace")
    if not namespace:
        raise ExecutorError("validation_error", "'namespace' is required")

    kwargs = {}
    if arguments.get("label_selector"):
        kwargs["label_selector"] = arguments["label_selector"]
    if arguments.get("field_selector"):
        kwargs["field_selector"] = arguments["field_selector"]

    result = k8s.run(
        core_v1_api().list_namespaced_pod,
        f"pods in namespace '{namespace}'",
        namespace=namespace,
        **kwargs,
    )

    now = datetime.now(timezone.utc)
    pods = []
    for pod in result.items:
        # container_statuses is None (not []) before the scheduler has
        # placed the pod, so ready/restart counts default to 0/0 for it.
        statuses = pod.status.container_statuses or []
        ready_count = sum(1 for s in statuses if s.ready)
        created = pod.metadata.creation_timestamp
        pods.append(
            {
                "name": pod.metadata.name,
                "ready": f"{ready_count}/{len(statuses)}",
                "status": pod.status.phase,
                "restarts": sum(s.restart_count for s in statuses),
                "age_seconds": int((now - created).total_seconds()) if created else None,
            }
        )
    return {"namespace": namespace, "pods": pods}


def get_deployment_status(arguments: dict):
    namespace = arguments.get("namespace")
    deployment = arguments.get("deployment")
    if not namespace or not deployment:
        raise ExecutorError("validation_error", "'namespace' and 'deployment' are required")

    dep = k8s.run(
        apps_v1_api().read_namespaced_deployment,
        f"deployment '{deployment}' in namespace '{namespace}'",
        name=deployment,
        namespace=namespace,
    )

    containers = dep.spec.template.spec.containers or []
    conditions = [
        {
            "type": c.type,
            "status": c.status,
            **({"reason": c.reason} if c.reason else {}),
            **({"message": c.message} if c.message else {}),
        }
        for c in (dep.status.conditions or [])
    ]
    # "Last updated" = the most recent condition transition, falling back to
    # the deployment's own creation time if it has no conditions yet.
    update_times = [c.last_update_time for c in (dep.status.conditions or []) if c.last_update_time]
    last_updated = max(update_times) if update_times else dep.metadata.creation_timestamp

    return {
        "name": dep.metadata.name,
        "namespace": dep.metadata.namespace,
        "desired_replicas": dep.spec.replicas,
        "ready_replicas": dep.status.ready_replicas or 0,
        "available_replicas": dep.status.available_replicas or 0,
        "image": containers[0].image if containers else None,
        "conditions": conditions,
        "last_updated": last_updated.isoformat() if last_updated else None,
    }


def restart_deployment(arguments: dict):
    namespace = arguments.get("namespace")
    deployment = arguments.get("deployment")
    if not namespace or not deployment:
        raise ExecutorError("validation_error", "'namespace' and 'deployment' are required")
    _require_reason(arguments)

    # Same mechanism `kubectl rollout restart` uses: a strategic-merge patch
    # that bumps the pod template's restartedAt annotation, which forces a
    # new ReplicaSet rollout without changing the image/spec otherwise.
    # Namespace/role/environment gating (e.g. never in kube-system, prod
    # needs approval) is OPA's job upstream of this call, not re-checked here.
    restarted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    patch = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": restarted_at}}}}}
    k8s.run(
        apps_v1_api().patch_namespaced_deployment,
        f"deployment '{deployment}' in namespace '{namespace}'",
        name=deployment,
        namespace=namespace,
        body=patch,
    )

    return {
        "status": "restart_initiated",
        "deployment": deployment,
        "namespace": namespace,
        "restarted_at": restarted_at,
        "message": "Rolling restart triggered. Monitor with get_deployment_status.",
    }


def scale_deployment(arguments: dict):
    namespace = arguments.get("namespace")
    deployment = arguments.get("deployment")
    replicas = arguments.get("replicas")
    if not namespace or not deployment:
        raise ExecutorError("validation_error", "'namespace' and 'deployment' are required")
    # isinstance(x, bool) check because bool is a subclass of int in Python -
    # without it, `replicas: true` would silently pass as replicas=1.
    if not isinstance(replicas, int) or isinstance(replicas, bool) or not (0 <= replicas <= 20):
        raise ExecutorError("validation_error", "'replicas' is required and must be an integer 0-20")
    _require_reason(arguments)

    # patch_namespaced_deployment_scale hits the /scale subresource, so it
    # can't touch anything but replica count - no risk of this call
    # clobbering an unrelated spec field the way a full deployment patch could.
    k8s.run(
        apps_v1_api().patch_namespaced_deployment_scale,
        f"deployment '{deployment}' in namespace '{namespace}'",
        name=deployment,
        namespace=namespace,
        body={"spec": {"replicas": replicas}},
    )

    return {
        "status": "scale_initiated",
        "deployment": deployment,
        "namespace": namespace,
        "replicas": replicas,
        "scaled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message": "Scale request submitted. Monitor with get_deployment_status.",
    }


# --- Terraform Cloud ----------------------------------------------------

TFC_URL = os.getenv("TFC_URL", "https://app.terraform.io/api/v2").rstrip("/")
TFC_ORG = os.getenv("TFC_ORG")
TFC_TOKEN = os.getenv("TFC_TOKEN")


def _send(fn, *, timeout: float, not_found_message: str, upstream_label: str, **kwargs):
    """Shared error-mapping around one httpx.Client.get/post call: timeout ->
    executor_timeout, 404 -> not_found, other 4xx/5xx -> upstream_error. `fn`
    is the bound client method (client.get or client.post) so this one
    function backs both verbs instead of duplicating the try/except twice."""
    try:
        response = fn(**kwargs)
    except httpx.TimeoutException as exc:
        raise ExecutorError("executor_timeout", f"{upstream_label} timed out after {timeout}s") from exc
    except httpx.HTTPError as exc:
        raise ExecutorError("upstream_error", f"{upstream_label} unreachable: {exc}") from exc

    if response.status_code == 404:
        raise ExecutorError("not_found", not_found_message)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ExecutorError("upstream_error", f"{upstream_label} error ({response.status_code})") from exc
    return response


def _http_get(client: httpx.Client, path: str, *, not_found_message: str, upstream_label: str, **kwargs):
    """GET + error-mapping for the read-only HTTP executors (Terraform Cloud,
    Jenkins, Prometheus, PagerDuty, Jira)."""
    # httpx.Client(timeout=X) fans a single float out to connect/read/write/
    # pool, all equal to X - .connect just reads back the value the client
    # was built with, for the timeout error message below.
    return _send(
        lambda **kw: client.get(path, **kw),
        timeout=client.timeout.connect,
        not_found_message=not_found_message,
        upstream_label=upstream_label,
        **kwargs,
    )


def _http_post(client: httpx.Client, path: str, *, not_found_message: str, upstream_label: str, **kwargs):
    """POST + error-mapping, for trigger_jenkins_job (the one write tool
    that's a plain HTTP call rather than a K8s patch)."""
    return _send(
        lambda **kw: client.post(path, **kw),
        timeout=client.timeout.connect,
        not_found_message=not_found_message,
        upstream_label=upstream_label,
        **kwargs,
    )


def query_terraform_plan(arguments: dict):
    workspace = arguments.get("workspace")
    if not workspace:
        raise ExecutorError("validation_error", "'workspace' is required")
    if not TFC_TOKEN or not TFC_ORG:
        raise ExecutorError("upstream_error", "Terraform Cloud is not configured (TFC_TOKEN/TFC_ORG)")

    headers = {"Authorization": f"Bearer {TFC_TOKEN}"}
    with httpx.Client(base_url=TFC_URL, headers=headers, timeout=get_executor_timeout()) as http:
        ws_resp = _http_get(
            http,
            f"/organizations/{TFC_ORG}/workspaces/{workspace}",
            not_found_message=f"Terraform Cloud workspace '{workspace}' not found",
            upstream_label="Terraform Cloud",
        )
        workspace_id = ws_resp.json()["data"]["id"]

        # Runs are returned newest-first by default, so the first page-1 row
        # is the latest run - no query-time filtering by status is needed
        # to get "the latest plan".
        runs_resp = _http_get(
            http,
            f"/workspaces/{workspace_id}/runs",
            not_found_message=f"no runs found for workspace '{workspace}'",
            upstream_label="Terraform Cloud",
            params={"page[size]": 1},
        )
        runs = runs_resp.json()["data"]
        if not runs:
            return {"workspace": workspace, "run_id": None, "status": "no_runs", "changes": None}

        run = runs[0]
        attrs = run["attributes"]
        plan_id = (run.get("relationships", {}).get("plan", {}).get("data") or {}).get("id")

        # The run summary alone has no add/change/destroy counts - those
        # live on the plan resource, so a second call is needed to fill in
        # `changes` (skipped entirely if this run has no plan yet).
        changes = None
        plan_data = None
        if plan_id:
            plan_resp = _http_get(
                http,
                f"/plans/{plan_id}",
                not_found_message=f"plan '{plan_id}' not found",
                upstream_label="Terraform Cloud",
            )
            plan_data = plan_resp.json()["data"]
            plan_attrs = plan_data["attributes"]
            changes = {
                "add": plan_attrs.get("resource-additions", 0),
                "change": plan_attrs.get("resource-changes", 0),
                "destroy": plan_attrs.get("resource-destructions", 0),
            }

        result = {
            "workspace": workspace,
            "run_id": run["id"],
            "status": attrs.get("status"),
            "created_at": attrs.get("created-at"),
            "triggered_by": attrs.get("trigger-reason", "unknown"),
            "changes": changes,
        }
        if arguments.get("include_full_plan"):
            result["full_plan"] = plan_data
        return result


# --- Jenkins --------------------------------------------------------------

JENKINS_URL = os.getenv("JENKINS_URL", "").rstrip("/")
JENKINS_USER = os.getenv("JENKINS_USER")
JENKINS_API_TOKEN = os.getenv("JENKINS_API_TOKEN")


def _epoch_ms_to_iso(epoch_ms: int | None) -> str | None:
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()


def get_jenkins_job_status(arguments: dict):
    job_name = arguments.get("job_name")
    if not job_name:
        raise ExecutorError("validation_error", "'job_name' is required")
    if not JENKINS_URL:
        raise ExecutorError("upstream_error", "Jenkins is not configured (JENKINS_URL)")

    build_number = arguments.get("build_number", "lastBuild")
    # Jenkins folder-style job names ("deploy/checkout-api") map to nested
    # /job/deploy/job/checkout-api URL segments, not a single path component.
    job_path = "/job/" + "/job/".join(job_name.split("/"))
    auth = (JENKINS_USER, JENKINS_API_TOKEN) if JENKINS_USER else None

    with httpx.Client(base_url=JENKINS_URL, auth=auth, timeout=get_executor_timeout()) as http:
        response = _http_get(
            http,
            f"{job_path}/{build_number}/api/json",
            not_found_message=f"Jenkins job '{job_name}' build '{build_number}' not found",
            upstream_label="Jenkins",
        )

    data = response.json()
    # "Who triggered this" lives in a build-cause action, not a top-level
    # field - scan for the first cause with a userId (human) or fall back
    # to its description (e.g. "Started by upstream project").
    triggered_by = "unknown"
    for action in data.get("actions", []):
        for cause in action.get("causes", []):
            triggered_by = cause.get("userId") or cause.get("shortDescription", "unknown")
            break
        else:
            continue
        break

    timestamp_ms = data.get("timestamp")
    duration_ms = data.get("duration")
    finished_ms = timestamp_ms + duration_ms if timestamp_ms is not None and duration_ms else None

    return {
        "job_name": job_name,
        "build_number": data.get("number"),
        # `result` is null while a build is still running.
        "status": data.get("result") or ("IN_PROGRESS" if data.get("building") else "UNKNOWN"),
        "duration_ms": duration_ms,
        "started_at": _epoch_ms_to_iso(timestamp_ms),
        "finished_at": _epoch_ms_to_iso(finished_ms),
        "triggered_by": triggered_by,
        "url": data.get("url"),
    }


def _jenkins_crumb(http: httpx.Client) -> dict:
    """Best-effort CSRF crumb header for Jenkins POSTs. Jenkins rejects
    unsafe methods without one when crumb protection is enabled (the
    default); an instance with it disabled just 404s here, which is treated
    as "no crumb needed" rather than a hard failure."""
    try:
        resp = http.get("/crumbIssuer/api/json")
    except httpx.HTTPError:
        return {}
    if resp.status_code != 200:
        return {}
    data = resp.json()
    return {data["crumbRequestField"]: data["crumb"]}


def trigger_jenkins_job(arguments: dict):
    job_name = arguments.get("job_name")
    if not job_name:
        raise ExecutorError("validation_error", "'job_name' is required")
    _require_reason(arguments)
    if not JENKINS_URL:
        raise ExecutorError("upstream_error", "Jenkins is not configured (JENKINS_URL)")

    # Per-job parameter allowlisting ("only parameters defined in the job's
    # allowed_params list are accepted") is an OPA policy constraint per
    # docs/tool-spec.md, enforced before the call ever reaches this executor.
    parameters = arguments.get("parameters") or {}
    job_path = "/job/" + "/job/".join(job_name.split("/"))
    auth = (JENKINS_USER, JENKINS_API_TOKEN) if JENKINS_USER else None

    with httpx.Client(base_url=JENKINS_URL, auth=auth, timeout=get_executor_timeout()) as http:
        response = _http_post(
            http,
            f"{job_path}/buildWithParameters",
            not_found_message=f"Jenkins job '{job_name}' not found",
            upstream_label="Jenkins",
            params=parameters,
            headers=_jenkins_crumb(http),
        )

    # Jenkins queues the build and responds 201 with a Location header
    # pointing at the queue item, not the build itself - the actual build
    # number isn't known until it leaves the queue, hence null here (per
    # docs/tool-spec.md's documented output shape; poll with
    # get_jenkins_job_status once it's running).
    return {
        "status": "triggered",
        "job_name": job_name,
        "build_number": None,
        "queue_item_url": response.headers.get("Location"),
        "message": "Build queued. Use get_jenkins_job_status to poll for completion.",
    }


# --- Prometheus -------------------------------------------------------------

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")


def read_prometheus_metrics(arguments: dict):
    query = arguments.get("query")
    if not query:
        raise ExecutorError("validation_error", "'query' is required")

    # Presence of start/end/step selects the range endpoint; otherwise this
    # is an instant query, per docs/tool-spec.md's input schema.
    is_range = any(arguments.get(k) for k in ("start", "end", "step"))
    if is_range:
        path = "/api/v1/query_range"
        params = {"query": query, "start": arguments.get("start"), "end": arguments.get("end"), "step": arguments.get("step")}
    else:
        path = "/api/v1/query"
        params = {"query": query}
        if arguments.get("time"):
            params["time"] = arguments["time"]

    with httpx.Client(base_url=PROMETHEUS_URL, timeout=get_executor_timeout()) as http:
        response = _http_get(
            http,
            path,
            not_found_message="Prometheus endpoint not found",
            upstream_label="Prometheus",
            params=params,
        )

    body = response.json()
    # Prometheus returns 200 even for a query error, with status="error" in
    # the body - HTTP status alone doesn't tell you the query failed.
    if body.get("status") != "success":
        raise ExecutorError("upstream_error", f"Prometheus query failed: {body.get('error', 'unknown error')}")

    data = body.get("data", {})
    return {"query": query, "result_type": data.get("resultType"), "result": data.get("result", [])}


# --- Ticketing (PagerDuty / Jira) --------------------------------------

PAGERDUTY_URL = os.getenv("PAGERDUTY_URL", "https://api.pagerduty.com").rstrip("/")
PAGERDUTY_API_TOKEN = os.getenv("PAGERDUTY_API_TOKEN")
JIRA_URL = os.getenv("JIRA_URL", "").rstrip("/")
JIRA_USER = os.getenv("JIRA_USER")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")


def open_ticket(arguments: dict):
    # Not in Phase 4's scope (only restart_deployment/scale_deployment/
    # trigger_jenkins_job) - stays a stub. PagerDuty severity:critical
    # requires approval per docs/tool-spec.md, same unbuilt-approval-gate
    # blocker as the other write tools.
    return {"status": "success"}


def read_ticket(arguments: dict):
    system = arguments.get("system")
    ticket_id = arguments.get("ticket_id")
    if system not in ("pagerduty", "jira") or not ticket_id:
        raise ExecutorError("validation_error", "'system' (pagerduty|jira) and 'ticket_id' are required")

    if system == "pagerduty":
        return _read_pagerduty_incident(ticket_id)
    return _read_jira_issue(ticket_id)


def _read_pagerduty_incident(ticket_id: str):
    if not PAGERDUTY_API_TOKEN:
        raise ExecutorError("upstream_error", "PagerDuty is not configured (PAGERDUTY_API_TOKEN)")

    headers = {
        "Authorization": f"Token token={PAGERDUTY_API_TOKEN}",
        "Accept": "application/vnd.pagerduty+json;version=2",
    }
    with httpx.Client(base_url=PAGERDUTY_URL, headers=headers, timeout=get_executor_timeout()) as http:
        response = _http_get(
            http,
            f"/incidents/{ticket_id}",
            not_found_message=f"PagerDuty incident '{ticket_id}' not found",
            upstream_label="PagerDuty",
        )

    incident = response.json()["incident"]
    return {
        "system": "pagerduty",
        "ticket_id": incident["id"],
        "status": incident.get("status"),
        "title": incident.get("title"),
        "severity": (incident.get("priority") or {}).get("summary") or incident.get("urgency"),
        "created_at": incident.get("created_at"),
        "assigned_to": next(
            (a["assignee"]["summary"] for a in incident.get("assignments", []) if a.get("assignee")), None
        ),
        "last_activity": incident.get("last_status_change_at"),
        # PagerDuty's incident payload doesn't include a note count inline
        # (it's a separate /notes call); left null rather than firing an
        # extra request most callers of read_ticket won't need.
        "notes_count": None,
    }


def _read_jira_issue(ticket_id: str):
    if not JIRA_URL or not JIRA_USER or not JIRA_API_TOKEN:
        raise ExecutorError("upstream_error", "Jira is not configured (JIRA_URL/JIRA_USER/JIRA_API_TOKEN)")

    with httpx.Client(base_url=JIRA_URL, auth=(JIRA_USER, JIRA_API_TOKEN), timeout=get_executor_timeout()) as http:
        response = _http_get(
            http,
            f"/rest/api/2/issue/{ticket_id}",
            not_found_message=f"Jira issue '{ticket_id}' not found",
            upstream_label="Jira",
        )

    fields = response.json()["fields"]
    return {
        "system": "jira",
        "ticket_id": ticket_id,
        "status": (fields.get("status") or {}).get("name"),
        "title": fields.get("summary"),
        "severity": (fields.get("priority") or {}).get("name"),
        "created_at": fields.get("created"),
        "assigned_to": (fields.get("assignee") or {}).get("emailAddress"),
        "last_activity": fields.get("updated"),
        "notes_count": (fields.get("comment") or {}).get("total"),
    }
