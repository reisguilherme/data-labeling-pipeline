from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image


class PipelineStateTests(unittest.TestCase):
    def test_only_fully_reviewed_valid_sam3_video_is_completed(self) -> None:
        from server.pipeline_state import classify_pipeline

        completed = classify_pipeline("done", "done", 7, 7, True)
        incomplete = classify_pipeline("done", "done", 7, 6, True)

        self.assertEqual((completed.stage, completed.status), ("completed", "validated"))
        self.assertEqual((incomplete.stage, incomplete.status), ("review", "in_progress"))

    def test_manual_status_and_active_sam3_take_precedence(self) -> None:
        from server.pipeline_state import classify_pipeline

        cases = [
            (("pending", None, 0, 0, False), ("triage", "pending")),
            (("in_progress", None, 0, 0, False), ("triage", "in_progress")),
            (("no_boom", "done", 4, 4, True), ("discarded", "discarded")),
            (("done", None, 0, 0, False), ("sam3", "ready")),
            (("done", "running", 7, 0, True), ("sam3", "running")),
            (("done", "error", 7, 7, True), ("sam3", "error")),
            (("done", "done", 7, 0, True), ("review", "waiting")),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                snapshot = classify_pipeline(*args)
                self.assertEqual((snapshot.stage, snapshot.status), expected)

    def test_filesystem_inspector_counts_mask_reviews_and_validates_artifacts(self) -> None:
        from server.pipeline_state import inspect_pipeline_entry

        root = Path(tempfile.mkdtemp())
        segment = root / "video" / "seg_00"
        out = segment / "_sam3"
        masks = out / "masks" / "1"
        masks.mkdir(parents=True)
        (segment / "prompt.json").write_text(
            json.dumps(
                {
                    "image_width": 8,
                    "image_height": 6,
                    "objects": [{"obj_id": 1, "label": "boom", "box_normalized": [0, 0, 1, 1]}],
                }
            ),
            encoding="utf-8",
        )
        (out / "run.json").write_text(
            json.dumps({"status": "done", "frame_count": 2, "frames_written": 2}),
            encoding="utf-8",
        )
        for frame in range(2):
            image = Image.new("1", (8, 6), 0)
            image.putpixel((frame + 1, 2), 1)
            image.save(masks / f"{frame:06d}.png", format="PNG")
        (out / "mask_review.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "frames": {
                        "0": {"revision": 1, "status": "ok", "by": "gui"},
                        "1": {"revision": 1, "status": "edited", "by": "gui", "instances": []},
                    },
                }
            ),
            encoding="utf-8",
        )
        entry = {
            "status": "done",
            "export": {"root": str(root / "video"), "segments": ["seg_00"]},
        }

        snapshot = inspect_pipeline_entry(entry, sam3={"state": "done"}, output_root=root)

        self.assertEqual(snapshot.stage, "completed")
        self.assertEqual(snapshot.expected_frames, 2)
        self.assertEqual(snapshot.reviewed_frames, 2)
        self.assertEqual(snapshot.edited_frames, 1)
        self.assertTrue(snapshot.artifacts_valid)

    def test_missing_raw_mask_keeps_video_in_sam3(self) -> None:
        from server.pipeline_state import inspect_pipeline_entry

        root = Path(tempfile.mkdtemp())
        segment = root / "video" / "seg_00"
        out = segment / "_sam3"
        out.mkdir(parents=True)
        (segment / "prompt.json").write_text(
            json.dumps(
                {
                    "image_width": 8,
                    "image_height": 6,
                    "objects": [{"obj_id": 1, "label": "boom", "box_normalized": [0, 0, 1, 1]}],
                }
            ),
            encoding="utf-8",
        )
        (out / "run.json").write_text(
            json.dumps({"status": "done", "frame_count": 1, "frames_written": 1}),
            encoding="utf-8",
        )
        entry = {
            "status": "done",
            "export": {"root": str(root / "video"), "segments": ["seg_00"]},
        }

        snapshot = inspect_pipeline_entry(entry, sam3={"state": "done"}, output_root=root)

        self.assertEqual((snapshot.stage, snapshot.status), ("sam3", "invalid"))
        self.assertFalse(snapshot.artifacts_valid)

    def test_unprocessed_segment_without_a_sam3_run_is_ready_not_invalid(self) -> None:
        """Catches treating a first SAM3 execution as a corrupt artifact."""
        from server.pipeline_state import inspect_pipeline_entry

        root = Path(tempfile.mkdtemp())
        segment = root / "video" / "seg_00"
        segment.mkdir(parents=True)
        entry = {
            "status": "done",
            "export": {"root": str(root / "video"), "segments": ["seg_00"]},
        }

        snapshot = inspect_pipeline_entry(entry, sam3=None, output_root=root)

        self.assertEqual((snapshot.stage, snapshot.status), ("sam3", "ready"))
        self.assertEqual(snapshot.inconsistencies, ())


if __name__ == "__main__":
    unittest.main()
