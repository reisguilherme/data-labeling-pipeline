from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from server import durable_jobs, export as export_module
from server.models import BBoxIn, IntervalIn, VideoEntryIn
from server.routers.annotations import (
    FinishPayload,
    NoBoomPayload,
    _cleanup_basename,
    finish_export,
    mark_no_object,
    put_one,
)
from server.store import AnnotationStore
from server.users import User
from server.videos import VideoFile


def _user() -> User:
    return User("guilherme", "Guilherme", "#fff", "2026-01-01T00:00:00Z")


class _Index:
    def __init__(self, root: Path, media: dict | None) -> None:
        source = root / "clip.mp4"
        source.write_bytes(b"video")
        self.video = VideoFile(
            video_id="video-1",
            relpath="clip.mp4",
            abspath=source,
            name="clip",
            size_bytes=5,
            file_mtime="2026-01-01T00:00:00Z",
        )
        self.media = media
        self.probe_calls: list[bool] = []

    def get(self, video_id: str):
        return self.video if video_id == self.video.video_id else None

    def cached_probe(self, _video_id: str):
        return dict(self.media) if self.media is not None else None

    def resolve_path(self, video_id: str) -> Path:
        if video_id != self.video.video_id:
            raise KeyError(video_id)
        return self.video.abspath

    async def probe(self, _video_id: str, *, count_packets: bool = False):
        self.probe_calls.append(count_packets)
        return {
            "width": 640,
            "height": 360,
            "fps": 30.0,
            "frame_count": 100,
        }


def _context(root: Path, media: dict | None = None):
    index = _Index(
        root,
        media
        or {
            "width": 640,
            "height": 360,
            "fps": 30.0,
            "frame_count": 100,
        },
    )
    output = root / "dataset"
    store = AnnotationStore(
        annotations_path=output / "annotations.json",
        cache_dir=output / "_cache",
        videos_root=root,
        output_root=output,
        object_id="boom",
        label="boom",
        total_provider=lambda: 1,
    )
    store.load()
    return SimpleNamespace(
        object_id="boom",
        label="boom",
        index=index,
        store=store,
        output_root=output,
        cache_dir=output / "_cache",
        config=SimpleNamespace(auto_sam3=True),
    )


def _payload(note: str = "") -> VideoEntryIn:
    return VideoEntryIn(
        status="in_progress",
        notes=note,
        intervals=[
            IntervalIn(
                start_frame=2,
                end_frame=4,
                prompt_frame=2,
                bboxes=[BBoxIn(normalized=[0.1, 0.2, 0.3, 0.4])],
                flags={"dificuldade": "facil"},
            )
        ],
    )


@asynccontextmanager
async def _no_lock(*_args, **_kwargs):
    yield


class TriageMutationTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_local_export_failure_preserves_previous(
        self, *, child_state: str
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            entry = {
                "status": "in_progress",
                "annotation_revision": 5,
                "media": {"width": 640, "height": 360},
                "intervals": [
                    {
                        "segment": "seg_00",
                        "index": 0,
                        "frame_count": 1,
                        "start_frame": 0,
                        "end_frame": 0,
                        "prompt_frame": 0,
                        "bboxes": [],
                        "flags": {},
                    }
                ],
            }
            await ctx.store.put_entry("clip.mp4", entry)
            root = export_module.export_root_for(ctx, ctx.index.video)
            old_frame = root / "seg_00" / "old.jpg"
            old_frame.parent.mkdir(parents=True)
            old_frame.write_bytes(b"previous-generation")
            old_owner = export_module.export_owner(ctx, ctx.index.video, 4)
            export_module.write_export_owner(root, old_owner)

            async def finish_child(child, _argv):
                child.state = child_state
                child.error = "ffmpeg falhou" if child_state == "error" else None
                return child

            with (
                patch(
                    "server.export.ffmpeg.resolve",
                    return_value=SimpleNamespace(version="test"),
                ),
                patch(
                    "server.export.ffmpeg.export_segment_argv",
                    return_value=["fake-ffmpeg"],
                ),
                patch("server.export.jobs.run", side_effect=finish_child),
            ):
                job = await export_module.export_video(ctx, "video-1", entry)
                for _ in range(100):
                    if job.terminal:
                        break
                    await asyncio.sleep(0)

            self.assertTrue(job.terminal, "job local nao finalizou")
            self.assertEqual(job.state, "error")
            self.assertEqual(old_frame.read_bytes(), b"previous-generation")
            self.assertEqual(export_module.read_export_owner(root), old_owner)
            self.assertFalse(any(ctx.output_root.glob(f".{root.name}.export-*.part")))

    async def test_local_export_ffmpeg_failure_preserves_previous_export(self) -> None:
        await self._assert_local_export_failure_preserves_previous(child_state="error")

    async def test_local_export_count_failure_preserves_previous_export(self) -> None:
        # O filho termina com exit 0, mas nenhum JPEG foi gerado.
        await self._assert_local_export_failure_preserves_previous(child_state="done")

    async def test_export_roots_are_stable_and_distinct_for_equal_stems(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            legacy = ctx.output_root / "clip"
            legacy.mkdir(parents=True)
            (legacy / "seg_00").mkdir()
            (legacy / "seg_00" / "000000.jpg").write_bytes(b"legacy")
            first = VideoFile(
                video_id="111111111111",
                relpath="a/clip.mp4",
                abspath=Path(temporary) / "a" / "clip.mp4",
                name="clip",
                size_bytes=1,
                file_mtime="2026-01-01T00:00:00Z",
            )
            second = VideoFile(
                video_id="222222222222",
                relpath="b/clip.mp4",
                abspath=Path(temporary) / "b" / "clip.mp4",
                name="clip",
                size_bytes=1,
                file_mtime="2026-01-01T00:00:00Z",
            )

            first_root = export_module.export_root_for(ctx, first)
            first_root_again = export_module.export_root_for(ctx, first)
            first_root.mkdir(parents=True)
            (first_root / "legacy.txt").write_text("first", encoding="utf-8")
            second_root = export_module.export_root_for(ctx, second)

            self.assertNotEqual(first_root, second_root)
            self.assertEqual(first_root, first_root_again)
            self.assertIn(first.video_id, first_root.name)
            self.assertIn(second.video_id, second_root.name)
            self.assertEqual((first_root / "legacy.txt").read_text(encoding="utf-8"), "first")
            self.assertEqual(
                (legacy / "seg_00" / "000000.jpg").read_bytes(), b"legacy"
            )

    async def test_cleanup_basename_rejects_original_symlink_to_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dataset"
            owned = output / "owned"
            owned.mkdir(parents=True)
            link = output / "alias"
            try:
                link.symlink_to(owned, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink indisponivel neste host: {exc}")

            self.assertIsNone(
                _cleanup_basename(output, {"root": link.as_posix()})
            )

    async def test_put_uses_bounded_media_and_never_scans_or_cleans_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            with (
                patch("server.routers.annotations.proxy.is_complete", return_value=100),
                patch(
                    "server.routers.annotations.proxy.status",
                    side_effect=AssertionError("proxy.status enumerates ranges"),
                ),
                patch(
                    "server.routers.annotations.export_module.clean_segments",
                    side_effect=AssertionError("request cleaned filesystem"),
                ),
                patch("server.routers.annotations.durable_jobs.enabled", return_value=False),
            ):
                entry = await put_one("video-1", _payload(), False, ctx, _user(), "tab-1")

            self.assertEqual(ctx.index.probe_calls, [])
            self.assertEqual(entry["annotation_revision"], 1)
            self.assertEqual(entry["proxy_mode"], "full")

    async def test_put_fallback_probe_never_counts_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary), media={})
            ctx.index.media = None
            with (
                patch("server.routers.annotations.proxy.is_complete", return_value=None),
                patch("server.routers.annotations.durable_jobs.enabled", return_value=False),
            ):
                await put_one("video-1", _payload(), False, ctx, _user(), "tab-1")

            self.assertEqual(ctx.index.probe_calls, [False])

    async def test_no_object_persists_before_async_cleanup_enqueue_and_never_probes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = _context(root)
            export_root = ctx.output_root / "clip"
            export_root.mkdir(parents=True)
            await ctx.store.put_entry(
                "clip.mp4",
                {
                    "video_id": "video-1",
                    "relpath": "clip.mp4",
                    "status": "done",
                    "annotation_revision": 7,
                    "media": {"width": 640, "height": 360, "frame_count": 100},
                    "intervals": [{"segment": "seg_00"}],
                    "export": {"root": export_root.as_posix(), "segments": ["seg_00"]},
                },
            )
            create_thread: list[int] = []

            def create(**_kwargs):
                create_thread.append(threading.get_ident())
                persisted = json.loads(ctx.store.annotations_path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["videos"]["clip.mp4"]["status"], "no_boom")
                return "cleanup-job"

            with (
                patch("server.routers.annotations.proxy.is_complete", return_value=None),
                patch(
                    "server.routers.annotations.export_module.clean_segments",
                    side_effect=AssertionError("request cleaned filesystem"),
                ),
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    _no_lock,
                    create=True,
                ),
                patch(
                    "server.routers.annotations.durable_jobs.cancel_stale_video_exports",
                    create=True,
                ),
                patch("server.routers.annotations.durable_jobs.create", side_effect=create),
            ):
                result = await mark_no_object(
                    "video-1", NoBoomPayload(), False, ctx, _user(), "tab-1"
                )

            self.assertEqual(ctx.index.probe_calls, [])
            self.assertEqual(result["status"], "no_boom")
            self.assertEqual(result["annotation_revision"], 8)
            self.assertEqual(result["cleanup_job_id"], "cleanup-job")
            self.assertNotEqual(create_thread, [threading.get_ident()])
            self.assertIsNone(result["export"])
            self.assertEqual(result["export_cleanup"]["previous_export"]["segments"], ["seg_00"])
            self.assertEqual(
                result["export_cleanup"]["owner"],
                export_module.export_owner(ctx, ctx.index.video, 7),
            )

    async def test_failed_cleanup_enqueue_keeps_no_object_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = _context(root)
            export_root = ctx.output_root / "clip"
            export_root.mkdir(parents=True)
            await ctx.store.put_entry(
                "clip.mp4",
                {
                    "status": "done",
                    "annotation_revision": 1,
                    "media": {"frame_count": 100},
                    "intervals": [{"segment": "seg_00"}],
                    "export": {"root": export_root.as_posix(), "segments": ["seg_00"]},
                },
            )
            with (
                patch("server.routers.annotations.proxy.is_complete", return_value=None),
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    _no_lock,
                    create=True,
                ),
                patch(
                    "server.routers.annotations.durable_jobs.cancel_stale_video_exports",
                    create=True,
                ),
                patch(
                    "server.routers.annotations.durable_jobs.create",
                    side_effect=RuntimeError("db unavailable"),
                ),
            ):
                result = await mark_no_object(
                    "video-1", NoBoomPayload(), False, ctx, _user(), "tab-1"
                )

            persisted = ctx.store.entry("clip.mp4")
            self.assertEqual(result["status"], "no_boom")
            self.assertEqual(persisted["status"], "no_boom")
            self.assertEqual(persisted["export_cleanup"]["status"], "deferred")
            self.assertNotIn("db unavailable", persisted["export_cleanup"]["error"])

    async def test_deferred_cleanup_retry_keeps_revision_and_requeues_same_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = _context(root)
            owner = {
                "schema_version": 1,
                "object_id": "boom",
                "video_id": "video-1",
                "relpath": "clip.mp4",
                "annotation_revision": 1,
            }
            cleanup = {
                "status": "deferred",
                "annotation_revision": 2,
                "root_basename": "clip__video-1",
                "owner": owner,
                "previous_export": {
                    "root": (ctx.output_root / "clip__video-1").as_posix(),
                    "segments": ["seg_00"],
                    "annotation_revision": 1,
                    "owner": owner,
                },
                "requested_at": "2026-01-01T00:00:00Z",
                "error": "fila de limpeza temporariamente indisponivel",
            }
            await ctx.store.put_entry(
                "clip.mp4",
                {
                    "video_id": "video-1",
                    "relpath": "clip.mp4",
                    "status": "no_boom",
                    "annotation_revision": 2,
                    "intervals": [],
                    "export": None,
                    "export_cleanup": cleanup,
                    "history": [{"revision": 2, "action": "no_object"}],
                },
            )
            creates: list[dict] = []

            def create(**kwargs):
                creates.append(kwargs)
                return "cleanup-retry"

            with (
                patch("server.routers.annotations.proxy.is_complete", return_value=None),
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    _no_lock,
                ),
                patch("server.routers.annotations.durable_jobs.cancel_stale_video_exports"),
                patch("server.routers.annotations.durable_jobs.create", side_effect=create),
            ):
                result = await mark_no_object(
                    "video-1", NoBoomPayload(), False, ctx, _user(), "tab-1"
                )

            self.assertEqual(result["annotation_revision"], 2)
            self.assertEqual(len(result["history"]), 1)
            self.assertEqual(result["export_cleanup"]["status"], "queued")
            self.assertEqual(result["cleanup_job_id"], "cleanup-retry")
            self.assertEqual(len(creates), 1)
            self.assertEqual(creates[0]["payload"]["owner"], owner)
            self.assertEqual(
                creates[0]["idempotency_key"], "video-export-cleanup:boom:video-1:r2"
            )

    async def test_same_video_mutations_increment_revision_and_preserve_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            with (
                patch("server.routers.annotations.proxy.is_complete", return_value=100),
                patch("server.routers.annotations.durable_jobs.enabled", return_value=False),
            ):
                first, second = await asyncio.gather(
                    put_one("video-1", _payload("first"), False, ctx, _user(), "tab-1"),
                    put_one("video-1", _payload("second"), False, ctx, _user(), "tab-1"),
                )

            self.assertEqual({first["annotation_revision"], second["annotation_revision"]}, {1, 2})
            final = ctx.store.entry("clip.mp4")
            self.assertEqual(final["annotation_revision"], 2)
            self.assertEqual(len(final["history"]), 2)
            self.assertEqual([item["revision"] for item in final["history"]], [1, 2])

    async def test_separate_application_stores_reload_inside_shared_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_ctx = _context(root)
            second_ctx = _context(root)
            shared = asyncio.Lock()

            @asynccontextmanager
            async def shared_lock(*_args, **_kwargs):
                async with shared:
                    yield

            with (
                patch("server.routers.annotations.proxy.is_complete", return_value=100),
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    shared_lock,
                ),
                patch(
                    "server.routers.annotations.durable_jobs.cancel_stale_video_exports"
                ),
            ):
                results = await asyncio.gather(
                    put_one(
                        "video-1", _payload("first process"), False,
                        first_ctx, _user(), "tab-1",
                    ),
                    put_one(
                        "video-1", _payload("second process"), False,
                        second_ctx, _user(), "tab-2",
                    ),
                )

            self.assertEqual(
                {entry["annotation_revision"] for entry in results}, {1, 2}
            )
            persisted = json.loads(
                first_ctx.store.annotations_path.read_text(encoding="utf-8")
            )["videos"]["clip.mp4"]
            self.assertEqual(persisted["annotation_revision"], 2)
            self.assertEqual([item["revision"] for item in persisted["history"]], [1, 2])

    async def test_cleanup_enqueue_does_not_block_other_coroutines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = _context(root)
            export_root = ctx.output_root / "clip"
            export_root.mkdir(parents=True)
            await ctx.store.put_entry(
                "clip.mp4",
                {
                    "status": "done",
                    "annotation_revision": 2,
                    "media": {"frame_count": 100},
                    "intervals": [{"segment": "seg_00"}],
                    "export": {"root": export_root.as_posix(), "segments": ["seg_00"]},
                },
            )
            started = threading.Event()
            release = threading.Event()

            def slow_create(**_kwargs):
                started.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("test timeout")
                return "cleanup-job"

            with (
                patch("server.routers.annotations.proxy.is_complete", return_value=None),
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    _no_lock,
                ),
                patch(
                    "server.routers.annotations.durable_jobs.cancel_stale_video_exports"
                ),
                patch(
                    "server.routers.annotations.durable_jobs.create",
                    side_effect=slow_create,
                ),
            ):
                task = asyncio.create_task(
                    mark_no_object(
                        "video-1", NoBoomPayload(), False, ctx, _user(), "tab-1"
                    )
                )
                await asyncio.to_thread(started.wait, 1)
                ticked = False

                async def tick() -> None:
                    nonlocal ticked
                    await asyncio.sleep(0)
                    ticked = True

                await tick()
                self.assertTrue(ticked)
                self.assertFalse(task.done())
                release.set()
                result = await task

            self.assertEqual(result["cleanup_job_id"], "cleanup-job")

    async def test_stale_durable_export_cannot_promote_later_no_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            await ctx.store.put_entry(
                "clip.mp4",
                {
                    "status": "no_boom",
                    "annotation_revision": 6,
                    "intervals": [],
                    "export": None,
                },
            )
            stale_job = {
                "job_id": "00000000-0000-0000-0000-000000000001",
                "kind": "video_export",
                "object_id": "boom",
                "video_id": "video-1",
                "annotation_revision": 5,
                "state": "done",
                "result": {
                    "root": (ctx.output_root / "clip").as_posix(),
                    "segments": ["seg_00"],
                },
            }
            with (
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch("server.routers.annotations.durable_jobs.get", return_value=stale_job),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    _no_lock,
                ),
            ):
                with self.assertRaisesRegex(Exception, "revis"):
                    await finish_export(
                        "video-1",
                        FinishPayload(job_id=stale_job["job_id"]),
                        ctx,
                        _user(),
                    )

            persisted = ctx.store.entry("clip.mp4")
            self.assertEqual(persisted["status"], "no_boom")
            self.assertIsNone(persisted["export"])

    async def test_unfenced_legacy_export_cannot_promote_a_new_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            await ctx.store.put_entry(
                "clip.mp4",
                {
                    "status": "no_boom",
                    "annotation_revision": 1,
                    "intervals": [],
                    "export": None,
                },
            )
            legacy_job = {
                "job_id": "00000000-0000-0000-0000-000000000002",
                "kind": "video_export",
                "object_id": "boom",
                "video_id": "video-1",
                "annotation_revision": None,
                "state": "done",
                "result": {
                    "root": (ctx.output_root / "clip").as_posix(),
                    "segments": ["seg_00"],
                },
            }
            with (
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch("server.routers.annotations.durable_jobs.get", return_value=legacy_job),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    _no_lock,
                ),
            ):
                with self.assertRaisesRegex(Exception, "revis"):
                    await finish_export(
                        "video-1",
                        FinishPayload(job_id=legacy_job["job_id"]),
                        ctx,
                        _user(),
                    )

            self.assertEqual(ctx.store.entry("clip.mp4")["status"], "no_boom")

    async def test_finish_rejects_job_for_another_video_or_wrong_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            await ctx.store.put_entry(
                "clip.mp4",
                {
                    "status": "in_progress",
                    "annotation_revision": 4,
                    "intervals": [{"segment": "seg_00"}],
                    "export": None,
                },
            )
            base = {
                "job_id": "00000000-0000-0000-0000-000000000003",
                "kind": "video_export",
                "object_id": "boom",
                "video_id": "other-video",
                "annotation_revision": 4,
                "root_basename": "clip__video-1",
                "state": "done",
                "result": {
                    "root": (ctx.output_root / "clip__video-1").as_posix(),
                    "segments": ["seg_00"],
                    "annotation_revision": 4,
                },
            }
            with (
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch("server.routers.annotations.durable_jobs.get", return_value=base),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    _no_lock,
                ),
            ):
                with self.assertRaisesRegex(Exception, "outro video"):
                    await finish_export(
                        "video-1", FinishPayload(job_id=base["job_id"]), ctx, _user()
                    )

            wrong_kind = {**base, "video_id": "video-1", "kind": "proxy_full"}
            with (
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch("server.routers.annotations.durable_jobs.get", return_value=wrong_kind),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    _no_lock,
                ),
            ):
                with self.assertRaisesRegex(Exception, "tipo"):
                    await finish_export(
                        "video-1", FinishPayload(job_id=base["job_id"]), ctx, _user()
                    )

    async def test_finish_rejects_result_root_different_from_job_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            await ctx.store.put_entry(
                "clip.mp4",
                {
                    "status": "in_progress",
                    "annotation_revision": 4,
                    "intervals": [{"segment": "seg_00"}],
                    "export": None,
                },
            )
            job = {
                "job_id": "00000000-0000-0000-0000-000000000004",
                "kind": "video_export",
                "object_id": "boom",
                "video_id": "video-1",
                "annotation_revision": 4,
                "root_basename": "clip__video-1",
                "state": "done",
                "result": {
                    "root": (ctx.output_root / "victim").as_posix(),
                    "segments": ["seg_00"],
                    "annotation_revision": 4,
                    "owner": {
                        "schema_version": 1,
                        "object_id": "boom",
                        "video_id": "video-1",
                        "relpath": "clip.mp4",
                        "annotation_revision": 4,
                    },
                },
            }
            with (
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch("server.routers.annotations.durable_jobs.get", return_value=job),
            ):
                with self.assertRaisesRegex(Exception, "raiz"):
                    await finish_export(
                        "video-1", FinishPayload(job_id=job["job_id"]), ctx, _user()
                    )

    async def test_finish_rejects_segments_different_from_current_annotation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            ctx.config.auto_sam3 = False
            owner = export_module.export_owner(ctx, ctx.index.video, 4)
            root = ctx.output_root / "clip__video-1"
            (root / "seg_99").mkdir(parents=True)
            export_module.write_export_owner(root, owner)
            await ctx.store.put_entry(
                "clip.mp4",
                {
                    "status": "in_progress",
                    "annotation_revision": 4,
                    "intervals": [{"segment": "seg_00", "frame_count": 1}],
                    "export": None,
                },
            )
            job = {
                "job_id": "00000000-0000-0000-0000-000000000005",
                "kind": "video_export",
                "object_id": "boom",
                "video_id": "video-1",
                "annotation_revision": 4,
                "root_basename": root.name,
                "state": "done",
                "result": {
                    "root": root.as_posix(),
                    "segments": ["seg_99"],
                    "total_frames": 1,
                    "annotation_revision": 4,
                    "owner": owner,
                },
            }
            with (
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch("server.routers.annotations.durable_jobs.get", return_value=job),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    _no_lock,
                ),
            ):
                with self.assertRaisesRegex(Exception, "segmentos"):
                    await finish_export(
                        "video-1", FinishPayload(job_id=job["job_id"]), ctx, _user()
                    )

    async def test_finish_accepts_only_matching_owned_export_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            ctx.config.auto_sam3 = False
            owner = export_module.export_owner(ctx, ctx.index.video, 4)
            root = ctx.output_root / "clip__video-1"
            (root / "seg_00").mkdir(parents=True)
            export_module.write_export_owner(root, owner)
            await ctx.store.put_entry(
                "clip.mp4",
                {
                    "status": "in_progress",
                    "annotation_revision": 4,
                    "intervals": [{"segment": "seg_00", "frame_count": 1}],
                    "export": None,
                },
            )
            job = {
                "job_id": "00000000-0000-0000-0000-000000000006",
                "kind": "video_export",
                "object_id": "boom",
                "video_id": "video-1",
                "annotation_revision": 4,
                "root_basename": root.name,
                "state": "done",
                "result": {
                    "root": root.as_posix(),
                    "segments": ["seg_00"],
                    "total_frames": 1,
                    "annotation_revision": 4,
                    "owner": owner,
                },
            }
            with (
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch("server.routers.annotations.durable_jobs.get", return_value=job),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    _no_lock,
                ),
            ):
                result = await finish_export(
                    "video-1", FinishPayload(job_id=job["job_id"]), ctx, _user()
                )

            self.assertEqual(result["status"], "done")
            self.assertEqual(result["export"], job["result"])

    async def test_export_job_is_revision_specific_and_created_off_event_loop(self) -> None:
        from server.routers.annotations import export_video

        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            await ctx.store.put_entry(
                "clip.mp4",
                {
                    "status": "in_progress",
                    "annotation_revision": 4,
                    "intervals": [{"frame_count": 3, "segment": "seg_00"}],
                },
            )
            caller = threading.get_ident()
            create_threads: list[int] = []

            def create(**kwargs):
                create_threads.append(threading.get_ident())
                self.assertEqual(kwargs["payload"]["annotation_revision"], 4)
                self.assertEqual(kwargs["payload"]["root_basename"], "clip__video-1")
                self.assertEqual(
                    kwargs["payload"]["owner"],
                    export_module.export_owner(ctx, ctx.index.video, 4),
                )
                self.assertEqual(kwargs["idempotency_key"], "video-export:boom:video-1:r4")
                return "export-job"

            with (
                patch("server.routers.annotations.durable_jobs.enabled", return_value=True),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    _no_lock,
                ),
                patch("server.routers.annotations.durable_jobs.create", side_effect=create),
            ):
                result = await export_video("video-1", False, ctx, _user(), "tab-1")

            self.assertEqual(result["job_id"], "export-job")
            self.assertTrue(create_threads)
            self.assertTrue(all(thread != caller for thread in create_threads))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL ausente")
class DurableCreatePostgresTests(unittest.TestCase):
    def test_concurrent_idempotent_create_returns_one_job(self) -> None:
        import psycopg

        url = os.environ["TEST_DATABASE_URL"]
        schema = "create_test_" + uuid.uuid4().hex
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(f'CREATE SCHEMA "{schema}"')
            connection.execute(
                f'''CREATE TABLE "{schema}".jobs (
                    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    kind text NOT NULL,
                    worker_kind text NOT NULL,
                    state text NOT NULL DEFAULT 'queued',
                    priority integer NOT NULL,
                    payload jsonb NOT NULL,
                    result jsonb,
                    error text,
                    attempts integer NOT NULL DEFAULT 0,
                    idempotency_key text UNIQUE,
                    worker_id text,
                    lease_token uuid,
                    lease_expires_at timestamptz,
                    cancel_requested boolean NOT NULL DEFAULT false,
                    progress jsonb NOT NULL DEFAULT '{{}}',
                    started_at timestamptz,
                    finished_at timestamptz,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )'''
            )

        def connect():
            return psycopg.connect(url, options=f"-c search_path={schema}")

        try:
            with patch("server.durable_jobs._connect", side_effect=connect):
                barrier = threading.Barrier(2)

                def create_one() -> str:
                    barrier.wait(timeout=2)
                    return durable_jobs.create(
                        kind="video_export",
                        object_id="boom",
                        payload={"video_id": "video-1", "annotation_revision": 9},
                        idempotency_key="same-key",
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    ids = list(pool.map(lambda _index: create_one(), range(2)))

            self.assertEqual(ids[0], ids[1])
            with connect() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        finally:
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


class DurableAdvisoryLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_lock_acquisition_releases_session_lock(self) -> None:
        started = threading.Event()
        release = threading.Event()
        exited = threading.Event()

        @asynccontextmanager
        async def consume_lock():
            async with durable_jobs.video_advisory_lock_async("boom", "video-1"):
                yield

        from contextlib import contextmanager

        @contextmanager
        def blocking_lock(*_args, **_kwargs):
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test timeout")
            try:
                yield
            finally:
                exited.set()

        with patch("server.durable_jobs.video_advisory_lock", blocking_lock):
            task = asyncio.create_task(consume_lock().__aenter__())
            await asyncio.to_thread(started.wait, 1)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(exited.wait(timeout=1), "session advisory lock ficou órfão")


if __name__ == "__main__":
    unittest.main()
