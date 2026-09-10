from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from PIL import Image

from pipeline_core.masks import encode_binary_png, inspect_binary_png
from server.routers.review import (
    MaskBatchIn,
    _export_root,
    _export_version,
    _locked_export,
    _segment_paths,
    mask_review_image,
    save_mask_review_batch,
    segment_review,
    segment_frame,
)
from server.video_fence import async_video_fence, video_fence


class ReviewMediaFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()) / "export"
        self.segment = self.root / "seg_00"
        self.segment.mkdir(parents=True)
        (self.segment / "000000.jpg").write_bytes(b"jpeg")
        (self.segment / "prompt.json").write_text(
            json.dumps(
                {
                    "image_width": 4,
                    "image_height": 4,
                    "objects": [
                        {
                            "obj_id": 1,
                            "label": "boom",
                            "box_normalized": [0.0, 0.0, 1.0, 1.0],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        mask = self.segment / "_sam3" / "masks" / "1" / "000000.png"
        mask.parent.mkdir(parents=True)
        mask.write_bytes(encode_binary_png(Image.new("1", (4, 4), 1)))
        (self.root / ".export-owner.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "object_id": "boom",
                    "video_id": "video-1",
                    "relpath": "clip.mp4",
                    "annotation_revision": 3,
                }
            ),
            encoding="utf-8",
        )
        self.ctx = SimpleNamespace(object_id="boom")

    def call_frame(self, version: str | None):
        with patch(
            "server.routers.review._export_root",
            return_value=(self.root, ["seg_00"], SimpleNamespace()),
        ):
            return asyncio.run(
                segment_frame(
                    "video-1",
                    "seg_00",
                    0,
                    version=version,
                    ctx=self.ctx,
                )
            )

    def test_versioned_frame_url_is_immutable_only_for_current_export(self) -> None:
        response = self.call_frame(_export_version(self.root))
        self.assertEqual(
            response.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )

    def test_unversioned_frame_revalidates_and_stale_version_is_rejected(self) -> None:
        response = self.call_frame(None)
        self.assertEqual(
            response.headers["cache-control"],
            "private, max-age=0, must-revalidate",
        )
        with self.assertRaises(HTTPException) as raised:
            self.call_frame("stale")
        self.assertEqual(raised.exception.status_code, 409)

    def test_export_version_changes_when_same_revision_is_republished(self) -> None:
        first = _export_version(self.root)
        marker = self.root / ".export-owner.json"
        replacement = self.root / ".owner.tmp"
        replacement.write_bytes(marker.read_bytes())
        replacement.replace(marker)
        second = _export_version(self.root)

        self.assertNotEqual(first, second)

    def test_generation_switch_invalidates_batch_before_manifest_or_database_write(self) -> None:
        control = self.root / "_sam3"
        control.mkdir()

        def publish_pointer(generation_id: str, manifest_sha256: str) -> None:
            replacement = control / ".current.tmp"
            replacement.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "generation_id": generation_id,
                        "annotation_revision": 3,
                        "model": {
                            "model_id": "model-1",
                            "checkpoint_sha256": "a" * 64,
                            "sam3_commit": "b" * 40,
                        },
                        "manifest_sha256": manifest_sha256,
                        "published_at": "2026-09-09T00:00:00Z",
                        "segments": {
                            "seg_00": {
                                "path": f"runs/{generation_id}/seg_00",
                                "prompt_digest": "d" * 64,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            replacement.replace(control / "current.json")

        publish_pointer("generation-a", "1" * 64)
        version_seen_by_reviewer = _export_version(self.root)
        legacy_manifest = self.segment / "_sam3" / "mask_review.json"
        legacy_manifest.write_text('{"sentinel": true}', encoding="utf-8")
        before = legacy_manifest.read_bytes()
        publish_pointer("generation-b", "2" * 64)
        payload = MaskBatchIn.model_validate(
            {
                "export_version": version_seen_by_reviewer,
                "frames": [
                    {
                        "frame": 0,
                        "expected_revision": 0,
                        "status": "ok",
                    }
                ],
            }
        )

        with patch(
            "server.routers.review._export_root",
            return_value=(self.root, ["seg_00"], SimpleNamespace(relpath="clip.mp4")),
        ), patch("server.routers.review._require_lock"), patch(
            "server.routers.review._mask_store"
        ) as mask_store, patch(
            "server.routers.review.index_revisions"
        ) as index:
            with self.assertRaises(HTTPException) as raised:
                save_mask_review_batch(
                    "video-1",
                    "seg_00",
                    payload,
                    ctx=self.ctx,
                    user=SimpleNamespace(user_id="guilherme"),
                    client_id="client-1",
                )

        self.assertEqual(raised.exception.status_code, 409)
        mask_store.assert_not_called()
        index.assert_not_called()
        self.assertEqual(legacy_manifest.read_bytes(), before)

    def test_review_refuses_generation_from_an_older_annotation_revision(self) -> None:
        control = self.root / "_sam3"
        control.mkdir()
        (control / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "generation-old",
                    "annotation_revision": 3,
                    "segments": {
                        "seg_00": {
                            "path": "runs/generation-old/seg_00",
                            "prompt_digest": "d" * 64,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        ctx = SimpleNamespace(
            output_root=self.root.parent,
            index=SimpleNamespace(
                get=lambda _video_id: SimpleNamespace(relpath="clip.mp4")
            ),
            store=SimpleNamespace(
                entry=lambda _relpath: {
                    "annotation_revision": 4,
                    "export": {
                        "root": str(self.root),
                        "segments": ["seg_00"],
                    },
                }
            ),
        )

        with self.assertRaises(HTTPException) as raised:
            _export_root(ctx, "video-1")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("geracao SAM3", str(raised.exception.detail))

    def test_review_rejects_an_existing_export_root_outside_object_output(self) -> None:
        outside = Path(tempfile.mkdtemp()) / "foreign-export"
        outside.mkdir()
        ctx = SimpleNamespace(
            output_root=self.root.parent,
            index=SimpleNamespace(
                get=lambda _video_id: SimpleNamespace(relpath="clip.mp4")
            ),
            store=SimpleNamespace(
                entry=lambda _relpath: {
                    "annotation_revision": 3,
                    "export": {
                        "root": str(outside),
                        "segments": ["seg_00"],
                    },
                }
            ),
        )

        with self.assertRaises(HTTPException) as raised:
            _export_root(ctx, "video-1")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("fora", str(raised.exception.detail))

    def test_segment_path_rejects_traversal_even_when_declared_in_metadata(self) -> None:
        outside = self.root.parent / "outside"
        outside.mkdir()

        with self.assertRaises(HTTPException) as raised:
            _segment_paths(self.root, ["../outside"], "../outside")

        self.assertEqual(raised.exception.status_code, 409)

    def test_segment_review_snapshots_state_and_version_under_one_fence(self) -> None:
        events: list[str] = []

        @asynccontextmanager
        async def fence(_object_id: str, _video_id: str):
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        def assert_fenced(value):
            self.assertEqual(events, ["enter"])
            return value

        with patch(
            "server.routers.review.async_video_fence", side_effect=fence
        ), patch(
            "server.routers.review._export_root",
            side_effect=lambda *_: assert_fenced(
                (
                    self.root,
                    ["seg_00"],
                    SimpleNamespace(name="clip", relpath="clip.mp4"),
                )
            ),
        ), patch(
            "server.routers.review.review_module.class_names", return_value=[]
        ), patch(
            "server.routers.review.review_module.segment_state",
            side_effect=lambda *_: assert_fenced({"frames": []}),
        ), patch(
            "server.routers.review.review_module.prompt_contract",
            side_effect=lambda *_: assert_fenced({"objects": []}),
        ), patch(
            "server.routers.review._export_version",
            side_effect=lambda *_: assert_fenced("version-a"),
        ):
            response = asyncio.run(
                segment_review(
                    "video-1",
                    "seg_00",
                    ctx=SimpleNamespace(object_id="boom", label="Boom"),
                )
            )

        self.assertEqual(events, ["enter", "exit"])
        self.assertEqual(response["export_version"], "version-a")

    def call_mask(self, revision: int | None, sha256: str | None):
        with patch(
            "server.routers.review._export_root",
            return_value=(self.root, ["seg_00"], SimpleNamespace()),
        ):
            return asyncio.run(
                mask_review_image(
                    "video-1",
                    "seg_00",
                    0,
                    1,
                    revision=revision,
                    sha256=sha256,
                    ctx=self.ctx,
                )
            )

    def test_mask_is_immutable_only_when_revision_and_digest_match(self) -> None:
        path = self.segment / "_sam3" / "masks" / "1" / "000000.png"
        digest = inspect_binary_png(path.read_bytes(), expected_size=(4, 4)).sha256
        response = self.call_mask(0, digest)
        self.assertEqual(
            response.headers["cache-control"],
            "private, max-age=31536000, immutable",
        )
        self.assertEqual(
            self.call_mask(None, None).headers["cache-control"],
            "private, max-age=0, must-revalidate",
        )
        with self.assertRaises(HTTPException) as raised:
            self.call_mask(0, "0" * 64)
        self.assertEqual(raised.exception.status_code, 409)

    def test_batch_requires_the_export_version_seen_by_the_reviewer(self) -> None:
        with self.assertRaises(ValidationError):
            MaskBatchIn.model_validate(
                {
                    "frames": [
                        {
                            "frame": 0,
                            "expected_revision": 0,
                            "status": "ok",
                        }
                    ]
                }
            )

    def test_export_version_is_rechecked_inside_shared_publication_lock(self) -> None:
        events: list[str] = []

        @contextmanager
        def shared_lock(_object_id: str, _video_id: str):
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        def resolve(_ctx, _video_id):
            self.assertEqual(events, ["enter"])
            return self.root, ["seg_00"], SimpleNamespace(relpath="clip.mp4")

        with patch("server.video_fence.durable_jobs.enabled", return_value=True), patch(
            "server.video_fence.durable_jobs.video_advisory_lock",
            side_effect=shared_lock,
        ), patch("server.routers.review._export_root", side_effect=resolve), patch(
            "server.routers.review._require_lock"
        ):
            with self.assertRaises(HTTPException) as raised:
                with _locked_export(self.ctx, "video-1", "client-1", "stale"):
                    self.fail("stale export must not enter commit body")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(events, ["enter", "exit"])


class LocalVideoFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_async_waiter_does_not_leak_the_local_fence(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with video_fence("boom", "video-cancel"):
                entered.set()
                release.wait(timeout=2)

        async def waiter() -> None:
            async with async_video_fence("boom", "video-cancel"):
                self.fail("cancelled waiter must not enter its body")

        with patch("server.durable_jobs.enabled", return_value=False):
            holding = asyncio.create_task(asyncio.to_thread(holder))
            await asyncio.to_thread(entered.wait, 1)
            waiting = asyncio.create_task(waiter())
            await asyncio.sleep(0.03)
            waiting.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, timeout=1)
            await asyncio.wait_for(holding, timeout=1)
            async with asyncio.timeout(1):
                async with async_video_fence("boom", "video-cancel"):
                    pass

    async def test_async_publisher_and_sync_review_share_the_same_local_fence(self) -> None:
        entered = threading.Event()

        def review_commit() -> None:
            with video_fence("boom", "video-1"):
                entered.set()

        with patch("server.durable_jobs.enabled", return_value=False):
            async with async_video_fence("boom", "video-1"):
                task = asyncio.create_task(asyncio.to_thread(review_commit))
                await asyncio.sleep(0.03)
                self.assertFalse(entered.is_set())
            await asyncio.wait_for(task, timeout=1)

        self.assertTrue(entered.is_set())


if __name__ == "__main__":
    unittest.main()
