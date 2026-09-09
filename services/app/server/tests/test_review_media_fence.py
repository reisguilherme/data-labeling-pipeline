from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from PIL import Image

from pipeline_core.masks import encode_binary_png, inspect_binary_png
from server.routers.review import (
    MaskBatchIn,
    _export_version,
    _locked_export,
    mask_review_image,
    segment_frame,
)


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

        with patch("server.routers.review.durable_jobs.enabled", return_value=True), patch(
            "server.routers.review.durable_jobs.video_advisory_lock",
            side_effect=shared_lock,
        ), patch("server.routers.review._export_root", side_effect=resolve), patch(
            "server.routers.review._require_lock"
        ):
            with self.assertRaises(HTTPException) as raised:
                with _locked_export(self.ctx, "video-1", "client-1", "stale"):
                    self.fail("stale export must not enter commit body")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(events, ["enter", "exit"])


if __name__ == "__main__":
    unittest.main()
