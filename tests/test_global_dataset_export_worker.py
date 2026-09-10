from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server import multiclass_dataset as multiclass
from server.workspace import workspace
from services.app import worker


class GlobalDatasetExportWorkerTests(unittest.TestCase):
    def test_worker_uses_only_snapshot_and_fences_atomic_publication(self) -> None:
        root = Path(tempfile.mkdtemp())
        snapshot = {
            "snapshot_id": "a" * 64,
            "scope": "global",
            "objects": [],
        }
        job = {
            "id": "job-global-1",
            "payload": {
                "name": "nightly-global",
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

        def atomic(root_arg, snapshot_arg, **kwargs):
            captured.update(root=root_arg, snapshot=snapshot_arg, kwargs=kwargs)
            kwargs["before_publish"]()
            out_dir = kwargs["out_dir"]
            (out_dir / "images").mkdir(parents=True)
            (out_dir / "labels").mkdir()
            (out_dir / "data.yaml").write_text("names: []", encoding="utf-8")
            (out_dir / "images" / "frame.jpg").write_bytes(b"image")
            (out_dir / "labels" / "frame.txt").write_text("", encoding="utf-8")
            (out_dir / "dataset_manifest.json").write_text("{}", encoding="utf-8")
            return {"snapshot_id": snapshot_arg["snapshot_id"], "images": 1}

        store = MagicMock()
        with patch.dict(os.environ, {"MST_WORKSPACE": str(root)}), patch.object(
            workspace, "load", side_effect=AssertionError("workspace live relido")
        ), patch.object(
            workspace, "list", side_effect=AssertionError("registro live relido")
        ), patch.object(
            workspace, "context", side_effect=AssertionError("contexto live relido")
        ), patch.object(
            multiclass, "_eligible_ids", side_effect=AssertionError("fila live relida")
        ), patch.object(
            multiclass, "validate_multiclass_snapshot"
        ) as validate, patch.object(
            multiclass,
            "export_multiclass_snapshot_atomic",
            side_effect=atomic,
        ), patch.object(
            worker.MinioBlobStore, "from_env", return_value=store
        ):
            result = worker.run_global_dataset_export(job, queue, "lease-1")

        validate.assert_called_once_with(snapshot)
        self.assertEqual(captured["root"], root.resolve())
        self.assertIs(captured["snapshot"], snapshot)
        self.assertEqual(
            captured["kwargs"]["out_dir"], root.resolve() / "_datasets" / "nightly-global"
        )
        self.assertEqual(result["snapshot_id"], "a" * 64)
        self.assertTrue(
            any(
                call.args[2].get("message") == "publicando dataset global"
                for call in queue.update_progress.call_args_list
            )
        )
        stored_keys = [call.args[1] for call in store.put_file.call_args_list]
        stored_key = stored_keys[-1]
        self.assertEqual(
            stored_key,
            "global/nightly-global/generations/"
            + "a" * 64
            + "/dataset_manifest.json",
        )
        self.assertEqual(stored_keys.count(stored_key), 1)
        self.assertTrue(all(not key.endswith("dataset_manifest.json") for key in stored_keys[:-1]))

    def test_worker_stops_minio_uploads_as_soon_as_the_lease_is_lost(self) -> None:
        root = Path(tempfile.mkdtemp())
        snapshot = {
            "snapshot_id": "a" * 64,
            "scope": "global",
            "objects": [],
        }
        job = {
            "id": "job-global-1",
            "payload": {
                "name": "nightly-global",
                "format": "yolo",
                "task": "detection",
                "val_fraction": 0.2,
                "test_fraction": 0,
                "snapshot": snapshot,
            },
        }
        queue = MagicMock()
        # Local publication succeeds, the first blob is uploaded, then the
        # lease is lost before the second blob can be written.
        queue.update_progress.side_effect = [True, True, False]

        def atomic(root_arg, snapshot_arg, **kwargs):
            kwargs["before_publish"]()
            out_dir = kwargs["out_dir"]
            (out_dir / "images").mkdir(parents=True)
            (out_dir / "labels").mkdir()
            (out_dir / "images" / "frame.jpg").write_bytes(b"image")
            (out_dir / "labels" / "frame.txt").write_text("", encoding="utf-8")
            (out_dir / "dataset_manifest.json").write_text("{}", encoding="utf-8")
            return {"snapshot_id": snapshot_arg["snapshot_id"], "images": 1}

        uploaded: list[str] = []

        class RecordingStore:
            def put_file(self, bucket: str, key: str, path: Path) -> None:
                uploaded.append(key)

        with patch.dict(os.environ, {"MST_WORKSPACE": str(root)}), patch.object(
            multiclass, "validate_multiclass_snapshot"
        ), patch.object(
            multiclass,
            "export_multiclass_snapshot_atomic",
            side_effect=atomic,
        ), patch.object(
            worker.MinioBlobStore, "from_env", return_value=RecordingStore()
        ):
            with self.assertRaises(worker.Cancelled):
                worker.run_global_dataset_export(job, queue, "lease-1")

        self.assertEqual(len(uploaded), 1)
        self.assertFalse(any(key.endswith("dataset_manifest.json") for key in uploaded))


if __name__ == "__main__":
    unittest.main()
