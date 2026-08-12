"""Lazy, cached Kubernetes API clients.

Loads kube config once per process (in-cluster config if running in a pod,
else `KUBECONFIG`/`~/.kube/config` for local dev against Kind/Minikube - see
README "Kubernetes tools" for local setup) and reuses the same CoreV1Api/
AppsV1Api/DynamicClient instances across calls instead of reloading config
per request.
"""
import logging
from functools import lru_cache

from kubernetes import client, config, dynamic
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from kubernetes.dynamic.exceptions import ResourceNotFoundError, ResourceNotUniqueError
from urllib3.exceptions import MaxRetryError
from urllib3.exceptions import TimeoutError as Urllib3TimeoutError

from .config import get_executor_timeout
from .errors import ExecutorError

logger = logging.getLogger(__name__)


# @lru_cache makes this a run-once: the first caller loads config, every
# later call is a no-op. lru_cache does not cache raised exceptions, so a
# pod that starts before the kubeconfig is mounted keeps retrying on every
# tool call instead of latching a permanent failure from the first attempt.
@lru_cache(maxsize=1)
def _load_config() -> None:
    try:
        # In-cluster (ServiceAccount token) first, since that's how this
        # gateway runs in prod/staging per docs/architecture.md.
        config.load_incluster_config()
    except ConfigException:
        # Not running in a pod -> local dev, fall back to kubeconfig
        # (KUBECONFIG env var or ~/.kube/config pointed at Kind/Minikube).
        try:
            config.load_kube_config()
        except ConfigException as exc:
            raise ExecutorError(
                "upstream_error", f"no usable kubeconfig found: {exc}"
            ) from exc


# Cached singletons: building a new ApiClient per call would re-parse the
# kubeconfig/cert bundle on every tool invocation for no benefit.
@lru_cache(maxsize=1)
def core_v1_api() -> client.CoreV1Api:
    _load_config()
    return client.CoreV1Api()


@lru_cache(maxsize=1)
def apps_v1_api() -> client.AppsV1Api:
    _load_config()
    return client.AppsV1Api()


@lru_cache(maxsize=1)
def dynamic_client() -> dynamic.DynamicClient:
    """Generic, kind-agnostic client - backs `apply_k8s_manifest`, which
    (unlike every other K8s tool here) has to work against whatever
    apiVersion/kind is in the manifest it's given, not one hardcoded
    CoreV1Api/AppsV1Api method."""
    _load_config()
    return dynamic.DynamicClient(client.ApiClient())


def resource_for(api_version: str, kind: str):
    """Look up the dynamic `Resource` for one apiVersion/kind pair, for
    `apply_k8s_manifest`. The DynamicClient discovers and caches every kind
    the cluster supports on first use, so only the very first call in this
    process's lifetime does a live HTTP request - every call after that is
    a local lookup against the cached discovery document.
    ponytail: that first-ever discovery request isn't bounded by
    get_executor_timeout() the way run() below bounds everything else (it
    can't accept `_request_timeout` - see the caller) - a real but narrow
    gap; pre-warming discovery at startup would close it if a hung first
    call ever became a problem in practice.
    """
    try:
        return dynamic_client().resources.get(api_version=api_version, kind=kind)
    except ResourceNotFoundError as exc:
        raise ExecutorError("not_found", f"resource type '{kind}' ({api_version}) not found on this cluster") from exc
    except ResourceNotUniqueError as exc:
        raise ExecutorError("validation_error", f"'{kind}' ({api_version}) is ambiguous on this cluster: {exc}") from exc


def run(fn, resource: str, **kwargs):
    """Call a CoreV1Api/AppsV1Api method (or a DynamicClient resource action
    like `server_side_apply`, or `kubernetes.stream.stream` for pod exec -
    anything that accepts `_request_timeout` and raises the same
    ApiException/urllib3 error types) with the shared executor timeout,
    mapping the client's exceptions to ExecutorError so every K8s tool
    reports failures the same way instead of each executor re-implementing
    this try/except. `resource` is a human-readable description of what was
    being fetched, used in the error message (e.g. "pod 'x' in namespace 'y'")."""
    try:
        return fn(_request_timeout=get_executor_timeout(), **kwargs)
    except ApiException as exc:
        # Distinguish "doesn't exist" and "not authorized" from a generic
        # upstream failure so main.py can map each to the right MCP
        # error_type/status per docs/tool-spec.md.
        if exc.status == 404:
            raise ExecutorError("not_found", f"{resource} not found") from exc
        if exc.status in (401, 403):
            raise ExecutorError("permission_denied", f"not authorized to access {resource}") from exc
        raise ExecutorError("upstream_error", f"Kubernetes API error ({exc.status}): {exc.reason}") from exc
    except MaxRetryError as exc:
        # Connection-level failures (timeout, cluster unreachable) never
        # reach the API server, so they surface as urllib3 errors instead
        # of ApiException. TimeoutError covers both connect and read
        # timeouts; anything else is a genuinely unreachable cluster.
        if isinstance(exc.reason, Urllib3TimeoutError):
            raise ExecutorError(
                "executor_timeout", f"Kubernetes API call for {resource} timed out"
            ) from exc
        raise ExecutorError("upstream_error", f"Kubernetes API unreachable: {exc}") from exc
