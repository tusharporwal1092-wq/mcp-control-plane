"""HTTP request logging middleware.

Logs one line per request (method, path, status code, latency), independent
of how far the request got - a 401 from auth or a 429 from the rate limiter
is logged just as well as a 200 from a route handler. That's why main.py
registers this as the outermost middleware (see the comment there): it must
wrap every other middleware to see the final response in all cases.
"""
import logging
import time

from fastapi import Request

logger = logging.getLogger("http")


async def log_requests(request: Request, call_next):
    """ASGI middleware entrypoint: time the request and log method/path/status/latency.

    Registered in app/main.py via `app.add_middleware(..., dispatch=log_requests)`.
    """
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s -> %s (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response
