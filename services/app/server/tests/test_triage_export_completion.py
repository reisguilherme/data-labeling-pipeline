from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server import export as export_module
from server.sam3 import Sam3Queue
from server.store import AnnotationStore
from server.videos import VideoFile

try:
    from server.video_export_completion import (
        ExportCompletionConflict,
        finalize_video_export,
    )
except ModuleNotFoundError:
    ExportCompletionConflict = RuntimeError
    finalize_video_export = None


class _Index:
    def __init__(self, root: Path) -> None:
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

    def get(self, video_id: str):
        return self.video if video_id == self.video.video_id else None


def _context(base: Path):
    output = base / "dataset"
    store = AnnotationStore(
        annotations_path=output / "annotations.json",
        cache_dir=output / "_cache",
        videos_root=base,
        output_root=output,
        object_id="boom",
        label="boom",
        total_provider=lambda: 1,
    )
    store.load()
    return SimpleNamespace(
        object_id="boom",
        label="boom",
        output_root=output,
        cache_dir=output / "_cache",
        store=store,
        index=_Index(base),
        config=SimpleNamespace(auto_sam3=True),
    )


def _entry(revision: int = 4) -> dict:
    return {
        "status": "in_progress",
        "annotation_revision": revision,
        "media": {"width": 640, "height": 360},
        "intervals": [
            {
                "segment": "seg_00",
                "index": 0,
                "frame_count": 2,
                "start_frame": 10,
                "end_frame": 11,
                "prompt_frame": 10,
                "bboxes": [],
                "flags": {},
            }
        ],
    }


async def _prepared(base: Path, revision: int = 4):
    ctx = _context(base)
    await ctx.store.put_entry("clip.mp4", _entry(revision))
    video = ctx.index.video
    root = export_module.export_root_for(ctx, video)
    segment = root / "seg_00"
    segment.mkdir(parents=True)
    (segment / "prompt.json").write_text("{}", encoding="utf-8")
    owner = export_module.export_owner(ctx, video, revision)
    export_module.write_export_owner(root, owner)
    result = {
        "root": root.as_posix(),
        "segments": ["seg_00"],
        "total_frames": 2,
        "jpeg_qscale": 2,
        "frame_naming": "restart_per_segment",
        "ffmpeg_version": "ffmpeg-test",
        "annotation_revision": revision,
        "owner": owner,
    }
    return ctx, root, result


class ExportCompletionContractTests(unittest.TestCase):
    def test_server_owned_finalizer_exists(self) -> None:
        self.assertTrue(
            callable(finalize_video_export),
            "server.video_export_completion.finalize_video_export ausente",
        )


@unittest.skipUnless(callable(finalize_video_export), "finalizador ainda nao implementado")
class VideoExportCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_completion_without_browser_promotes_and_enqueues_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, _, result = await _prepared(Path(temporary))
            queue = Sam3Queue()

            with patch("server.video_export_completion.sam3_queue", queue):
                first = await finalize_video_export(
                    ctx,
                    "video-1",
                    result,
                    "guilherme",
                    "job-1",
                    expected_revision=4,
                    expected_root_basename="clip__video-1",
                    expected_owner=result["owner"],
                    expected_total=2,
                )
                second = await finalize_video_export(
                    ctx,
                    "video-1",
                    copy.deepcopy(result),
                    "guilherme",
                    "job-1",
                    expected_revision=4,
                    expected_root_basename="clip__video-1",
                    expected_owner=result["owner"],
                    expected_total=2,
                )

            entry = ctx.store.entry("clip.mp4")
            self.assertEqual(entry["status"], "done")
            self.assertEqual(entry["export"], result)
            self.assertEqual(entry["exported_by"], "guilherme")
            self.assertEqual(first.digest, second.digest)
            self.assertFalse(first.replayed)
            self.assertTrue(second.replayed)
            queued = queue.get("boom", "clip.mp4")
            self.assertIsNotNone(queued)
            self.assertEqual(queued.annotation_revision, 4)
            self.assertEqual(queued.segments, ["seg_00"])

    async def test_same_job_with_different_valid_result_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, _, result = await _prepared(Path(temporary))
            queue = Sam3Queue()
            with patch("server.video_export_completion.sam3_queue", queue):
                await finalize_video_export(ctx, "video-1", result, "ana", "job-1")
                changed = {**result, "ffmpeg_version": "another-build"}
                with self.assertRaises(ExportCompletionConflict):
                    await finalize_video_export(ctx, "video-1", changed, "ana", "job-1")

    async def test_another_job_cannot_replace_a_completed_revision_with_divergent_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, _, result = await _prepared(Path(temporary))
            queue = Sam3Queue()
            with patch("server.video_export_completion.sam3_queue", queue):
                await finalize_video_export(ctx, "video-1", result, "ana", "job-1")
                changed = {**result, "ffmpeg_version": "unexpected-other-build"}
                with self.assertRaises(ExportCompletionConflict):
                    await finalize_video_export(
                        ctx, "video-1", changed, "ana", "job-2"
                    )

            entry = ctx.store.entry("clip.mp4")
            self.assertEqual(entry["export"], result)
            self.assertEqual(entry["export_completion"]["job_id"], "job-1")

    async def test_completed_revision_cannot_be_claimed_by_another_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, _, result = await _prepared(Path(temporary))
            queue = Sam3Queue()
            with patch("server.video_export_completion.sam3_queue", queue):
                first = await finalize_video_export(
                    ctx, "video-1", result, "ana", "job-1"
                )
                with self.assertRaises(ExportCompletionConflict):
                    await finalize_video_export(
                        ctx, "video-1", copy.deepcopy(result), "ana", "job-2"
                    )

            self.assertEqual(
                ctx.store.entry("clip.mp4")["export_completion"]["digest"],
                first.digest,
            )

    async def test_replay_repairs_enqueue_failure_after_annotation_was_persisted(self) -> None:
        class FailOnceQueue:
            def __init__(self, delegate: Sam3Queue) -> None:
                self.delegate = delegate
                self.calls = 0

            def bind(self, object_id, output_root):
                return self.delegate.bind(object_id, output_root)

            def enqueue(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("queue unavailable")
                return self.delegate.enqueue(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            ctx, _, result = await _prepared(Path(temporary))
            queue = Sam3Queue()
            flaky = FailOnceQueue(queue)
            with patch("server.video_export_completion.sam3_queue", flaky):
                with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
                    await finalize_video_export(ctx, "video-1", result, "ana", "job-1")
                self.assertEqual(ctx.store.entry("clip.mp4")["status"], "done")
                replay = await finalize_video_export(
                    ctx, "video-1", result, "ana", "job-1"
                )

            self.assertTrue(replay.replayed)
            self.assertEqual(flaky.calls, 2)
            self.assertIsNotNone(queue.get("boom", "clip.mp4"))

    async def test_invalid_result_contract_never_mutates_annotation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, root, result = await _prepared(Path(temporary))
            invalid = {
                "revision": {**result, "annotation_revision": 3},
                "root": {**result, "root": (root.parent / "other").as_posix()},
                "owner": {
                    **result,
                    "owner": {**result["owner"], "video_id": "video-2"},
                },
                "segments": {**result, "segments": ["seg_99"]},
                "total": {**result, "total_frames": 1},
            }
            queue = Sam3Queue()
            with patch("server.video_export_completion.sam3_queue", queue):
                for label, candidate in invalid.items():
                    with self.subTest(label=label), self.assertRaises(
                        ExportCompletionConflict
                    ):
                        await finalize_video_export(
                            ctx,
                            "video-1",
                            candidate,
                            "ana",
                            f"job-{label}",
                            expected_revision=4,
                            expected_root_basename="clip__video-1",
                            expected_owner=result["owner"],
                            expected_total=2,
                        )

            self.assertEqual(ctx.store.entry("clip.mp4")["status"], "in_progress")
            self.assertIsNone(queue.get("boom", "clip.mp4"))


if __name__ == "__main__":
    unittest.main()
