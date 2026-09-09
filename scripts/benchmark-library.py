#!/usr/bin/env python3
"""Measure warmed latency of an existing object-library GET endpoint."""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Sequence
from urllib import error, parse, request


def percentile(samples: Sequence[float], percentage: float) -> float:
    """Return the deterministic nearest-rank percentile for non-empty samples."""
    if not samples:
        raise ValueError("percentile requires at least one sample")
    if not 0 < percentage <= 100:
        raise ValueError("percentage must be in (0, 100]")

    ordered = sorted(samples)
    rank = math.ceil((percentage / 100) * len(ordered))
    return ordered[rank - 1]


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure warmed latency of an existing object library."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--object-id", required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=10.0, metavar="SECONDS")
    args = parser.parse_args(argv)
    timings_ms: list[float] = []

    try:
        if args.samples <= 0:
            raise ValueError("sample count must be positive")
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("timeout must be finite and positive")

        base_url = parse.urlsplit(args.base_url)
        if base_url.scheme not in {"http", "https"}:
            raise ValueError("base URL must use HTTP or HTTPS")
        if base_url.hostname is None:
            raise ValueError("base URL must include a host")
        if base_url.username is not None or base_url.password is not None:
            raise ValueError("base URL must not include user information")
        if base_url.query or base_url.fragment:
            raise ValueError("base URL must not include query or fragment")
        _ = base_url.port  # Validate the port before constructing the request.

        object_id = parse.quote(args.object_id, safe="")
        endpoint_path = (
            f"{base_url.path.rstrip('/')}/api/objects/{object_id}/videos"
        )
        url = parse.urlunsplit(
            (base_url.scheme, base_url.netloc, endpoint_path, "", "")
        )
        http_request = request.Request(
            url, headers={"Accept": "application/json"}, method="GET"
        )
        opener = request.build_opener(_NoRedirectHandler())

        for iteration in range(args.samples + 1):
            started = time.perf_counter()
            with opener.open(http_request, timeout=args.timeout) as response:
                response.read()
                if not 200 <= response.status < 300:
                    raise error.HTTPError(
                        url, response.status, "non-2xx response", response.headers, None
                    )
            elapsed_ms = (time.perf_counter() - started) * 1000
            if iteration:
                timings_ms.append(elapsed_ms)
    except Exception:
        print(
            "benchmark failed: request did not return a usable 2xx response",
            file=sys.stderr,
        )
        return 1

    print(f"sample_count: {len(timings_ms)}")
    print(f"p50_ms: {percentile(timings_ms, 50):.2f}")
    print(f"p95_ms: {percentile(timings_ms, 95):.2f}")
    print(f"min_ms: {min(timings_ms):.2f}")
    print(f"max_ms: {max(timings_ms):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
