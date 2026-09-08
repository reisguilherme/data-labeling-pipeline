from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import migrate_legacy
from scripts.migrate_legacy import filter_decisions, inventory


class LegacyMigrationInventoryTests(unittest.TestCase):
    def test_existing_minio_objects_are_listed_once_per_bucket(self) -> None:
        indexer = getattr(migrate_legacy, "index_existing_objects", None)
        if indexer is None:
            self.fail("index_existing_objects ainda nao foi implementado")

        class Item:
            def __init__(self, name: str, size: int) -> None:
                self.object_name = name
                self.size = size

        class Client:
            def __init__(self) -> None:
                self.calls: list[tuple[str, bool]] = []

            def list_objects(self, bucket: str, recursive: bool):
                self.calls.append((bucket, recursive))
                return [Item(f"{bucket}/one", 10), Item(f"{bucket}/two", 20)]

        client = Client()
        indexed = indexer(client, ("videos", "frames"))

        self.assertEqual(client.calls, [("videos", True), ("frames", True)])
        self.assertEqual(indexed["frames"]["frames/two"], 20)

    def test_inventory_counts_videos_segments_frames_and_reviews_without_writes(self) -> None:
        root = Path(tempfile.mkdtemp())
        dataset = root / "boom" / "dataset"
        segment = dataset / "movie" / "seg_00"
        (segment / "_sam3").mkdir(parents=True)
        for index in range(2):
            (segment / f"{index:06d}.jpg").write_bytes(b"jpeg")
        (segment / "prompt.json").write_text("{}")
        (segment / "_sam3" / "review.json").write_text(
            json.dumps({"frames": {"0": {"status": "ok"}}})
        )
        (dataset / "annotations.json").write_text(
            json.dumps(
                {
                    "videos": {
                        "movie.mp4": {
                            "status": "done",
                            "export": {"root": str(dataset / "movie"), "segments": ["seg_00"]},
                            "intervals": [{"segment": "seg_00", "frame_count": 2}],
                        }
                    }
                }
            )
        )

        result = inventory(root, "boom")

        self.assertEqual(result["videos"], 1)
        self.assertEqual(result["segments"], 1)
        self.assertEqual(result["frames"], 2)
        self.assertEqual(result["legacy_reviews"], 1)
        self.assertEqual(list(root.rglob("migration*.json")), [])

    def test_filter_decisions_preserve_keep_trash_and_pending_video_state(self) -> None:
        root = Path(tempfile.mkdtemp())
        meta = root / "movies-meta"
        meta.mkdir()
        (meta / "movies-keep.txt").write_text("useful.mp4\npending.mov\n", encoding="utf-8")
        (meta / "movies-trash.txt").write_text("discard.mp4\n", encoding="utf-8")
        (meta / "movies-trash-duplicados.txt").write_text("duplicate.mp4\n", encoding="utf-8")

        decisions = filter_decisions(root)

        self.assertEqual(decisions["useful.mp4"], "keep")
        self.assertEqual(decisions["pending.mov"], "keep")
        self.assertEqual(decisions["discard.mp4"], "trash")
        self.assertEqual(decisions["duplicate.mp4"], "trash_duplicate")


if __name__ == "__main__":
    unittest.main()
