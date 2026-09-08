"""Concurrency regressions for the annotation store."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server.store import AnnotationStore


class AnnotationStoreConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_writer_cannot_publish_before_first_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            annotations_path = root / "annotations.json"
            store = AnnotationStore(
                annotations_path=annotations_path,
                cache_dir=root / "cache",
                videos_root=root / "videos",
                output_root=root / "output",
                object_id="ordered-object",
                label="Ordered object",
                total_provider=lambda: 1,
            )
            store.load()
            first_replace_started = threading.Event()
            release_first_replace = threading.Event()
            replace_calls = 0
            real_replace = os.replace

            def blocking_replace(source: str | bytes, destination: str | bytes) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    first_replace_started.set()
                    release_first_replace.wait(timeout=2)
                real_replace(source, destination)

            with patch("server.store.os.replace", blocking_replace):
                first = asyncio.create_task(
                    store.put_entry(
                        "video.mp4", {"status": "in_progress", "intervals": []}
                    )
                )
                started = await asyncio.wait_for(
                    asyncio.to_thread(first_replace_started.wait, 1), timeout=2
                )
                self.assertTrue(started)
                second = asyncio.create_task(
                    store.put_entry(
                        "video.mp4", {"status": "done", "intervals": []}
                    )
                )
                await asyncio.sleep(0)
                self.assertFalse(second.done())
                release_first_replace.set()
                await asyncio.gather(first, second)

            persisted = json.loads(annotations_path.read_text(encoding="utf-8"))
            self.assertEqual(replace_calls, 2)
            self.assertEqual(persisted["videos"]["video.mp4"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
