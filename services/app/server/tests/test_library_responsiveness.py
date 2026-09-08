"""Responsiveness and coalescing tests for the library listing."""

from __future__ import annotations

import asyncio
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server.routers import library


class _FakeIndex:
    scanned_at = "2026-09-08T12:00:00+00:00"

    def all(self) -> list[object]:
        return []

    def cached_probe(self, _video_id: str) -> None:
        return None


class _FakeStore:
    def counts(self, total: int) -> dict[str, int]:
        return {"total": total}


class _FakeContext:
    def __init__(self, object_id: str) -> None:
        self.object_id = object_id
        self.label = object_id
        self.videos_root = Path("/fake/videos")
        self.index = _FakeIndex()
        self.store = _FakeStore()


class LibraryResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_listing_builder_does_not_block_event_loop(self) -> None:
        ctx = _FakeContext("responsive-object")
        builder_started = threading.Event()
        release_builder = threading.Event()
        loop_progress = threading.Event()
        observed_progress: list[bool] = []
        expected = {"object_id": ctx.object_id, "videos": []}

        def blocking_builder(*_args: object, **_kwargs: object) -> dict:
            builder_started.set()
            release_builder.wait(timeout=2)
            return expected

        def release_after_observation() -> None:
            builder_started.wait(timeout=1)
            observed_progress.append(loop_progress.wait(timeout=0.5))
            release_builder.set()

        async def mark_loop_progress() -> None:
            await asyncio.sleep(0)
            loop_progress.set()

        watchdog = threading.Thread(target=release_after_observation)
        watchdog.start()
        try:
            with patch.object(
                library, "_build_video_listing", blocking_builder, create=True
            ):
                listing = asyncio.create_task(library.list_videos(ctx=ctx))
                progress = asyncio.create_task(mark_loop_progress())
                result = await asyncio.wait_for(listing, timeout=2)
                await progress
        finally:
            release_builder.set()
            watchdog.join(timeout=2)

        self.assertTrue(builder_started.is_set())
        self.assertEqual(result, expected)
        self.assertEqual(observed_progress, [True])

    async def test_identical_concurrent_listings_share_one_builder(self) -> None:
        ctx = _FakeContext("coalesced-object")
        builder_started = threading.Event()
        release_builder = threading.Event()
        builder_calls = 0
        expected = {"object_id": ctx.object_id, "videos": []}

        def blocking_builder(*_args: object, **_kwargs: object) -> dict:
            nonlocal builder_calls
            builder_calls += 1
            builder_started.set()
            release_builder.wait(timeout=2)
            return expected

        with patch.object(
            library, "_build_video_listing", blocking_builder, create=True
        ):
            first = asyncio.create_task(
                library.list_videos(search="needle", status="all", ctx=ctx)
            )
            second = asyncio.create_task(
                library.list_videos(search="needle", status="all", ctx=ctx)
            )
            try:
                started = await asyncio.wait_for(
                    asyncio.to_thread(builder_started.wait, 1), timeout=2
                )
                await asyncio.sleep(0.05)
                self.assertTrue(started)
                self.assertEqual(builder_calls, 1)
            finally:
                release_builder.set()

            self.assertEqual(await asyncio.gather(first, second), [expected, expected])


if __name__ == "__main__":
    unittest.main()
