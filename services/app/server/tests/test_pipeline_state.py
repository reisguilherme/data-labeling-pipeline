from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def test_filesystem_inspector_uses_manifest_without_reading_mask_bytes(self) -> None:
        from server.pipeline_state import inspect_pipeline_entry

        root = Path(tempfile.mkdtemp())
        segment = root / "video" / "seg_00"
        out = segment / "_sam3"
        out.mkdir(parents=True)
        (segment / "prompt.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "frame_count": 2,
                    "image_width": 8,
                    "image_height": 6,
                    "objects": [{"obj_id": 1, "label": "boom", "box_normalized": [0, 0, 1, 1]}],
                }
            ),
            encoding="utf-8",
        )
        (out / "run.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "done",
                    "frame_count": 2,
                    "frames_written": 2,
                    "objects": [{"obj_id": 1}],
                    "artifacts": {
                        "format": "png-1bit-v1",
                        "files": 2,
                        "checksums": {
                            "masks/1/000000.png": "a" * 64,
                            "masks/1/000001.png": "b" * 64,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
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

        with patch.object(
            Path, "read_bytes", side_effect=AssertionError("PNG read on fast path")
        ):
            snapshot = inspect_pipeline_entry(
                entry, sam3={"state": "done"}, output_root=root
            )

        self.assertEqual((snapshot.stage, snapshot.status), ("completed", "validated"))
        self.assertEqual(snapshot.validation_status, "manifest")
        self.assertEqual(snapshot.public_progress()["validation_status"], "manifest")
        self.assertEqual(snapshot.expected_frames, 2)
        self.assertEqual(snapshot.reviewed_frames, 2)
        self.assertEqual(snapshot.edited_frames, 1)
        self.assertTrue(snapshot.artifacts_valid)

    def test_missing_raw_mask_keeps_video_in_sam3(self) -> None:
        from server.pipeline_state import audit_pipeline_entry

        root = Path(tempfile.mkdtemp())
        segment = root / "video" / "seg_00"
        out = segment / "_sam3"
        out.mkdir(parents=True)
        (segment / "prompt.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "frame_count": 1,
                    "image_width": 8,
                    "image_height": 6,
                    "objects": [{"obj_id": 1, "label": "boom", "box_normalized": [0, 0, 1, 1]}],
                }
            ),
            encoding="utf-8",
        )
        (out / "run.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "done",
                    "frame_count": 1,
                    "frames_written": 1,
                    "objects": [{"obj_id": 1}],
                    "artifacts": {
                        "format": "png-1bit-v1",
                        "files": 1,
                        "checksums": {"masks/1/000000.png": "a" * 64},
                    },
                }
            ),
            encoding="utf-8",
        )
        entry = {
            "status": "done",
            "export": {"root": str(root / "video"), "segments": ["seg_00"]},
        }

        snapshot = audit_pipeline_entry(entry, sam3={"state": "done"}, output_root=root)

        self.assertEqual((snapshot.stage, snapshot.status), ("sam3", "invalid"))
        self.assertEqual(snapshot.validation_status, "invalid")
        self.assertFalse(snapshot.artifacts_valid)

    def test_legacy_run_without_artifact_manifest_requires_audit(self) -> None:
        from server.pipeline_state import inspect_pipeline_entry

        root = Path(tempfile.mkdtemp())
        segment = root / "video" / "seg_00"
        out = segment / "_sam3"
        out.mkdir(parents=True)
        (segment / "prompt.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "frame_count": 1,
                    "image_width": 8,
                    "image_height": 6,
                    "objects": [
                        {
                            "obj_id": 1,
                            "label": "boom",
                            "box_normalized": [0, 0, 1, 1],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (out / "run.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "done",
                    "frame_count": 1,
                    "frames_written": 1,
                    "objects": [{"obj_id": 1}],
                }
            ),
            encoding="utf-8",
        )
        (out / "mask_review.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "frames": {"0": {"revision": 1, "status": "ok", "by": "gui"}},
                }
            ),
            encoding="utf-8",
        )
        entry = {
            "status": "done",
            "export": {"root": str(root / "video"), "segments": ["seg_00"]},
        }

        snapshot = inspect_pipeline_entry(entry, sam3={"state": "done"}, output_root=root)

        self.assertEqual((snapshot.stage, snapshot.status), ("review", "audit_required"))
        self.assertEqual(snapshot.validation_status, "audit_required")
        self.assertFalse(snapshot.artifacts_valid)

    def test_manifest_metadata_rejects_structural_contradictions(self) -> None:
        from server.pipeline_state import inspect_pipeline_entry

        prompt = {
            "schema_version": 1,
            "frame_count": 2,
            "image_width": 8,
            "image_height": 6,
            "objects": [{"obj_id": 1, "label": "boom"}],
        }
        run = {
            "schema_version": 1,
            "status": "done",
            "frame_count": 2,
            "frames_written": 2,
            "objects": [{"obj_id": 1}],
            "artifacts": {
                "format": "png-1bit-v1",
                "files": 2,
                "checksums": {
                    "masks/1/000000.png": "a" * 64,
                    "masks/1/000001.png": "b" * 64,
                },
            },
        }
        mutations = {
            "unknown run schema": lambda p, r: r.__setitem__("schema_version", 2),
            "boolean run schema": lambda p, r: r.__setitem__("schema_version", True),
            "unknown prompt schema": lambda p, r: p.__setitem__("schema_version", 2),
            "non-positive frame count": lambda p, r: r.__setitem__("frame_count", 0),
            "non-positive dimensions": lambda p, r: p.__setitem__("image_width", 0),
            "frames written mismatch": lambda p, r: r.__setitem__("frames_written", 1),
            "prompt frame count mismatch": lambda p, r: p.__setitem__("frame_count", 1),
            "duplicate prompt object ids": lambda p, r: p["objects"].append(
                {"obj_id": 1, "label": "duplicate"}
            ),
            "run object ids mismatch": lambda p, r: r.__setitem__(
                "objects", [{"obj_id": 2}]
            ),
            "unknown artifact format": lambda p, r: r["artifacts"].__setitem__(
                "format", "png-v1"
            ),
            "artifact count mismatch": lambda p, r: r["artifacts"].__setitem__(
                "files", 1
            ),
            "artifact key mismatch": lambda p, r: r["artifacts"].__setitem__(
                "checksums", {"masks/1/wrong.png": "a" * 64, "other": "b" * 64}
            ),
            "non-lowercase checksum": lambda p, r: r["artifacts"][
                "checksums"
            ].__setitem__("masks/1/000000.png", "A" * 64),
        }

        for name, mutate in mutations.items():
            with self.subTest(name=name):
                case_prompt = copy.deepcopy(prompt)
                case_run = copy.deepcopy(run)
                mutate(case_prompt, case_run)
                root = Path(tempfile.mkdtemp())
                segment = root / "video" / "seg_00"
                out = segment / "_sam3"
                out.mkdir(parents=True)
                (segment / "prompt.json").write_text(
                    json.dumps(case_prompt), encoding="utf-8"
                )
                (out / "run.json").write_text(
                    json.dumps(case_run), encoding="utf-8"
                )
                entry = {
                    "status": "done",
                    "export": {
                        "root": str(root / "video"),
                        "segments": ["seg_00"],
                    },
                }

                snapshot = inspect_pipeline_entry(
                    entry, sam3={"state": "done"}, output_root=root
                )

                self.assertEqual((snapshot.stage, snapshot.status), ("sam3", "invalid"))
                self.assertEqual(snapshot.validation_status, "invalid")
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
