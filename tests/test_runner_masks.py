from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "pipeline-core" / "src"))
sys.path.insert(0, str(ROOT / "services" / "sam3-worker"))

from pipeline_core.masks import inspect_binary_png
from sam3_runner.config import RunnerConfig
from sam3_runner.mask_io import MaskSetValidationError, save_mask_png, validate_mask_set


class RunnerMaskPersistenceTests(unittest.TestCase):
    def test_masks_are_enabled_by_default_for_canonical_annotations(self) -> None:
        self.assertTrue(RunnerConfig().save_masks)

    def test_writes_lossless_binary_png_with_frame_index(self) -> None:
        root = Path(tempfile.mkdtemp())
        image = Image.new("1", (12, 8), 0)
        image.putpixel((5, 3), 1)

        path = save_mask_png(root, obj_id=2, frame_idx=17, mask=image)

        self.assertEqual(path, root / "masks" / "2" / "000017.png")
        info = inspect_binary_png(path.read_bytes(), expected_size=(12, 8))
        self.assertEqual(info.area_pixels, 1)
        self.assertFalse((root / "masks" / "2" / "000017.npy").exists())

    def test_empty_mask_is_persisted_as_explicit_valid_artifact(self) -> None:
        root = Path(tempfile.mkdtemp())

        path = save_mask_png(root, obj_id=1, frame_idx=0, mask=Image.new("1", (8, 6), 0))

        info = inspect_binary_png(path.read_bytes(), expected_size=(8, 6))
        self.assertEqual(info.area_pixels, 0)
        self.assertIsNone(info.bbox_pixels)

    def test_completed_run_requires_every_object_frame_mask_and_checksums(self) -> None:
        root = Path(tempfile.mkdtemp())
        for obj_id in (1, 2):
            for frame in range(3):
                save_mask_png(root, obj_id=obj_id, frame_idx=frame, mask=Image.new("1", (8, 6), 0))

        manifest = validate_mask_set(
            root,
            obj_ids=[1, 2],
            frame_count=3,
            image_size=(8, 6),
        )

        self.assertEqual(manifest["files"], 6)
        self.assertEqual(manifest["empty"], 6)
        self.assertEqual(len(manifest["checksums"]), 6)

    def test_completed_run_rejects_missing_mask(self) -> None:
        root = Path(tempfile.mkdtemp())
        save_mask_png(root, obj_id=1, frame_idx=0, mask=Image.new("1", (8, 6), 0))

        with self.assertRaisesRegex(MaskSetValidationError, "ausentes"):
            validate_mask_set(root, obj_ids=[1], frame_count=2, image_size=(8, 6))


if __name__ == "__main__":
    unittest.main()
