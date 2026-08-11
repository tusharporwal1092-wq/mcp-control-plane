"""Lazy, cached Kubernetes API clients.

Loads kube config once per process (in-cluster config if running in a pod,
else `KUBECONFIG`/`~/.kube/config` for local dev against Kind/Minikube - see
README "Kubernetes tools" for local setup) and reuses the same CoreV1Api/
AppsV1Api instances across calls instead of reloading config per request.
"""
import logging
from functools import lru_cache

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
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


def run(fn, resource: str, **kwargs):
    """Call a CoreV1Api/AppsV1Api method with the shared executor timeout,
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
