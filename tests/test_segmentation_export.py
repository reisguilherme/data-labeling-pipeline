from __future__ import annotations

import sys
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "pipeline-core" / "src"))

from pipeline_core.segmentation import coco_rle, decode_coco_rle, yolo_polygons


class SegmentationEncodingTests(unittest.TestCase):
    def test_coco_rle_round_trip_preserves_every_pixel(self) -> None:
        image = Image.new("1", (5, 4), 0)
        for point in [(0, 0), (0, 1), (3, 2), (4, 3)]:
            image.putpixel(point, 1)

        encoded = coco_rle(image)
        decoded = decode_coco_rle(encoded)

        self.assertEqual(encoded["size"], [4, 5])
        self.assertIsInstance(encoded["counts"], str)
        self.assertEqual(decoded.convert("L").tobytes(), image.convert("L").tobytes())

    def test_yolo_polygon_follows_mask_boundary_not_bbox_center_format(self) -> None:
        image = Image.new("1", (4, 4), 0)
        for y in range(1, 3):
            for x in range(1, 3):
                image.putpixel((x, y), 1)

        result = yolo_polygons(image)

        self.assertEqual(len(result.polygons), 1)
        self.assertEqual(
            set(result.polygons[0]),
            {(0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)},
        )
        self.assertEqual(result.warnings, ())

    def test_yolo_polygon_reports_holes_that_format_cannot_preserve(self) -> None:
        image = Image.new("1", (5, 5), 1)
        image.putpixel((2, 2), 0)

        result = yolo_polygons(image)

        self.assertIn("holes_dropped:1", result.warnings)


if __name__ == "__main__":
    unittest.main()
