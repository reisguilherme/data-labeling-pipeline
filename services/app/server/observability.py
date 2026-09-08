"""Low-cardinality request timing and correlation IDs."""

from __future__ import annotations

import logging
import re
import time
import uuid

from fastapi import FastAPI, Request

log = logging.getLogger(__name__)

_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


def _request_id(request: Request) -> str:
    incoming = request.headers.get("X-Request-ID", "")
    if _SAFE_REQUEST_ID.fullmatch(incoming):
        return incoming
    return str(uuid.uuid4())


def install_request_observability(
    app: FastAPI,
    slow_request_ms: float = 1000.0,
) -> None:
    """Install normalized request logging, timing, and correlation headers."""

    @app.middleware("http")
    async def observe_request(request: Request, call_next):
        request_id = _request_id(request)
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = max(0.0, (time.perf_counter() - started) * 1000.0)

        response.headers["X-Request-ID"] = request_id
        response.headers["Server-Timing"] = f"app;dur={duration_ms:.3f}"

        if duration_ms > slow_request_ms:
            route = request.scope.get("route")
            route_path = getattr(route, "path", None) or "unmatched"
            log.warning(
                "request_id=%s method=%s route=%s status=%d duration_ms=%.3f",
                request_id,
                request.method,
                route_path,
                response.status_code,
                duration_ms,
            )

        return response
