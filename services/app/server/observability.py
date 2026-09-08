"""Low-cardinality request timing and correlation IDs."""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass

from fastapi import FastAPI, Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

_OBSERVATION_STATE_KEY = "_request_observation"
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


@dataclass
class _RequestObservation:
    request_id: str
    started: float
    slow_request_ms: float
    response_start_ms: float | None = None
    logged: bool = False


def _request_id(headers: Headers) -> str:
    incoming = headers.get("X-Request-ID", "")
    if _SAFE_REQUEST_ID.fullmatch(incoming):
        return incoming
    return str(uuid.uuid4())


def _elapsed_ms(observation: _RequestObservation) -> float:
    return max(0.0, (time.perf_counter() - observation.started) * 1000.0)


def _warn_if_slow(
    scope: Scope,
    observation: _RequestObservation,
    status_code: int,
    duration_ms: float,
) -> None:
    if observation.logged or duration_ms <= observation.slow_request_ms:
        return
    route = scope.get("route")
    route_path = getattr(route, "path", None) or "unmatched"
    response_start_ms = observation.response_start_ms
    if response_start_ms is None:
        response_start_ms = duration_ms
    log.warning(
        "request_id=%s method=%s route=%s status=%d "
        "response_start_ms=%.3f duration_ms=%.3f",
        observation.request_id,
        scope["method"],
        route_path,
        status_code,
        response_start_ms,
        duration_ms,
    )
    observation.logged = True


class _RequestObservabilityMiddleware:
    def __init__(self, app: ASGIApp, *, slow_request_ms: float) -> None:
        self.app = app
        self.slow_request_ms = slow_request_ms

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        observation = _RequestObservation(
            request_id=_request_id(Headers(scope=scope)),
            started=time.perf_counter(),
            slow_request_ms=self.slow_request_ms,
        )
        state = scope.setdefault("state", {})
        state["request_id"] = observation.request_id
        state[_OBSERVATION_STATE_KEY] = observation
        status_code = 500

        async def send_observed(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                observation.response_start_ms = _elapsed_ms(observation)
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = observation.request_id
                headers["Server-Timing"] = (
                    f"app;dur={observation.response_start_ms:.3f}"
                )

            await send(message)

            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                _warn_if_slow(
                    scope,
                    observation,
                    status_code,
                    _elapsed_ms(observation),
                )

        await self.app(scope, receive, send_observed)


def install_request_observability(
    app: FastAPI,
    slow_request_ms: float = 1000.0,
) -> None:
    """Install timing where the header covers work up to response start.

    Slow warnings measure through the final ASGI response body message, including
    streaming generation. Starlette still re-raises unhandled exceptions after
    the registered 500 handler returns, preserving server error logging.
    """

    app.add_middleware(
        _RequestObservabilityMiddleware,
        slow_request_ms=slow_request_ms,
    )

    async def unexpected_error(request: Request, exc: Exception) -> PlainTextResponse:
        observation = request.scope["state"].get(_OBSERVATION_STATE_KEY)
        if observation is None:
            observation = _RequestObservation(
                request_id=_request_id(request.headers),
                started=time.perf_counter(),
                slow_request_ms=slow_request_ms,
            )
        duration_ms = _elapsed_ms(observation)
        observation.response_start_ms = duration_ms
        _warn_if_slow(request.scope, observation, 500, duration_ms)
        return PlainTextResponse(
            "Internal Server Error",
            status_code=500,
            headers={
                "X-Request-ID": observation.request_id,
                "Server-Timing": f"app;dur={duration_ms:.3f}",
            },
        )

    app.add_exception_handler(Exception, unexpected_error)
