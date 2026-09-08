from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.review import SegmentPaths, prompt_contract


class PromptContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.segment_dir = Path(tempfile.mkdtemp()) / "seg_00"
        self.segment_dir.mkdir()
        self.paths = SegmentPaths(self.segment_dir)
        (self.segment_dir / "prompt.json").write_text(
            json.dumps(
                {
                    "image_width": 1920,
                    "image_height": 1080,
                    "source_start_frame": 100,
                    "prompt_frame_idx": 7,
                    "flags": {"dificuldade": "medio"},
                    "objects": [
                        {
                            "obj_id": 4,
                            "label": "boom",
                            "box_normalized": [0.1, 0.2, 0.3, 0.4],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_uses_prompt_objects_before_sam3_has_labels(self) -> None:
        contract = prompt_contract(self.paths)

        self.assertEqual(contract["frame_idx"], 7)
        self.assertEqual(
            contract["objects"],
            [
                {
                    "obj_id": 4,
                    "label": "boom",
                    "normalized": [0.1, 0.2, 0.3, 0.4],
                }
            ],
        )

    def test_saved_override_has_precedence_over_original_boxes(self) -> None:
        self.paths.out_dir.mkdir()
        (self.paths.out_dir / "prompt_override.json").write_text(
            json.dumps(
                {
                    "objects": [
                        {
                            "obj_id": 9,
                            "label": "boom",
                            "box_normalized": [0.5, 0.6, 0.7, 0.8],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        contract = prompt_contract(self.paths)

        self.assertEqual(contract["frame_idx"], 7)
        self.assertEqual(contract["objects"][0]["obj_id"], 9)
        self.assertEqual(contract["objects"][0]["normalized"], [0.5, 0.6, 0.7, 0.8])


if __name__ == "__main__":
    unittest.main()
