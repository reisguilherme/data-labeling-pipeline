"""Tests for the read-only object-library benchmark."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[4] / "scripts" / "benchmark-library.py"


def _load_benchmark():
    spec = importlib.util.spec_from_file_location("benchmark_library", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import benchmark: {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _library_server(status: int = 200):
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            requests.append((self.command, self.path))
            body = b'{"videos":[{"name":"private-video.mp4"}]}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class BenchmarkLibraryPercentileTests(unittest.TestCase):
    def test_percentile_uses_deterministic_nearest_rank(self) -> None:
        self.assertTrue(SCRIPT_PATH.is_file(), f"missing benchmark: {SCRIPT_PATH}")
        module = _load_benchmark()

        samples = [10, 20, 30, 40, 50]
        self.assertEqual(module.percentile(samples, 50), 30)
        self.assertEqual(module.percentile(samples, 95), 50)


class BenchmarkLibraryCommandTests(unittest.TestCase):
    def test_warmup_and_samples_use_get_and_print_only_aggregate_metrics(self) -> None:
        module = _load_benchmark()
        self.assertTrue(callable(getattr(module, "main", None)), "missing main(argv)")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with _library_server() as (base_url, requests):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = module.main(
                    [
                        "--base-url",
                        base_url,
                        "--object-id",
                        "boom",
                        "--samples",
                        "2",
                        "--timeout",
                        "1",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            requests,
            [("GET", "/api/objects/boom/videos")] * 3,
        )
        fields = [line.split(":", 1)[0] for line in stdout.getvalue().splitlines()]
        self.assertEqual(
            fields, ["sample_count", "p50_ms", "p95_ms", "min_ms", "max_ms"]
        )
        self.assertNotIn("private-video.mp4", stdout.getvalue())
        self.assertNotIn("boom", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_non_2xx_response_fails_without_echoing_request_details(self) -> None:
        module = _load_benchmark()
        self.assertTrue(callable(getattr(module, "main", None)), "missing main(argv)")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with _library_server(status=503) as (base_url, requests):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = module.main(
                    [
                        "--base-url",
                        base_url,
                        "--object-id",
                        "private-object",
                        "--samples",
                        "1",
                        "--timeout",
                        "1",
                    ]
                )

        self.assertNotEqual(result, 0)
        self.assertEqual(requests, [("GET", "/api/objects/private-object/videos")])
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("private-object", stderr.getvalue())
        self.assertNotIn(base_url, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
