"""Behavior tests for request observability middleware."""

from __future__ import annotations

import asyncio
import json
import re
import unittest
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from starlette.datastructures import Headers

from server.observability import install_request_observability


@dataclass(frozen=True)
class _Response:
    status_code: int
    headers: Headers
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


async def _asgi_get(
    app: FastAPI,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> _Response:
    request_messages = [
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    response_messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if request_messages:
            return request_messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        response_messages.append(message)

    raw_headers = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": raw_headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "state": {},
        },
        receive,
        send,
    )
    start = next(message for message in response_messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in response_messages
        if message["type"] == "http.response.body"
    )
    return _Response(start["status"], Headers(raw=start["headers"]), body)


class RequestObservabilityTests(unittest.TestCase):
    @staticmethod
    def _app(*, slow_request_ms: float = 1000.0) -> FastAPI:
        app = FastAPI()
        install_request_observability(app, slow_request_ms=slow_request_ms)

        @app.get("/ok")
        async def ok() -> dict[str, bool]:
            return {"ok": True}

        @app.get("/items/{item_id}")
        async def item(item_id: str) -> dict[str, str]:
            return {"item_id": item_id}

        @app.get("/files/{filename}")
        async def file(filename: str) -> dict[str, str]:
            return {"filename": filename}

        @app.get("/handled-error")
        async def handled_error() -> None:
            raise HTTPException(status_code=418, detail="expected")

        @app.get("/api/health")
        async def health() -> dict[str, bool]:
            return {"ok": True}

        return app

    def test_generates_uuid_and_parseable_non_negative_server_timing(self) -> None:
        response = asyncio.run(_asgi_get(self._app(), "/ok"))

        request_id = uuid.UUID(response.headers["X-Request-ID"])
        self.assertEqual(request_id.version, 4)
        timing_match = re.fullmatch(
            r"app;dur=(\d+(?:\.\d+)?)",
            response.headers["Server-Timing"],
        )
        self.assertIsNotNone(timing_match)
        self.assertGreaterEqual(float(timing_match.group(1)), 0.0)

    def test_accepts_only_safe_incoming_request_id(self) -> None:
        app = self._app()
        safe_id = "client.Request_01-abc"

        accepted = asyncio.run(_asgi_get(app, "/ok", headers={"X-Request-ID": safe_id}))
        self.assertEqual(accepted.headers["X-Request-ID"], safe_id)

        for unsafe_id in ("contains space", "contains/slash", "x" * 129, ""):
            with self.subTest(unsafe_id=unsafe_id):
                response = asyncio.run(
                    _asgi_get(app, "/ok", headers={"X-Request-ID": unsafe_id})
                )
                self.assertNotEqual(response.headers["X-Request-ID"], unsafe_id)
                self.assertEqual(uuid.UUID(response.headers["X-Request-ID"]).version, 4)

    def test_handled_errors_receive_observability_headers(self) -> None:
        response = asyncio.run(_asgi_get(self._app(), "/handled-error"))

        self.assertEqual(response.status_code, 418)
        self.assertEqual(uuid.UUID(response.headers["X-Request-ID"]).version, 4)
        self.assertRegex(response.headers["Server-Timing"], r"^app;dur=\d+(?:\.\d+)?$")

    def test_slow_log_uses_route_template_without_ids_or_filenames(self) -> None:
        app = self._app(slow_request_ms=0)

        with self.assertLogs("server.observability", level="WARNING") as captured:
            item_response = asyncio.run(_asgi_get(app, "/items/private-item-8675309"))
            file_response = asyncio.run(
                _asgi_get(app, "/files/customer-recording-2026.mp4")
            )

        self.assertEqual(item_response.status_code, 200)
        self.assertEqual(file_response.status_code, 200)
        messages = "\n".join(captured.output)
        self.assertIn("route=/items/{item_id}", messages)
        self.assertIn("route=/files/{filename}", messages)
        self.assertNotIn("private-item-8675309", messages)
        self.assertNotIn("customer-recording-2026.mp4", messages)

    def test_unmatched_request_log_never_contains_raw_path(self) -> None:
        raw_path = "/private/customer-recording-2026.mp4"

        with self.assertLogs("server.observability", level="WARNING") as captured:
            response = asyncio.run(_asgi_get(self._app(slow_request_ms=0), raw_path))

        self.assertEqual(response.status_code, 404)
        messages = "\n".join(captured.output)
        self.assertIn("route=unmatched", messages)
        self.assertNotIn(raw_path, messages)
        self.assertNotIn("customer-recording-2026.mp4", messages)

    def test_health_remains_dependency_free_and_observable(self) -> None:
        response = asyncio.run(_asgi_get(self._app(), "/api/health"))

        self.assertEqual(response.json(), {"ok": True})
        self.assertIn("X-Request-ID", response.headers)
        self.assertIn("Server-Timing", response.headers)


if __name__ == "__main__":
    unittest.main()
