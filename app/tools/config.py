"""Executor timeout configuration.

Every downstream call (K8s, Terraform Cloud, Jenkins, Prometheus, PagerDuty/
Jira) gets this timeout, so one slow dependency can't hang a request past
the bound docs/tool-spec.md's Timeout Handling section commits to: 10s
default, 30s max. `EXECUTOR_TIMEOUT_SECONDS` lets ops tune it without a code
change, clamped so it can never exceed the documented ceiling.
"""
import os

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0


def get_executor_timeout() -> float:
    raw = os.getenv("EXECUTOR_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(value, MAX_TIMEOUT_SECONDS))
