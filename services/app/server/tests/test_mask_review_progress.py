from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.review import SegmentPaths, progress_for, segment_state


class MaskReviewProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.export_root = Path(tempfile.mkdtemp()) / "export"
        self.paths = SegmentPaths(self.export_root / "seg_00")
        self.paths.labels_dir.mkdir(parents=True)
        (self.paths.out_dir / "masks").mkdir()
        self.paths.marker_path.write_text(
            json.dumps({"frame_count": 2}), encoding="utf-8"
        )
        self.paths.review_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "frames": {
                        "0": {"status": "ok"},
                        "1": {"status": "ok"},
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_mask_run_does_not_treat_legacy_bbox_approvals_as_mask_reviews(self) -> None:
        segment = segment_state(self.paths, 2, ["boom"])
        video = progress_for(self.export_root, ["seg_00"], ["boom"])

        self.assertEqual([frame["status"] for frame in segment["frames"]], [None, None])
        self.assertEqual(segment["reviewed"], 0)
        self.assertFalse(segment["complete"])
        self.assertEqual(video["reviewed"], 0)
        self.assertFalse(video["complete"])

    def test_mask_progress_uses_only_the_canonical_mask_manifest(self) -> None:
        (self.paths.out_dir / "mask_review.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "frames": {
                        "0": {"revision": 1, "status": "ok"},
                        "1": {"revision": 1, "status": "edited"},
                    },
                }
            ),
            encoding="utf-8",
        )

        segment = segment_state(self.paths, 2, ["boom"])
        video = progress_for(self.export_root, ["seg_00"], ["boom"])

        self.assertEqual([frame["status"] for frame in segment["frames"]], ["ok", "edited"])
        self.assertEqual(segment["reviewed"], 2)
        self.assertEqual(segment["edited"], 1)
        self.assertTrue(segment["complete"])
        self.assertEqual(video["reviewed"], 2)
        self.assertEqual(video["edited"], 1)
        self.assertTrue(video["complete"])


if __name__ == "__main__":
    unittest.main()
