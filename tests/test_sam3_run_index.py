from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from server import sam3_run_index
from server.sam3_run_index import _effective_prompt
from pipeline_core.masks import encode_binary_png, inspect_binary_png


class Sam3RunIndexTests(unittest.TestCase):
    @staticmethod
    def _publish_pointer(export_root: Path, generation_id: str = "generation-1") -> Path:
        output = export_root / "_sam3" / "runs" / generation_id / "seg_00"
        output.mkdir(parents=True)
        control = export_root / "_sam3"
        (control / "current.json").write_text(
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
                    "manifest_sha256": "c" * 64,
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
        return output

    def test_effective_prompt_digest_includes_override_and_is_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            segment = Path(temporary)
            raw = json.dumps({"objects": [{"obj_id": 1, "label": "boom"}]}).encode()
            override = json.dumps(
                {"objects": [{"obj_id": 1, "label": "boom", "box_normalized": [0, 0, 1, 1]}]}
            ).encode()
            (segment / "prompt.json").write_bytes(raw)
            (segment / "_sam3").mkdir()
            (segment / "_sam3" / "prompt_override.json").write_bytes(override)

            prompt, digest = _effective_prompt(segment)

            self.assertEqual(prompt["objects"][0]["box_normalized"], [0, 0, 1, 1])
            self.assertEqual(digest, hashlib.sha256(raw + override).hexdigest())
            self.assertEqual(len(digest), 64)

    def test_existing_index_is_rejected_when_an_artifact_checksum_differs(self) -> None:
        validate = getattr(sam3_run_index, "_validate_existing_instances", None)
        self.assertTrue(callable(validate), "validador do indice SAM3 ausente")
        expected = {
            (0, 1): {
                "is_empty": False,
                "sha256": "a" * 64,
                "object_key": "boom/video/_sam3/runs/run-1/seg_00/masks/1/000000.png",
                "area_pixels": 12,
            }
        }
        indexed = [
            (
                0,
                1,
                False,
                "b" * 64,
                expected[(0, 1)]["object_key"],
                12,
            )
        ]

        with self.assertRaisesRegex(ValueError, "checksum"):
            validate(expected, indexed)

    def test_published_raw_artifact_uses_content_addressed_object_key(self) -> None:
        workspace = Path(tempfile.mkdtemp())
        export_root = workspace / "boom" / "dataset" / "clip"
        segment = export_root / "seg_00"
        output = export_root / "_sam3" / "runs" / "generation-1" / "seg_00"
        segment.mkdir(parents=True)
        mask = output / "masks" / "1" / "000000.png"
        mask.parent.mkdir(parents=True)
        mask.write_bytes(encode_binary_png(Image.new("1", (4, 4), 1)))
        info = inspect_binary_png(mask.read_bytes(), expected_size=(4, 4))
        prompt = {
            "segment": "seg_00",
            "source_start_frame": 10,
            "source_end_frame": 10,
            "frame_count": 1,
            "prompt_frame_idx": 0,
            "image_width": 4,
            "image_height": 4,
            "objects": [{"obj_id": 1, "label": "boom"}],
        }
        (segment / "prompt.json").write_text(json.dumps(prompt), encoding="utf-8")
        _, digest = _effective_prompt(segment)
        model = {
            "model_id": "model-1",
            "checkpoint_sha256": "a" * 64,
            "sam3_commit": "b" * 40,
        }
        result = {
            "run_id": "generation-1",
            "annotation_revision": 3,
            "model": model,
            "publication": {
                "generation_id": "generation-1",
                "annotation_revision": 3,
                "manifest_sha256": "c" * 64,
            },
            "segment_runs": [
                {
                    "segment": "seg_00",
                    "status": "done",
                    "prompt_digest": digest,
                    "frames_written": 1,
                    "params": {"threshold": 0.5},
                    "model": model,
                    "objects": [{"obj_id": 1, "class_index": 0}],
                    "artifacts": {
                        "format": "png-1bit-v1",
                        "files": 1,
                        "empty": 0,
                        "checksums": {"masks/1/000000.png": info.sha256},
                    },
                }
            ],
        }
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [
            ("project-1",),
            ("video-1",),
            ("interval-1",),
            ("prompt-1",),
            ("model-db-1",),
            ("run-db-1",),
            ("class-1",),
            ("artifact-1",),
        ]
        connection = MagicMock()
        connection.cursor.return_value = cursor

        with patch.dict("os.environ", {"MST_WORKSPACE": str(workspace)}):
            indexed = sam3_run_index.index_completed_runs(
                connection,
                object_id="boom",
                relpath="clip.mp4",
                result=result,
                export_root=export_root,
            )

        self.assertEqual(indexed, 1)
        artifact_insert = next(
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO artifacts" in call.args[0]
        )
        self.assertEqual(
            artifact_insert.args[1][1],
            f"sha256/{info.sha256[:2]}/{info.sha256}.png",
        )

    def test_revision_index_targets_the_exact_active_generation_and_its_mask(self) -> None:
        workspace = Path(tempfile.mkdtemp())
        export_root = workspace / "boom" / "dataset" / "clip"
        segment = export_root / "seg_00"
        segment.mkdir(parents=True)
        (segment / "prompt.json").write_text(
            json.dumps(
                {
                    "segment": "seg_00",
                    "source_start_frame": 10,
                    "source_end_frame": 10,
                    "frame_count": 1,
                    "prompt_frame_idx": 0,
                    "image_width": 4,
                    "image_height": 4,
                    "objects": [{"obj_id": 1, "label": "boom"}],
                }
            ),
            encoding="utf-8",
        )
        output = self._publish_pointer(export_root)
        pointer_path = export_root / "_sam3" / "current.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        prompt_digest = hashlib.sha256(
            (segment / "prompt.json").read_bytes()
        ).hexdigest()
        pointer["segments"]["seg_00"]["prompt_digest"] = prompt_digest
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
        review_mask = output / "reviews" / "000000" / "rev_000001" / "1.png"
        review_mask.parent.mkdir(parents=True)
        review_mask.write_bytes(encode_binary_png(Image.new("1", (4, 4), 1)))
        entry = {
            "revision": 1,
            "status": "edited",
            "instances": [
                {
                    "obj_id": 1,
                    "label": "boom",
                    "path": "reviews/000000/rev_000001/1.png",
                }
            ],
            "deleted_obj_ids": [],
        }
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [
            ("run-db-1", "project-1"),
            None,
            ("revision-1",),
            ("class-1",),
            None,
            ("artifact-1",),
        ]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor

        with patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://test", "MST_WORKSPACE": str(workspace)},
        ), patch("psycopg.connect", return_value=connection):
            sam3_run_index.index_revisions(
                object_id="boom",
                relpath="clip.mp4",
                segment_dir=segment,
                revisions=[(0, entry)],
                user="guilherme",
            )

        run_lookup = cursor.execute.call_args_list[0]
        self.assertIn("parameters", run_lookup.args[0])
        self.assertIn("generation-1", run_lookup.args[1])
        self.assertIn("c" * 64, run_lookup.args[1])
        artifact_insert = next(
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO artifacts" in call.args[0]
        )
        self.assertIn("_sam3/runs/generation-1/seg_00/reviews/", artifact_insert.args[1][1])


if __name__ == "__main__":
    unittest.main()
