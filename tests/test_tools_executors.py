"""Unit tests for the tool executors in app/tools/tools_spec.py: Phase 3's 7
read-only tools, Phase 4's 3 write tools (restart_deployment,
scale_deployment, trigger_jenkins_job), and Phase 10's 2 higher-risk stretch
tools (exec_into_pod, apply_k8s_manifest).

Each downstream client (Kubernetes, Terraform Cloud, Jenkins, Prometheus,
PagerDuty, Jira) is faked at the boundary the executor actually calls
through, so the real error-mapping logic (kubernetes ApiException/timeout ->
ExecutorError in app/tools/k8s_client.py, httpx status/timeout -> ExecutorError
in tools_spec._send) is exercised for real - only the network call itself
is replaced. exec_into_pod fakes `kubernetes.stream.stream` itself (imported
into tools_spec's namespace) rather than the K8s API client, since the real
call goes through that function, not a plain CoreV1Api method.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from kubernetes.client.exceptions import ApiException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import tools_spec  # noqa: E402
from app.tools.config import get_executor_timeout  # noqa: E402
from app.tools.errors import ExecutorError  # noqa: E402

_REQUEST = httpx.Request("GET", "http://test")


def _response(status: int, json=None) -> httpx.Response:
    # httpx.Response.raise_for_status() requires `.request` to be set even
    # for a 200 - a plain httpx.Response(200) blows up with RuntimeError, so
    # every canned response in this file goes through here instead.
    return httpx.Response(status, json=json, request=_REQUEST)


def _fake_get(responses):
    """A fake `httpx.Client.get` that returns the next canned response from
    `responses` on each call, in order - stands in for the real network call."""
    calls = iter(responses)

    def get(self, path, **kwargs):
        return next(calls)

    return get


# --- Kubernetes ---------------------------------------------------------


def test_get_pod_logs_returns_structured_output(monkeypatch):
    fake_core_v1 = SimpleNamespace(read_namespaced_pod_log=lambda **kwargs: "line1\nline2\n")
    monkeypatch.setattr(tools_spec, "core_v1_api", lambda: fake_core_v1)

    result = tools_spec.get_pod_logs({"namespace": "payments", "pod_name": "checkout-api-xyz"})

    assert result["lines_returned"] == 2
    assert result["logs"] == "line1\nline2\n"


def test_get_pod_logs_missing_pod_name_is_validation_error():
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.get_pod_logs({"namespace": "payments"})
    assert exc_info.value.error_type == "validation_error"


def test_get_pod_logs_not_found_maps_to_not_found(monkeypatch):
    def raise_404(**kwargs):
        raise ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(tools_spec, "core_v1_api", lambda: SimpleNamespace(read_namespaced_pod_log=raise_404))

    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.get_pod_logs({"namespace": "payments", "pod_name": "missing"})
    assert exc_info.value.error_type == "not_found"


def test_list_pods_parses_ready_and_restarts(monkeypatch):
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="checkout-api-1", creation_timestamp=None),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(ready=True, restart_count=2)],
        ),
    )
    fake_core_v1 = SimpleNamespace(list_namespaced_pod=lambda **kwargs: SimpleNamespace(items=[pod]))
    monkeypatch.setattr(tools_spec, "core_v1_api", lambda: fake_core_v1)

    result = tools_spec.list_pods({"namespace": "payments"})

    assert result["pods"] == [
        {"name": "checkout-api-1", "ready": "1/1", "status": "Running", "restarts": 2, "age_seconds": None}
    ]


def test_get_deployment_status_parses_conditions(monkeypatch):
    dep = SimpleNamespace(
        metadata=SimpleNamespace(name="checkout-api", namespace="payments", creation_timestamp=None),
        spec=SimpleNamespace(
            replicas=3,
            template=SimpleNamespace(spec=SimpleNamespace(containers=[SimpleNamespace(image="checkout-api:v1")])),
        ),
        status=SimpleNamespace(
            ready_replicas=2,
            available_replicas=2,
            conditions=[
                SimpleNamespace(
                    type="Available", status="True", reason=None, message=None, last_update_time=None
                )
            ],
        ),
    )
    monkeypatch.setattr(tools_spec, "apps_v1_api", lambda: SimpleNamespace(read_namespaced_deployment=lambda **k: dep))

    result = tools_spec.get_deployment_status({"namespace": "payments", "deployment": "checkout-api"})

    assert result["ready_replicas"] == 2
    assert result["image"] == "checkout-api:v1"
    assert result["conditions"] == [{"type": "Available", "status": "True"}]


def test_restart_deployment_patches_restarted_at_annotation(monkeypatch):
    captured = {}

    def fake_patch(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(tools_spec, "apps_v1_api", lambda: SimpleNamespace(patch_namespaced_deployment=fake_patch))

    result = tools_spec.restart_deployment(
        {"namespace": "payments", "deployment": "checkout-api", "reason": "fixing connection pool exhaustion"}
    )

    assert result["status"] == "restart_initiated"
    annotations = captured["body"]["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in annotations


def test_restart_deployment_missing_reason_is_validation_error(monkeypatch):
    monkeypatch.setattr(
        tools_spec, "apps_v1_api", lambda: SimpleNamespace(patch_namespaced_deployment=lambda **k: None)
    )
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.restart_deployment({"namespace": "payments", "deployment": "checkout-api"})
    assert exc_info.value.error_type == "validation_error"


def test_scale_deployment_patches_replica_count(monkeypatch):
    captured = {}

    def fake_patch_scale(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        tools_spec, "apps_v1_api", lambda: SimpleNamespace(patch_namespaced_deployment_scale=fake_patch_scale)
    )

    result = tools_spec.scale_deployment(
        {"namespace": "payments", "deployment": "checkout-api", "replicas": 5, "reason": "scaling up for load"}
    )

    assert result["status"] == "scale_initiated"
    assert result["replicas"] == 5
    assert captured["body"] == {"spec": {"replicas": 5}}


def test_scale_deployment_rejects_bool_replicas():
    # bool is a subclass of int in Python - `replicas: true` must not
    # silently pass validation as replicas=1.
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.scale_deployment(
            {"namespace": "payments", "deployment": "checkout-api", "replicas": True, "reason": "scaling up for load"}
        )
    assert exc_info.value.error_type == "validation_error"


def test_scale_deployment_rejects_out_of_range_replicas():
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.scale_deployment(
            {"namespace": "payments", "deployment": "checkout-api", "replicas": 25, "reason": "scaling up for load"}
        )
    assert exc_info.value.error_type == "validation_error"


def test_exec_into_pod_runs_command_and_returns_output(monkeypatch):
    captured = {}

    def fake_stream(func, **kwargs):
        captured.update(kwargs)
        return "hello from pod\n"

    monkeypatch.setattr(tools_spec, "stream", fake_stream)
    monkeypatch.setattr(
        tools_spec, "core_v1_api", lambda: SimpleNamespace(connect_get_namespaced_pod_exec=lambda **k: None)
    )

    result = tools_spec.exec_into_pod(
        {
            "namespace": "payments",
            "pod_name": "checkout-api-xyz",
            "command": ["/bin/sh", "-c", "echo hello"],
            "reason": "checking container filesystem state",
        }
    )

    assert result["status"] == "executed"
    assert result["output"] == "hello from pod\n"
    assert result["output_truncated"] is False
    assert captured["command"] == ["/bin/sh", "-c", "echo hello"]
    assert captured["namespace"] == "payments"
    assert captured["name"] == "checkout-api-xyz"
    # k8s.run() must bound this call the same way it bounds every other K8s
    # call - a stuck exec session should not hang the gateway indefinitely.
    assert captured["_request_timeout"] == get_executor_timeout()


def test_exec_into_pod_rejects_non_list_command():
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.exec_into_pod(
            {"namespace": "payments", "pod_name": "x", "command": "echo hi", "reason": "should fail validation here"}
        )
    assert exc_info.value.error_type == "validation_error"


def test_exec_into_pod_rejects_empty_command():
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.exec_into_pod(
            {"namespace": "payments", "pod_name": "x", "command": [], "reason": "should fail validation here"}
        )
    assert exc_info.value.error_type == "validation_error"


def test_exec_into_pod_missing_reason_is_validation_error(monkeypatch):
    monkeypatch.setattr(tools_spec, "stream", lambda func, **kwargs: "")
    monkeypatch.setattr(
        tools_spec, "core_v1_api", lambda: SimpleNamespace(connect_get_namespaced_pod_exec=lambda **k: None)
    )
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.exec_into_pod({"namespace": "payments", "pod_name": "x", "command": ["ls"]})
    assert exc_info.value.error_type == "validation_error"


def test_exec_into_pod_truncates_long_output(monkeypatch):
    long_output = "x" * (tools_spec.EXEC_OUTPUT_MAX_CHARS + 500)
    monkeypatch.setattr(tools_spec, "stream", lambda func, **kwargs: long_output)
    monkeypatch.setattr(
        tools_spec, "core_v1_api", lambda: SimpleNamespace(connect_get_namespaced_pod_exec=lambda **k: None)
    )

    result = tools_spec.exec_into_pod(
        {"namespace": "payments", "pod_name": "x", "command": ["cat", "bigfile"], "reason": "checking large file contents"}
    )

    assert result["output_truncated"] is True
    assert len(result["output"]) == tools_spec.EXEC_OUTPUT_MAX_CHARS


def test_exec_into_pod_not_found_maps_to_not_found(monkeypatch):
    def raise_404(func, **kwargs):
        raise ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(tools_spec, "stream", raise_404)
    monkeypatch.setattr(
        tools_spec, "core_v1_api", lambda: SimpleNamespace(connect_get_namespaced_pod_exec=lambda **k: None)
    )

    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.exec_into_pod(
            {"namespace": "payments", "pod_name": "missing", "command": ["ls"], "reason": "checking pod contents here"}
        )
    assert exc_info.value.error_type == "not_found"


def test_apply_k8s_manifest_calls_server_side_apply(monkeypatch):
    captured = {}

    def fake_server_side_apply(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    fake_resource = SimpleNamespace(namespaced=True, server_side_apply=fake_server_side_apply)
    monkeypatch.setattr(tools_spec.k8s, "resource_for", lambda api_version, kind: fake_resource)

    manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "checkout-api"},
        "spec": {"replicas": 3},
    }
    result = tools_spec.apply_k8s_manifest(
        {"namespace": "payments", "manifest": manifest, "reason": "rolling out new deployment config"}
    )

    assert result["status"] == "applied"
    assert result["kind"] == "Deployment"
    assert result["name"] == "checkout-api"
    assert captured["namespace"] == "payments"
    assert captured["name"] == "checkout-api"
    # manifest.metadata.namespace gets filled in from the top-level
    # `namespace` argument - the one OPA's namespace allowlist actually checked.
    assert captured["body"]["metadata"]["namespace"] == "payments"
    assert captured["field_manager"] == "mcp-control-plane"


def test_apply_k8s_manifest_rejects_namespace_mismatch():
    manifest = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cfg", "namespace": "other-ns"}}
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.apply_k8s_manifest(
            {"namespace": "payments", "manifest": manifest, "reason": "updating configmap values here"}
        )
    assert exc_info.value.error_type == "validation_error"


def test_apply_k8s_manifest_rejects_cluster_scoped_resource(monkeypatch):
    fake_resource = SimpleNamespace(namespaced=False, server_side_apply=lambda **k: None)
    monkeypatch.setattr(tools_spec.k8s, "resource_for", lambda api_version, kind: fake_resource)

    manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "new-namespace"}}
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.apply_k8s_manifest(
            {"namespace": "payments", "manifest": manifest, "reason": "provisioning a new namespace here"}
        )
    assert exc_info.value.error_type == "validation_error"


def test_apply_k8s_manifest_missing_kind_is_validation_error():
    manifest = {"apiVersion": "v1", "metadata": {"name": "cfg"}}
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.apply_k8s_manifest(
            {"namespace": "payments", "manifest": manifest, "reason": "testing missing kind field here"}
        )
    assert exc_info.value.error_type == "validation_error"


def test_apply_k8s_manifest_missing_metadata_name_is_validation_error():
    manifest = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {}}
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.apply_k8s_manifest(
            {"namespace": "payments", "manifest": manifest, "reason": "testing missing name field here"}
        )
    assert exc_info.value.error_type == "validation_error"


def test_apply_k8s_manifest_unknown_resource_type_is_not_found(monkeypatch):
    def fake_resource_for(api_version, kind):
        raise ExecutorError("not_found", f"resource type '{kind}' not found")

    monkeypatch.setattr(tools_spec.k8s, "resource_for", fake_resource_for)

    manifest = {"apiVersion": "bogus/v1", "kind": "Bogus", "metadata": {"name": "x"}}
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.apply_k8s_manifest(
            {"namespace": "payments", "manifest": manifest, "reason": "testing unknown resource kind here"}
        )
    assert exc_info.value.error_type == "not_found"


# --- Terraform Cloud ------------------------------------------------------


def test_query_terraform_plan_summarizes_changes(monkeypatch):
    monkeypatch.setattr(tools_spec, "TFC_TOKEN", "tok")
    monkeypatch.setattr(tools_spec, "TFC_ORG", "acme")
    monkeypatch.setattr(
        httpx.Client,
        "get",
        _fake_get(
            [
                _response(200, {"data": {"id": "ws-123"}}),
                _response(
                    200,
                    {
                        "data": [
                            {
                                "id": "run-abc",
                                "attributes": {"status": "planned", "created-at": "t", "trigger-reason": "api"},
                                "relationships": {"plan": {"data": {"id": "plan-1"}}},
                            }
                        ]
                    },
                ),
                _response(
                    200,
                    {
                        "data": {
                            "attributes": {
                                "resource-additions": 1,
                                "resource-changes": 2,
                                "resource-destructions": 0,
                            }
                        }
                    },
                ),
            ]
        ),
    )

    result = tools_spec.query_terraform_plan({"workspace": "mcp-control-plane-prod"})

    assert result["run_id"] == "run-abc"
    assert result["changes"] == {"add": 1, "change": 2, "destroy": 0}


def test_query_terraform_plan_without_credentials_is_upstream_error(monkeypatch):
    monkeypatch.setattr(tools_spec, "TFC_TOKEN", None)
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.query_terraform_plan({"workspace": "x"})
    assert exc_info.value.error_type == "upstream_error"


def test_query_terraform_plan_unknown_workspace_is_not_found(monkeypatch):
    monkeypatch.setattr(tools_spec, "TFC_TOKEN", "tok")
    monkeypatch.setattr(tools_spec, "TFC_ORG", "acme")
    monkeypatch.setattr(httpx.Client, "get", _fake_get([_response(404)]))

    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.query_terraform_plan({"workspace": "does-not-exist"})
    assert exc_info.value.error_type == "not_found"


# --- Jenkins ----------------------------------------------------------------


def test_get_jenkins_job_status_parses_build(monkeypatch):
    monkeypatch.setattr(tools_spec, "JENKINS_URL", "https://jenkins.internal")
    monkeypatch.setattr(
        httpx.Client,
        "get",
        _fake_get(
            [
                _response(
                    200,
                    {
                        "number": 142,
                        "result": "SUCCESS",
                        "duration": 1000,
                        "timestamp": 1700000000000,
                        "actions": [{"causes": [{"shortDescription": "Started by timer"}]}],
                        "url": "https://jenkins.internal/job/test/smoke-staging/142/",
                        "building": False,
                    },
                )
            ]
        ),
    )

    result = tools_spec.get_jenkins_job_status({"job_name": "test/smoke-staging"})

    assert result["build_number"] == 142
    assert result["status"] == "SUCCESS"
    assert result["triggered_by"] == "Started by timer"


def test_get_jenkins_job_status_timeout_maps_to_executor_timeout(monkeypatch):
    monkeypatch.setattr(tools_spec, "JENKINS_URL", "https://jenkins.internal")

    def get(self, path, **kwargs):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx.Client, "get", get)

    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.get_jenkins_job_status({"job_name": "test/smoke-staging"})
    assert exc_info.value.error_type == "executor_timeout"


def _fake_post(responses):
    """Same as _fake_get, for the one write executor (trigger_jenkins_job)
    that POSTs instead of GETs."""
    calls = iter(responses)

    def post(self, path, **kwargs):
        return next(calls)

    return post


def test_trigger_jenkins_job_queues_build(monkeypatch):
    monkeypatch.setattr(tools_spec, "JENKINS_URL", "https://jenkins.internal")
    # No crumb issuer configured on this fake Jenkins - _jenkins_crumb should
    # treat the 404 as "no crumb needed" rather than failing the call.
    monkeypatch.setattr(httpx.Client, "get", lambda self, path, **kwargs: _response(404))
    queued = httpx.Response(
        201, request=_REQUEST, headers={"Location": "https://jenkins.internal/queue/item/1234/"}
    )
    monkeypatch.setattr(httpx.Client, "post", _fake_post([queued]))

    result = tools_spec.trigger_jenkins_job(
        {
            "job_name": "test/smoke-staging",
            "reason": "verifying deploy pipeline before release",
            "parameters": {"env": "staging"},
        }
    )

    assert result["status"] == "triggered"
    assert result["build_number"] is None
    assert result["queue_item_url"] == "https://jenkins.internal/queue/item/1234/"


def test_trigger_jenkins_job_short_reason_is_validation_error():
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.trigger_jenkins_job({"job_name": "test/smoke-staging", "reason": "short"})
    assert exc_info.value.error_type == "validation_error"


def test_trigger_jenkins_job_unknown_job_is_not_found(monkeypatch):
    monkeypatch.setattr(tools_spec, "JENKINS_URL", "https://jenkins.internal")
    monkeypatch.setattr(httpx.Client, "get", lambda self, path, **kwargs: _response(404))
    monkeypatch.setattr(httpx.Client, "post", _fake_post([_response(404)]))

    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.trigger_jenkins_job({"job_name": "deploy/does-not-exist", "reason": "testing not-found mapping"})
    assert exc_info.value.error_type == "not_found"


# --- Prometheus -------------------------------------------------------------


def test_read_prometheus_metrics_returns_vector(monkeypatch):
    monkeypatch.setattr(
        httpx.Client,
        "get",
        _fake_get(
            [
                _response(
                    200,
                    {
                        "status": "success",
                        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]},
                    },
                )
            ]
        ),
    )

    result = tools_spec.read_prometheus_metrics({"query": "up"})

    assert result["result_type"] == "vector"
    assert len(result["result"]) == 1


def test_read_prometheus_metrics_query_error_is_upstream_error(monkeypatch):
    monkeypatch.setattr(
        httpx.Client, "get", _fake_get([_response(200, {"status": "error", "error": "bad query"})])
    )

    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.read_prometheus_metrics({"query": "??"})
    assert exc_info.value.error_type == "upstream_error"


# --- Ticketing (PagerDuty / Jira) --------------------------------------


def test_read_ticket_pagerduty(monkeypatch):
    monkeypatch.setattr(tools_spec, "PAGERDUTY_API_TOKEN", "tok")
    monkeypatch.setattr(
        httpx.Client,
        "get",
        _fake_get(
            [
                _response(
                    200,
                    {
                        "incident": {
                            "id": "Q3WE8FXPZ2R",
                            "status": "acknowledged",
                            "title": "checkout-api error rate spike",
                            "urgency": "high",
                            "created_at": "t",
                            "assignments": [],
                            "last_status_change_at": "t2",
                        }
                    },
                )
            ]
        ),
    )

    result = tools_spec.read_ticket({"system": "pagerduty", "ticket_id": "Q3WE8FXPZ2R"})

    assert result["status"] == "acknowledged"
    assert result["severity"] == "high"


def test_read_ticket_jira(monkeypatch):
    monkeypatch.setattr(tools_spec, "JIRA_URL", "https://jira.internal")
    monkeypatch.setattr(tools_spec, "JIRA_USER", "bot@example.com")
    monkeypatch.setattr(tools_spec, "JIRA_API_TOKEN", "tok")
    monkeypatch.setattr(
        httpx.Client,
        "get",
        _fake_get(
            [
                _response(
                    200,
                    {
                        "fields": {
                            "status": {"name": "In Progress"},
                            "summary": "DB migration failing",
                            "priority": {"name": "High"},
                            "created": "t",
                            "assignee": {"emailAddress": "alice@example.com"},
                            "updated": "t2",
                            "comment": {"total": 3},
                        }
                    },
                )
            ]
        ),
    )

    result = tools_spec.read_ticket({"system": "jira", "ticket_id": "INFRA-1234"})

    assert result["status"] == "In Progress"
    assert result["notes_count"] == 3


def test_read_ticket_invalid_system_is_validation_error():
    with pytest.raises(ExecutorError) as exc_info:
        tools_spec.read_ticket({"system": "servicenow", "ticket_id": "x"})
    assert exc_info.value.error_type == "validation_error"
