from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from server.routers.video import frame_count_for, get_meta


def context(cached: dict | None):
    index = MagicMock()
    index.get.return_value = SimpleNamespace(
        relpath="folder/clip.mp4",
        name="clip.mp4",
        size_bytes=123,
        file_mtime=456.0,
    )
    index.cached_probe.return_value = cached
    index.probe = AsyncMock(
        return_value={
            "frame_count": 321,
            "frame_count_source": "container",
        }
    )
    return SimpleNamespace(object_id="boom", label="boom", index=index)


class VideoRequestLatencyTests(unittest.TestCase):
    def test_known_container_count_is_used_without_packet_scan(self) -> None:
        ctx = context({"frame_count": 120, "frame_count_source": "container"})
        with patch("server.routers.video.proxy.is_complete", return_value=None):
            count = asyncio.run(frame_count_for(ctx, "video-1"))

        self.assertEqual(count, 120)
        ctx.index.probe.assert_not_awaited()

    def test_missing_probe_uses_bounded_metadata_probe(self) -> None:
        ctx = context(None)
        with patch("server.routers.video.proxy.is_complete", return_value=None):
            count = asyncio.run(frame_count_for(ctx, "video-1"))

        self.assertEqual(count, 321)
        ctx.index.probe.assert_awaited_once_with("video-1", count_packets=False)

    def test_meta_refresh_never_counts_every_packet_in_request(self) -> None:
        ctx = context({"frame_count": 120, "frame_count_source": "container"})

        payload = asyncio.run(get_meta("video-1", refresh=True, ctx=ctx))

        self.assertEqual(payload["media"]["frame_count"], 321)
        ctx.index.probe.assert_awaited_once_with("video-1", count_packets=False)


if __name__ == "__main__":
    unittest.main()
