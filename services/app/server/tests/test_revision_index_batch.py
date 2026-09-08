from __future__ import annotations

import json
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from server import sam3_run_index
from server.routers.review import save_mask_review_batch


class RevisionIndexBatchTests(unittest.TestCase):
    def test_batch_save_does_not_block_the_async_application_loop(self) -> None:
        self.assertFalse(
            inspect.iscoroutinefunction(save_mask_review_batch),
            "a persistência síncrona precisa executar no pool de threads do FastAPI",
        )

    def test_all_frame_revisions_share_one_database_connection(self) -> None:
        segment_dir = Path(tempfile.mkdtemp()) / "seg_00"
        segment_dir.mkdir()
        (segment_dir / "prompt.json").write_text(
            json.dumps(
                {
                    "source_start_frame": 10,
                    "source_end_frame": 11,
                    "frame_count": 2,
                    "image_width": 640,
                    "image_height": 360,
                    "objects": [{"obj_id": 1, "label": "boom"}],
                }
            ),
            encoding="utf-8",
        )

        index_revisions = getattr(sam3_run_index, "index_revisions", None)
        self.assertTrue(callable(index_revisions), "index_revisions em lote ausente")

        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [
            ("run-1", "project-1"),
            None,
            ("revision-1",),
            None,
            ("revision-2",),
        ]

        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}), patch(
            "psycopg.connect", return_value=connection
        ) as connect:
            index_revisions(
                object_id="boom",
                relpath="clip.mp4",
                segment_dir=segment_dir,
                revisions=[
                    (0, {"revision": 1, "status": "ok"}),
                    (1, {"revision": 1, "status": "ok"}),
                ],
                user="guilherme",
            )

        connect.assert_called_once_with("postgresql://test")
        revision_inserts = [
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO revisions" in call.args[0]
        ]
        self.assertEqual(len(revision_inserts), 2)


if __name__ == "__main__":
    unittest.main()
