"""Tests for the read-only object-library benchmark."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import threading
import time
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
def _library_server(
    status: int = 200,
    *,
    delays: tuple[float, ...] = (),
    body: bytes = b'{"videos":[{"name":"private-video.mp4"}]}',
):
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            requests.append((self.command, self.path))
            request_index = len(requests) - 1
            if request_index < len(delays):
                time.sleep(delays[request_index])
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

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

    def test_reserved_object_id_characters_are_percent_encoded(self) -> None:
        module = _load_benchmark()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with _library_server() as (base_url, requests):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = module.main(
                    [
                        "--base-url",
                        base_url,
                        "--object-id",
                        "private/name ?#%",
                        "--samples",
                        "1",
                        "--timeout",
                        "1",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            requests,
            [("GET", "/api/objects/private%2Fname%20%3F%23%25/videos")] * 2,
        )
        combined_output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("private/name", combined_output)

    def test_slow_warmup_is_excluded_from_reported_samples(self) -> None:
        module = _load_benchmark()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with _library_server(delays=(0.4, 0.0)) as (base_url, requests):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = module.main(
                    [
                        "--base-url",
                        base_url,
                        "--object-id",
                        "boom",
                        "--samples",
                        "1",
                        "--timeout",
                        "1",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(len(requests), 2)
        metrics = dict(
            line.split(": ", 1) for line in stdout.getvalue().splitlines()
        )
        self.assertEqual(metrics["sample_count"], "1")
        self.assertLess(float(metrics["max_ms"]), 200)
        self.assertEqual(stderr.getvalue(), "")

    def test_expired_timeout_fails_without_exposing_response_details(self) -> None:
        module = _load_benchmark()
        stdout = io.StringIO()
        stderr = io.StringIO()
        secret_body = b'{"videos":[{"name":"timeout-secret-video.mp4"}]}'

        with _library_server(delays=(0.2,), body=secret_body) as (base_url, requests):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = module.main(
                    [
                        "--base-url",
                        base_url,
                        "--object-id",
                        "timeout-secret-object",
                        "--samples",
                        "1",
                        "--timeout",
                        "0.01",
                    ]
                )

        self.assertNotEqual(result, 0)
        self.assertEqual(
            requests, [("GET", "/api/objects/timeout-secret-object/videos")]
        )
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("timeout-secret", stderr.getvalue())
        self.assertNotIn(base_url, stderr.getvalue())

    def test_non_finite_timeout_is_rejected_before_request(self) -> None:
        module = _load_benchmark()

        for timeout in ("nan", "inf", "-inf"):
            with self.subTest(timeout=timeout):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with _library_server() as (base_url, requests):
                    with contextlib.redirect_stdout(
                        stdout
                    ), contextlib.redirect_stderr(stderr):
                        try:
                            result = module.main(
                                [
                                    "--base-url",
                                    base_url,
                                    "--object-id",
                                    "finite-timeout-object",
                                    "--samples",
                                    "1",
                                    f"--timeout={timeout}",
                                ]
                            )
                        except BaseException as exc:
                            self.fail(
                                f"main leaked {type(exc).__name__} for non-finite timeout"
                            )

                self.assertNotEqual(result, 0)
                self.assertEqual(requests, [])
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn(base_url, stderr.getvalue())

    def test_invalid_base_urls_fail_without_echoing_sensitive_values(self) -> None:
        module = _load_benchmark()
        invalid_urls = (
            "ftp://127.0.0.1/library",
            "http:///missing-host",
            "http://audit-user:audit-password@127.0.0.1:8000",
            "http://127.0.0.1:8000?token=audit-query-secret",
            "http://127.0.0.1:8000#audit-fragment-secret",
            "http://[audit-invalid-ipv6",
        )

        for base_url in invalid_urls:
            with self.subTest(base_url=base_url):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                    stderr
                ):
                    try:
                        result = module.main(
                            [
                                "--base-url",
                                base_url,
                                "--object-id",
                                "audit-object-secret",
                                "--samples",
                                "1",
                                "--timeout",
                                "0.01",
                            ]
                        )
                    except BaseException as exc:
                        self.fail(
                            f"main leaked {type(exc).__name__} for invalid base URL"
                        )

                self.assertNotEqual(result, 0)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn("audit", stderr.getvalue())
                self.assertNotIn(base_url, stderr.getvalue())

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
        self.assertNotIn("private-video.mp4", stderr.getvalue())
        self.assertNotIn(base_url, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
