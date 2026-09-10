from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from server import dataset
from server.workspace import workspace
from services.app import worker


class DatasetExportWorkerTests(unittest.TestCase):
    def test_worker_consumes_the_frozen_snapshot_and_fences_publication(self) -> None:
        root = Path(tempfile.mkdtemp())
        output = root / "boom" / "dataset"
        output.mkdir(parents=True)
        ctx = SimpleNamespace(
            object_id="boom",
            output_root=output,
            ensure_loaded=MagicMock(),
        )
        snapshot = {"snapshot_id": "a" * 64, "object_id": "boom"}
        job = {
            "id": "job-1",
            "payload": {
                "object_id": "boom",
                "name": "nightly",
                "format": "yolo",
                "task": "detection",
                "val_fraction": 0.2,
                "test_fraction": 0,
                "snapshot": snapshot,
            },
        }
        queue = MagicMock()
        queue.update_progress.return_value = True
        captured: dict = {}

        def atomic(ctx_arg, snapshot_arg, **kwargs):
            captured.update(ctx=ctx_arg, snapshot=snapshot_arg, kwargs=kwargs)
            kwargs["before_publish"]()
            return {"snapshot_id": snapshot_arg["snapshot_id"], "images": 1}

        with patch.dict(os.environ, {"MST_WORKSPACE": str(root)}), patch.object(
            workspace, "load"
        ), patch.object(workspace, "context", return_value=ctx), patch.object(
            dataset, "validate_snapshot"
        ), patch.object(dataset, "export_snapshot_atomic", side_effect=atomic), patch.object(
            worker.MinioBlobStore, "from_env", return_value=None
        ):
            result = worker.run_dataset_export(job, queue, "lease-1")

        self.assertIs(captured["ctx"], ctx)
        self.assertIs(captured["snapshot"], snapshot)
        self.assertEqual(captured["kwargs"]["out_dir"], output / "_datasets" / "nightly")
        self.assertEqual(result["snapshot_id"], "a" * 64)
        self.assertTrue(
            any(
                call.args[2].get("message") == "publicando dataset"
                for call in queue.update_progress.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
