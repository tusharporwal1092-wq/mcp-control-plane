"""Executor error type.

Raised by tool executors so app/main.py can map a failure to the right HTTP
status and the `error_type` field documented in docs/tool-spec.md's Error
Response Format (not_found, executor_timeout, upstream_error,
validation_error). Anything an executor doesn't raise itself (e.g. an
unexpected exception from a client library) falls back to "upstream_error"
in main.py rather than needing every executor to catch every exception type.
"""


class ExecutorError(Exception):
    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        super().__init__(message)
