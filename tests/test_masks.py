from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

from PIL import Image

CORE_SRC = Path(__file__).resolve().parents[1] / "packages" / "pipeline-core" / "src"
sys.path.insert(0, str(CORE_SRC))

from pipeline_core.masks import MaskValidationError, encode_binary_png, inspect_binary_png


class BinaryMaskTests(unittest.TestCase):
    def test_round_trip_derives_area_and_exclusive_bbox_from_pixels(self) -> None:
        image = Image.new("1", (10, 10), 0)
        for y in range(2, 5):
            for x in range(3, 8):
                image.putpixel((x, y), 1)

        encoded = encode_binary_png(image)
        info = inspect_binary_png(encoded, expected_size=(10, 10))

        self.assertEqual(info.area_pixels, 15)
        self.assertEqual(info.bbox_pixels, (3, 2, 8, 5))
        self.assertEqual(info.bbox_normalized, (0.3, 0.2, 0.8, 0.5))
        self.assertEqual(Image.open(io.BytesIO(encoded)).mode, "1")

    def test_empty_mask_is_valid_and_has_no_bbox(self) -> None:
        encoded = encode_binary_png(Image.new("1", (8, 6), 0))

        info = inspect_binary_png(encoded, expected_size=(8, 6))

        self.assertEqual(info.area_pixels, 0)
        self.assertIsNone(info.bbox_pixels)
        self.assertIsNone(info.bbox_normalized)

    def test_rejects_non_binary_png_instead_of_silently_thresholding_it(self) -> None:
        image = Image.new("L", (4, 4), 0)
        image.putpixel((1, 1), 128)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        with self.assertRaisesRegex(MaskValidationError, "binária"):
            inspect_binary_png(buffer.getvalue(), expected_size=(4, 4))

    def test_rejects_mask_with_wrong_dimensions(self) -> None:
        encoded = encode_binary_png(Image.new("1", (4, 4), 0))

        with self.assertRaisesRegex(MaskValidationError, "dimensões"):
            inspect_binary_png(encoded, expected_size=(8, 8))


if __name__ == "__main__":
    unittest.main()
