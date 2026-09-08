"""Behavior tests for request observability middleware."""

from __future__ import annotations

import asyncio
import json
import re
import unittest
import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from starlette.datastructures import Headers
from starlette.responses import StreamingResponse

from server.observability import install_request_observability


@dataclass(frozen=True)
class _Response:
    status_code: int
    headers: Headers
    body: bytes
    exception: Exception | None = None

    def json(self) -> Any:
        return json.loads(self.body)


async def _asgi_request(
    app: FastAPI,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    raise_server_exceptions: bool = True,
    response_body_send_delay_ms: float = 0,
) -> _Response:
    request_messages = [
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    response_messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if request_messages:
            return request_messages.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and response_body_send_delay_ms:
            await asyncio.sleep(response_body_send_delay_ms / 1000.0)
        response_messages.append(message)

    raw_headers = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    caught: Exception | None = None
    try:
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": method,
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
    except Exception as exc:  # ServerErrorMiddleware sends 500, then re-raises.
        if raise_server_exceptions:
            raise
        caught = exc
    start = next(message for message in response_messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in response_messages
        if message["type"] == "http.response.body"
    )
    return _Response(start["status"], Headers(raw=start["headers"]), body, caught)


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

        @app.get("/runtime-error/{item_id}")
        async def runtime_error(item_id: str) -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError("expected server failure")

        @app.get("/stream/{filename}")
        async def stream(filename: str) -> StreamingResponse:
            async def body():
                yield b"first"
                await asyncio.sleep(0.02)
                yield b"second"

            return StreamingResponse(body())

        @app.get("/broken-stream/{filename}")
        async def broken_stream(filename: str) -> StreamingResponse:
            async def body():
                yield b"first"
                await asyncio.sleep(0.01)
                raise RuntimeError("expected streaming failure")

            return StreamingResponse(body())

        @app.get("/api/health")
        async def health() -> dict[str, bool]:
            return {"ok": True}

        return app

    def test_generates_uuid_and_parseable_non_negative_server_timing(self) -> None:
        response = asyncio.run(_asgi_request(self._app(), "/ok"))

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

        accepted = asyncio.run(
            _asgi_request(app, "/ok", headers={"X-Request-ID": safe_id})
        )
        self.assertEqual(accepted.headers["X-Request-ID"], safe_id)

        for unsafe_id in ("contains space", "contains/slash", "x" * 129, ""):
            with self.subTest(unsafe_id=unsafe_id):
                response = asyncio.run(
                    _asgi_request(app, "/ok", headers={"X-Request-ID": unsafe_id})
                )
                self.assertNotEqual(response.headers["X-Request-ID"], unsafe_id)
                self.assertEqual(uuid.UUID(response.headers["X-Request-ID"]).version, 4)

    def test_handled_errors_receive_observability_headers(self) -> None:
        response = asyncio.run(_asgi_request(self._app(), "/handled-error"))

        self.assertEqual(response.status_code, 418)
        self.assertEqual(uuid.UUID(response.headers["X-Request-ID"]).version, 4)
        self.assertRegex(response.headers["Server-Timing"], r"^app;dur=\d+(?:\.\d+)?$")

    def test_slow_log_uses_route_template_without_ids_or_filenames(self) -> None:
        app = self._app(slow_request_ms=0)

        with self.assertLogs("server.observability", level="WARNING") as captured:
            item_response = asyncio.run(
                _asgi_request(app, "/items/private-item-8675309")
            )
            file_response = asyncio.run(
                _asgi_request(app, "/files/customer-recording-2026.mp4")
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
            response = asyncio.run(
                _asgi_request(self._app(slow_request_ms=0), raw_path)
            )

        self.assertEqual(response.status_code, 404)
        messages = "\n".join(captured.output)
        self.assertIn("route=unmatched", messages)
        self.assertNotIn(raw_path, messages)
        self.assertNotIn("customer-recording-2026.mp4", messages)

    def test_health_remains_dependency_free_and_observable(self) -> None:
        response = asyncio.run(_asgi_request(self._app(), "/api/health"))

        self.assertEqual(response.json(), {"ok": True})
        self.assertIn("X-Request-ID", response.headers)
        self.assertIn("Server-Timing", response.headers)

    def test_slow_runtime_error_gets_headers_and_propagates_without_path_leak(self) -> None:
        raw_id = "private-object-8675309"
        app = self._app(slow_request_ms=0)

        with self.assertLogs("server.observability", level="WARNING") as captured:
            response = asyncio.run(
                _asgi_request(
                    app,
                    f"/runtime-error/{raw_id}",
                    raise_server_exceptions=False,
                )
            )

        self.assertEqual(response.status_code, 500)
        self.assertIsInstance(response.exception, RuntimeError)
        self.assertIn("X-Request-ID", response.headers)
        self.assertRegex(response.headers["Server-Timing"], r"^app;dur=\d+(?:\.\d+)?$")
        messages = "\n".join(captured.output)
        self.assertIn("route=/runtime-error/{item_id}", messages)
        self.assertNotIn(raw_id, messages)

    def test_streaming_header_measures_response_start_and_log_measures_full_body(self) -> None:
        filename = "private-recording-2026.mp4"
        app = self._app(slow_request_ms=0)

        with self.assertLogs("server.observability", level="WARNING") as captured:
            response = asyncio.run(_asgi_request(app, f"/stream/{filename}"))

        header_ms = float(response.headers["Server-Timing"].removeprefix("app;dur="))
        messages = "\n".join(captured.output)
        start_match = re.search(r"response_start_ms=(\d+(?:\.\d+)?)", messages)
        total_match = re.search(r"duration_ms=(\d+(?:\.\d+)?)", messages)
        self.assertEqual(response.body, b"firstsecond")
        self.assertIsNotNone(start_match)
        self.assertIsNotNone(total_match)
        self.assertAlmostEqual(header_ms, float(start_match.group(1)), places=3)
        self.assertGreater(float(total_match.group(1)), header_ms + 10.0)
        self.assertIn("route=/stream/{filename}", messages)
        self.assertNotIn(filename, messages)

    def test_streaming_error_preserves_started_status_and_timing_once(self) -> None:
        filename = "private-recording-2026.mp4"
        app = self._app(slow_request_ms=1000)

        with self.assertLogs("server.observability", level="WARNING") as captured:
            response = asyncio.run(
                _asgi_request(
                    app,
                    f"/broken-stream/{filename}",
                    raise_server_exceptions=False,
                )
            )

        header_ms = float(response.headers["Server-Timing"].removeprefix("app;dur="))
        message = "\n".join(captured.output)
        start_match = re.search(r"response_start_ms=(\d+(?:\.\d+)?)", message)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"first")
        self.assertIsInstance(response.exception, RuntimeError)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("status=200", message)
        self.assertIn("stream_error=true", message)
        self.assertIn("exception=RuntimeError", message)
        self.assertNotIn("status=500", message)
        self.assertIsNotNone(start_match)
        self.assertAlmostEqual(header_ms, float(start_match.group(1)), places=3)
        self.assertIn("route=/broken-stream/{filename}", message)
        self.assertNotIn(filename, message)

    def test_server_error_warning_includes_sending_its_body_once(self) -> None:
        app = self._app(slow_request_ms=15)

        with self.assertLogs("server.observability", level="WARNING") as captured:
            response = asyncio.run(
                _asgi_request(
                    app,
                    "/runtime-error/private-object-8675309",
                    raise_server_exceptions=False,
                    response_body_send_delay_ms=20,
                )
            )

        header_ms = float(response.headers["Server-Timing"].removeprefix("app;dur="))
        message = "\n".join(captured.output)
        total_match = re.search(r"duration_ms=(\d+(?:\.\d+)?)", message)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.body, b"Internal Server Error")
        self.assertIsInstance(response.exception, RuntimeError)
        self.assertEqual(len(captured.output), 1)
        self.assertIsNotNone(total_match)
        self.assertGreater(float(total_match.group(1)), header_ms + 15.0)
        self.assertIn("status=500", message)
        self.assertNotIn("stream_error=true", message)

    def test_observability_wraps_cors_preflight_and_exposes_headers(self) -> None:
        from server import main

        origin_headers = {
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        }
        preflight = asyncio.run(
            _asgi_request(
                main.app,
                "/api/health",
                method="OPTIONS",
                headers=origin_headers,
            )
        )
        actual = asyncio.run(
            _asgi_request(
                main.app,
                "/api/health",
                headers={"Origin": "http://localhost:5173"},
            )
        )

        self.assertEqual(preflight.status_code, 200)
        self.assertIn("X-Request-ID", preflight.headers)
        self.assertIn("Server-Timing", preflight.headers)
        self.assertEqual(
            actual.headers["Access-Control-Expose-Headers"],
            "X-Request-ID, Server-Timing",
        )

    def test_real_application_health_has_headers_without_using_dependencies(self) -> None:
        from server import main

        with (
            patch.object(main.ffmpeg, "resolve") as resolve_ffmpeg,
            patch.object(main.workspace, "load") as load_workspace,
        ):
            response = asyncio.run(_asgi_request(main.app, "/api/health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        self.assertIn("X-Request-ID", response.headers)
        self.assertIn("Server-Timing", response.headers)
        resolve_ffmpeg.assert_not_called()
        load_workspace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
