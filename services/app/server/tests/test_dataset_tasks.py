from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "pipeline-core" / "src"))

from pipeline_core.masks import encode_binary_png
from pipeline_core.segmentation import decode_coco_rle
from server.dataset import Filters, export, preview


class DatasetTaskExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "sam3_classes.json").write_text(
            json.dumps({"names": ["boom"]}), encoding="utf-8"
        )

        self.output = self.root / "output"
        segment = self.output / "video-a" / "seg_000"
        segment.mkdir(parents=True)
        Image.new("RGB", (4, 4), "black").save(segment / "000000.jpg")
        (segment / "prompt.json").write_text(
            json.dumps(
                {
                    "image_width": 4,
                    "image_height": 4,
                    "prompt_frame_idx": 0,
                    "objects": [
                        {"obj_id": 1, "label": "boom", "normalized": [0, 0, 1, 1]}
                    ],
                }
            ),
            encoding="utf-8",
        )
        mask = Image.new("1", (4, 4), 0)
        for y in (1, 2):
            for x in (1, 2):
                mask.putpixel((x, y), 1)
        mask_path = segment / "_sam3" / "masks" / "1" / "000000.png"
        mask_path.parent.mkdir(parents=True)
        mask_path.write_bytes(encode_binary_png(mask))

        entry = {
            "status": "done",
            "video_id": "v1",
            "export": {"root": str(self.output / "video-a"), "segments": ["seg_000"]},
            "intervals": [{"segment": "seg_000", "frame_count": 1, "flags": {}}],
            "media": {"width": 4, "height": 4},
        }
        self.ctx = SimpleNamespace(
            object_id="boom",
            label="boom",
            output_root=self.output,
            store=SimpleNamespace(doc={"videos": {"bucket/video.mp4": entry}}),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _export(self, fmt: str, task: str) -> Path:
        out = self.root / f"{fmt}-{task}"
        export(
            self.ctx,
            Filters(),
            out_dir=out,
            fmt=fmt,
            task=task,
            val_fraction=0,
            workspace_root=self.workspace,
        )
        return out

    def test_yolo_detection_bbox_is_derived_from_mask(self) -> None:
        out = self._export("yolo", "detection")
        label = next((out / "labels" / "train").glob("*.txt")).read_text().strip()

        self.assertEqual(label, "0 0.500000 0.500000 0.500000 0.500000")
        manifest = json.loads((out / "dataset_manifest.json").read_text())
        self.assertEqual(manifest["task"], "detection")

    def test_yolo_segmentation_emits_normalized_contour(self) -> None:
        out = self._export("yolo", "segmentation")
        fields = next((out / "labels" / "train").glob("*.txt")).read_text().split()

        self.assertEqual(fields[0], "0")
        self.assertGreaterEqual(len(fields), 7)
        self.assertTrue(all(0 <= float(value) <= 1 for value in fields[1:]))

    def test_coco_segmentation_rle_round_trips_exact_mask(self) -> None:
        out = self._export("coco", "segmentation")
        document = json.loads((out / "instances_train.json").read_text())
        annotation = document["annotations"][0]

        decoded = decode_coco_rle(annotation["segmentation"])
        expected = Image.new("1", (4, 4), 0)
        for y in (1, 2):
            for x in (1, 2):
                expected.putpixel((x, y), 1)
        self.assertEqual(decoded.tobytes(), expected.tobytes())
        self.assertEqual(annotation["area"], 4)
        self.assertEqual(annotation["bbox"], [1, 1, 2, 2])
        self.assertEqual(decoded.getbbox(), (1, 1, 3, 3))

    def test_segmentation_preview_blocks_legacy_segment_without_masks(self) -> None:
        mask_dir = self.output / "video-a" / "seg_000" / "_sam3" / "masks"
        for path in mask_dir.rglob("*.png"):
            path.unlink()
        for path in sorted(mask_dir.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
        mask_dir.rmdir()
        labels = self.output / "video-a" / "seg_000" / "_sam3" / "labels"
        labels.mkdir(parents=True)
        (labels / "000000.txt").write_text("0 0.5 0.5 1 1\n")

        result = preview(
            self.ctx,
            Filters(),
            self.workspace,
            task="segmentation",
        )

        self.assertIs(result["export_allowed"], False)
        self.assertTrue(result["blocking_reasons"])

    def test_segmentation_export_rejects_incomplete_mask_run(self) -> None:
        mask = self.output / "video-a" / "seg_000" / "_sam3" / "masks" / "1" / "000000.png"
        mask.unlink()

        with self.assertRaisesRegex(ValueError, "mascara ausente"):
            self._export("coco", "segmentation")


if __name__ == "__main__":
    unittest.main()
