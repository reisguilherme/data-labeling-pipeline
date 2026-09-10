from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "pipeline-core" / "src"))

from pipeline_core.masks import encode_binary_png
from pipeline_core.review_store import (
    FileMaskReviewStore,
    MaskEdit,
    RevisionConflict,
)


def mask_at(size: tuple[int, int], x: int, y: int) -> bytes:
    image = Image.new("1", size, 0)
    image.putpixel((x, y), 1)
    return encode_binary_png(image)


class FileMaskReviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.out_dir = Path(tempfile.mkdtemp()) / "_sam3"
        raw_path = self.out_dir / "masks" / "1" / "000000.png"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_bytes(mask_at((10, 10), 1, 1))
        self.raw_path = raw_path
        self.store = FileMaskReviewStore(
            self.out_dir,
            image_size=(10, 10),
            labels={1: "boom"},
        )

    def test_edited_revision_never_overwrites_raw_mask(self) -> None:
        raw_before = self.raw_path.read_bytes()

        state = self.store.save_frame(
            0,
            expected_revision=0,
            status="edited",
            instances=[MaskEdit(obj_id=1, label="boom", png=mask_at((10, 10), 7, 6))],
            user="guilherme",
        )

        self.assertEqual(state.revision, 1)
        self.assertEqual(state.status, "edited")
        self.assertEqual(state.instances[0].info.bbox_pixels, (7, 6, 8, 7))
        self.assertEqual(self.raw_path.read_bytes(), raw_before)
        self.assertNotEqual(state.instances[0].path, self.raw_path)

    def test_stale_expected_revision_is_rejected(self) -> None:
        self.store.save_frame(
            0,
            expected_revision=0,
            status="ok",
            instances=[],
            user="guilherme",
        )

        with self.assertRaises(RevisionConflict):
            self.store.save_frame(
                0,
                expected_revision=0,
                status="ok",
                instances=[],
                user="ana",
            )

    def test_ok_revision_keeps_raw_mask_as_effective_annotation(self) -> None:
        state = self.store.save_frame(
            0,
            expected_revision=0,
            status="ok",
            instances=[],
            user="guilherme",
        )

        self.assertEqual(state.instances[0].path, self.raw_path)
        self.assertEqual(state.instances[0].info.bbox_pixels, (1, 1, 2, 2))

    def test_manifest_keeps_immutable_revision_history(self) -> None:
        first = encode_binary_png(Image.new("1", (10, 10), 0))
        self.store.save_frame(
            0,
            expected_revision=0,
            status="edited",
            instances=[MaskEdit(obj_id=1, label="boom", png=first)],
            user="ana",
        )
        self.store.save_frame(
            0,
            expected_revision=1,
            status="ok",
            instances=[],
            user="bia",
        )

        manifest = json.loads(self.store.manifest_path.read_text())
        history = manifest["frames"]["0"]["history"]
        self.assertEqual([item["revision"] for item in history], [1, 2])
        self.assertEqual(
            history[0]["instances"][0]["path"],
            "reviews/000000/rev_000001/1.png",
        )
        self.assertEqual(history[0]["deleted_obj_ids"], [])

    def test_empty_edited_manifest_has_explicit_instance_tombstone(self) -> None:
        self.store.save_frame(
            0,
            expected_revision=0,
            status="edited",
            instances=[],
            user="ana",
        )

        manifest = json.loads(self.store.manifest_path.read_text())
        self.assertEqual(manifest["frames"]["0"]["deleted_obj_ids"], [1])

    def test_edited_revision_references_unchanged_mask_without_copying_png(self) -> None:
        state = self.store.save_frame(
            0,
            expected_revision=0,
            status="edited",
            instances=[],
            retain_obj_ids=[1],
            user="ana",
        )

        self.assertEqual(state.instances[0].path, self.raw_path)
        self.assertFalse((self.out_dir / "reviews" / "000000" / "rev_000001" / "1.png").exists())
        manifest = json.loads(self.store.manifest_path.read_text())
        self.assertEqual(manifest["frames"]["0"]["instances"][0]["path"], "masks/1/000000.png")

    def test_manifest_cannot_read_a_mask_outside_the_generation(self) -> None:
        outside = self.out_dir.parent / "outside.png"
        outside.write_bytes(mask_at((10, 10), 9, 9))
        self.store.manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "frames": {
                        "0": {
                            "revision": 1,
                            "status": "edited",
                            "instances": [
                                {
                                    "obj_id": 1,
                                    "label": "boom",
                                    "path": "../../outside.png",
                                }
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "caminho"):
            self.store.get_frame(0)

    def test_manifest_checksum_is_revalidated_against_immutable_bytes(self) -> None:
        edited = self.out_dir / "reviews" / "000000" / "rev_000001" / "1.png"
        edited.parent.mkdir(parents=True)
        edited.write_bytes(mask_at((10, 10), 5, 5))
        self.store.manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "frames": {
                        "0": {
                            "revision": 1,
                            "status": "edited",
                            "instances": [
                                {
                                    "obj_id": 1,
                                    "label": "boom",
                                    "path": "reviews/000000/rev_000001/1.png",
                                    "sha256": "f" * 64,
                                }
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "checksum"):
            self.store.get_frame(0)

    def test_database_hook_runs_before_manifest_commit(self) -> None:
        observed = []

        def before_commit(entry: dict) -> None:
            observed.append((entry["revision"], self.store.manifest_path.exists()))

        self.store.save_frame(
            0,
            expected_revision=0,
            status="ok",
            instances=[],
            before_commit=before_commit,
        )

        self.assertEqual(observed, [(1, False)])
        self.assertTrue(self.store.manifest_path.exists())

    def test_failed_database_hook_does_not_advance_manifest_revision(self) -> None:
        def fail(_: dict) -> None:
            raise RuntimeError("database unavailable")

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.store.save_frame(
                0,
                expected_revision=0,
                status="ok",
                instances=[],
                before_commit=fail,
            )

        self.assertFalse(self.store.manifest_path.exists())

    def test_corrupt_existing_manifest_fails_closed_and_is_not_replaced(self) -> None:
        corrupt = b'{"schema_version": 1, "frames": '
        self.store.manifest_path.write_bytes(corrupt)

        with self.assertRaises(ValueError):
            self.store.save_frame(
                0,
                expected_revision=0,
                status="ok",
                instances=[],
                user="guilherme",
            )

        self.assertEqual(self.store.manifest_path.read_bytes(), corrupt)

    def test_batch_saves_original_and_edited_frames_after_validating_all_revisions(self) -> None:
        second_raw = self.out_dir / "masks" / "1" / "000001.png"
        second_raw.write_bytes(mask_at((10, 10), 2, 2))
        save_frames = getattr(self.store, "save_frames", None)
        self.assertTrue(callable(save_frames), "FileMaskReviewStore.save_frames ausente")

        observed = []
        states = save_frames(
            [
                {
                    "frame": 0,
                    "expected_revision": 0,
                    "status": "ok",
                    "instances": [],
                    "retain_obj_ids": [],
                },
                {
                    "frame": 1,
                    "expected_revision": 0,
                    "status": "edited",
                    "instances": [
                        MaskEdit(
                            obj_id=1,
                            label="boom",
                            png=mask_at((10, 10), 8, 7),
                        )
                    ],
                    "retain_obj_ids": [],
                },
            ],
            user="guilherme",
            before_commit=lambda frame, entry: observed.append(
                (frame, entry["revision"], self.store.manifest_path.exists())
            ),
        )

        self.assertEqual([(state.frame, state.status) for state in states], [(0, "ok"), (1, "edited")])
        self.assertEqual(observed, [(0, 1, False), (1, 1, False)])
        manifest = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["frames"]["0"]["status"], "ok")
        self.assertEqual(manifest["frames"]["1"]["status"], "edited")
        self.assertEqual(self.store.get_frame(1).instances[0].info.bbox_pixels, (8, 7, 9, 8))

    def test_batch_revision_conflict_commits_no_frame(self) -> None:
        second_raw = self.out_dir / "masks" / "1" / "000001.png"
        second_raw.write_bytes(mask_at((10, 10), 2, 2))
        self.store.save_frame(
            1,
            expected_revision=0,
            status="ok",
            instances=[],
            user="ana",
        )
        before = self.store.manifest_path.read_bytes()
        save_frames = getattr(self.store, "save_frames", None)
        self.assertTrue(callable(save_frames), "FileMaskReviewStore.save_frames ausente")

        with self.assertRaises(RevisionConflict):
            save_frames(
                [
                    {
                        "frame": 0,
                        "expected_revision": 0,
                        "status": "ok",
                        "instances": [],
                    },
                    {
                        "frame": 1,
                        "expected_revision": 0,
                        "status": "ok",
                        "instances": [],
                    },
                ],
                user="guilherme",
            )

        self.assertEqual(self.store.manifest_path.read_bytes(), before)
        self.assertIsNone(self.store.get_frame(0).status)


if __name__ == "__main__":
    unittest.main()
