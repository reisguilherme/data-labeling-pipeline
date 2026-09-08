from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


class WorkerContextTests(unittest.TestCase):
    def test_each_job_reloads_annotations_written_by_the_api_process(self) -> None:
        from server.config import settings
        from server.workspace import workspace
        from worker import _context

        root = Path(tempfile.mkdtemp())
        (root / "boom" / "raw").mkdir(parents=True)
        dataset = root / "boom" / "dataset"
        dataset.mkdir(parents=True)
        (root / "objects.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "objects": [
                        {
                            "object_id": "boom",
                            "display_name": "Boom",
                            "label": "boom",
                            "videos_root": "boom/raw",
                            "output_root": "boom/dataset",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        annotations = {
            "schema_version": 2,
            "videos": {},
            "counts": {},
        }
        path = dataset / "annotations.json"
        path.write_text(json.dumps(annotations), encoding="utf-8")

        previous_env = os.environ.get("MST_WORKSPACE")
        previous_root = settings.workspace_root
        os.environ["MST_WORKSPACE"] = str(root)
        try:
            workspace._contexts.clear()
            first = _context({"payload": {"object_id": "boom"}})
            self.assertIsNone(first.store.entry("clip.mp4"))

            annotations["videos"]["clip.mp4"] = {
                "status": "in_progress",
                "intervals": [{"segment": "seg_00", "frame_count": 4}],
            }
            path.write_text(json.dumps(annotations), encoding="utf-8")

            second = _context({"payload": {"object_id": "boom"}})
            self.assertEqual(second.store.entry("clip.mp4")["intervals"][0]["frame_count"], 4)
        finally:
            workspace._contexts.clear()
            settings.workspace_root = previous_root
            if previous_env is None:
                os.environ.pop("MST_WORKSPACE", None)
            else:
                os.environ["MST_WORKSPACE"] = previous_env


if __name__ == "__main__":
    unittest.main()
