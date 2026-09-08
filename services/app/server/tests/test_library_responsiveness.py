"""Responsiveness and coalescing tests for the library listing."""

from __future__ import annotations

import asyncio
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.routers import library
from server.store import AnnotationStore


class _FakeIndex:
    scanned_at = "2026-09-08T12:00:00+00:00"

    def __init__(self, videos: list[object] | None = None) -> None:
        self._videos = videos or []

    def all(self) -> list[object]:
        return list(self._videos)

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

    async def test_listing_uses_one_store_snapshot_and_releases_it_before_pipeline_io(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            relpath = "video.mp4"
            store = AnnotationStore(
                annotations_path=root / "annotations.json",
                cache_dir=root / "cache",
                videos_root=root / "videos",
                output_root=root / "output",
                object_id="snapshot-object",
                label="Snapshot object",
                total_provider=lambda: 1,
            )
            store.load()
            await store.put_entry(relpath, {"status": "pending", "intervals": []})
            video = SimpleNamespace(
                video_id="video-1",
                relpath=relpath,
                name=relpath,
                size_bytes=10,
                file_mtime=1.0,
                abspath=root / relpath,
            )
            ctx = SimpleNamespace(
                object_id="snapshot-object",
                label="Snapshot object",
                videos_root=root / "videos",
                output_root=root / "output",
                index=_FakeIndex([video]),
                store=store,
            )
            pipeline_started = threading.Event()
            release_pipeline = threading.Event()
            writer_finished = threading.Event()
            writer_finished_before_release: list[bool] = []

            def blocking_pipeline(*_args: object, **_kwargs: object) -> object:
                pipeline_started.set()
                release_pipeline.wait(timeout=2)
                return SimpleNamespace(
                    stage="ready",
                    status="ready",
                    public_progress=lambda: {},
                )

            def release_after_writer_observation() -> None:
                writer_finished_before_release.append(
                    writer_finished.wait(timeout=0.5)
                )
                release_pipeline.set()

            async def update_store() -> None:
                await store.put_entry(
                    relpath, {"status": "done", "intervals": []}
                )
                writer_finished.set()

            with (
                patch.object(library.locks, "map_for", return_value={}),
                patch.object(library.sam3_queue, "map_for", return_value={}),
                patch.object(library, "inspect_pipeline_entry", blocking_pipeline),
            ):
                listing = asyncio.create_task(library.list_videos(ctx=ctx))
                started = await asyncio.wait_for(
                    asyncio.to_thread(pipeline_started.wait, 1), timeout=2
                )
                self.assertTrue(started)
                writer = asyncio.create_task(update_store())
                watchdog = threading.Thread(target=release_after_writer_observation)
                watchdog.start()
                try:
                    result, _ = await asyncio.wait_for(
                        asyncio.gather(listing, writer), timeout=2
                    )
                finally:
                    release_pipeline.set()
                    watchdog.join(timeout=2)

            self.assertEqual(writer_finished_before_release, [True])
            self.assertEqual(result["videos"][0]["status"], "pending")
            self.assertEqual(result["counts"]["pending"], 1)
            self.assertEqual(result["counts"]["done"], 0)


if __name__ == "__main__":
    unittest.main()
