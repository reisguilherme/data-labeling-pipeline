from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from pipeline_core.masks import encode_binary_png


class MulticlassDatasetTests(unittest.TestCase):
    def test_class_ids_sort_by_object_id(self) -> None:
        from server.multiclass_dataset import class_map

        result = class_map([("microfone", "microphone"), ("boom", "boom")])
        self.assertEqual(result, {"boom": 0, "microphone": 1})

    def test_duplicate_class_names_are_rejected_case_insensitively(self) -> None:
        from server.multiclass_dataset import class_map

        with self.assertRaisesRegex(ValueError, "classes duplicadas"):
            class_map([("boom", "Object"), ("microfone", " object ")])

    def test_namespaced_stem_prevents_same_video_name_collision(self) -> None:
        from server.multiclass_dataset import namespaced_stem

        self.assertNotEqual(
            namespaced_stem("boom", "abc", "seg_00", 1),
            namespaced_stem("microfone", "abc", "seg_00", 1),
        )

    def test_split_key_contains_object_id_and_video_id(self) -> None:
        from server.multiclass_dataset import split_key

        self.assertEqual(split_key("boom", "abc"), "boom:abc")
        self.assertEqual(split_key("microfone", "abc"), "microfone:abc")

    def test_global_yolo_export_merges_classes_and_namespaces_equal_video_ids(self) -> None:
        from server.multiclass_dataset import export_multiclass

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)

        def context(object_id: str, label: str):
            output = root / object_id / "dataset"
            segment = output / "video" / "seg_00"
            out = segment / "_sam3"
            masks = out / "masks" / "1"
            masks.mkdir(parents=True)
            Image.new("RGB", (4, 4), "black").save(segment / "000000.jpg")
            (segment / "prompt.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "frame_count": 1,
                        "image_width": 4,
                        "image_height": 4,
                        "objects": [{"obj_id": 1, "label": label, "normalized": [0, 0, 1, 1]}],
                    }
                ),
                encoding="utf-8",
            )
            mask = Image.new("1", (4, 4), 0)
            mask.putpixel((1, 1), 1)
            mask_payload = encode_binary_png(mask)
            (masks / "000000.png").write_bytes(mask_payload)
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
                            "checksums": {
                                "masks/1/000000.png": hashlib.sha256(
                                    mask_payload
                                ).hexdigest()
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            (out / "mask_review.json").write_text(
                json.dumps({"frames": {"0": {"revision": 1, "status": "ok"}}}),
                encoding="utf-8",
            )
            entry = {
                "status": "done",
                "video_id": "same-video-id",
                "export": {"root": str(output / "video"), "segments": ["seg_00"]},
                "intervals": [{"segment": "seg_00", "frame_count": 1, "flags": {}}],
                "media": {"width": 4, "height": 4},
            }
            return SimpleNamespace(
                object_id=object_id,
                label=label,
                output_root=output,
                store=SimpleNamespace(doc={"videos": {"same.mp4": entry}}),
            )

        out_dir = root / "_datasets" / "merged"
        result = export_multiclass(
            [context("microfone", "microphone"), context("boom", "boom")],
            [],
            flags={},
            include_empty=False,
            out_dir=out_dir,
            fmt="yolo",
            task="detection",
            val_fraction=0,
            test_fraction=0,
            workspace_root=root,
        )

        labels = sorted((out_dir / "labels" / "train").glob("*.txt"))
        self.assertEqual(result["images"], 2)
        self.assertEqual(len(labels), 2)
        self.assertEqual({path.read_text().split()[0] for path in labels}, {"0", "1"})
        self.assertEqual(len({path.stem for path in labels}), 2)
        manifest = json.loads((out_dir / "dataset_manifest.json").read_text())
        self.assertEqual(
            [(item["object_id"], item["id"]) for item in manifest["classes"]],
            [("boom", 0), ("microfone", 1)],
        )


if __name__ == "__main__":
    unittest.main()
