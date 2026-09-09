from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from server.routers.video import WindowPayload, start_window


class ProxyWindowForceTests(unittest.TestCase):
    def test_force_bypasses_a_stale_available_range(self) -> None:
        index = MagicMock()
        index.get.return_value = SimpleNamespace(video_id="video-1")
        index.cached_probe.return_value = {"frame_count": 100}
        ctx = SimpleNamespace(object_id="boom", index=index)

        with (
            patch("server.routers.video.proxy.is_complete", return_value=None),
            patch("server.routers.video.proxy.available_ranges", return_value=[(8, 12)]),
            patch("server.routers.video.durable_jobs.enabled", return_value=True),
            patch("server.routers.video.durable_jobs.create", return_value="repair-job") as create,
        ):
            result = asyncio.run(
                start_window(
                    "video-1",
                    WindowPayload(center=10, radius=2, force=True),
                    ctx,
                    "browser-1",
                )
            )

        self.assertEqual(result["job_id"], "repair-job")
        self.assertFalse(result["already_available"])
        create.assert_called_once()

    def test_force_is_forwarded_to_the_in_process_proxy(self) -> None:
        index = MagicMock()
        index.get.return_value = SimpleNamespace(video_id="video-1")
        index.cached_probe.return_value = {"frame_count": 100}
        ctx = SimpleNamespace(object_id="boom", index=index)
        job = SimpleNamespace(job_id="repair-job")

        with (
            patch("server.routers.video.proxy.is_complete", return_value=None),
            patch("server.routers.video.proxy.available_ranges", return_value=[(8, 12)]),
            patch("server.routers.video.durable_jobs.enabled", return_value=False),
            patch(
                "server.routers.video.proxy.start_window",
                new=AsyncMock(return_value=(job, 8, 12)),
            ) as start,
        ):
            result = asyncio.run(
                start_window(
                    "video-1",
                    WindowPayload(center=10, radius=2, force=True),
                    ctx,
                    "browser-1",
                )
            )

        self.assertEqual(result["job_id"], "repair-job")
        self.assertFalse(result["already_available"])
        self.assertTrue(start.await_args.kwargs["force"])


if __name__ == "__main__":
    unittest.main()
