from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server import proxy


class ProxyReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.cache = Path(self.temporary.name) / "cache"
        self.ctx = SimpleNamespace(cache_dir=self.cache)
        self.root = proxy.proxy_dir(self.ctx, "video-1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _staging(self, *, frames: int = 2) -> Path:
        staging = proxy.new_staging_generation(self.root)
        for tier in proxy.TIERS:
            tier_dir = staging / tier
            tier_dir.mkdir(parents=True)
            for frame in range(frames):
                (tier_dir / f"{frame:06d}.jpg").write_bytes(b"jpeg")
        return staging

    def _publish(self, *, frames: int = 2):
        return proxy.publish_generation(
            self._staging(frames=frames),
            self.root,
            self.cache,
            kind="full",
            job_id="job-1",
        )

    def test_complete_requires_v2_marker_equal_tiers_and_nonempty_boundaries(self) -> None:
        generation = self._publish(frames=2)
        self.assertEqual(proxy.is_complete(self.ctx, "video-1"), 2)

        marker = generation.path / proxy.COMPLETE_MARKER
        original = marker.read_bytes()
        data = json.loads(original)

        data["schema_version"] = 1
        marker.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(proxy.is_complete(self.ctx, "video-1"))

        marker.write_bytes(original)
        data = json.loads(original)
        data["tier_counts"][proxy.FULL] = 1
        marker.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(proxy.is_complete(self.ctx, "video-1"))

        marker.write_bytes(original)
        (generation.path / proxy.FULL / "000001.jpg").unlink()
        self.assertIsNone(proxy.is_complete(self.ctx, "video-1"))

        (generation.path / proxy.FULL / "000001.jpg").write_bytes(b"")
        self.assertIsNone(proxy.is_complete(self.ctx, "video-1"))

    def test_legacy_flat_cache_and_part_generation_are_never_available(self) -> None:
        for tier in proxy.TIERS:
            flat = self.root / tier
            flat.mkdir(parents=True, exist_ok=True)
            (flat / "000000.jpg").write_bytes(b"legacy")
        (self.root / proxy.COMPLETE_MARKER).write_text('{"frames": 1}', encoding="utf-8")
        staging = self._staging(frames=1)

        self.assertIsNone(proxy.is_complete(self.ctx, "video-1"))
        self.assertEqual(proxy.available_ranges(self.ctx, "video-1"), [])
        self.assertIsNone(proxy.locate_frame(self.ctx, "video-1", 0, proxy.SMALL))
        self.assertIn(".part", staging.name)

    def test_current_is_the_only_publication_write_and_staging_is_hidden(self) -> None:
        staging = self._staging(frames=3)
        other_staging = proxy.new_staging_generation(self.root)
        self.assertNotEqual(staging, other_staging)
        self.assertIsNone(proxy.locate_frame(self.ctx, "video-1", 0, proxy.SMALL))

        generation = proxy.publish_generation(
            staging, self.root, self.cache, kind="full", job_id="job-2"
        )

        self.assertFalse(staging.exists())
        self.assertEqual((self.root / proxy.CURRENT_POINTER).read_text().strip(), generation.token)
        located = proxy.locate_frame(self.ctx, "video-1", 2, proxy.FULL)
        self.assertEqual(located, generation.path / proxy.FULL / "000002.jpg")
        self.assertNotIn(".part", str(located))

        marker = json.loads((generation.path / proxy.COMPLETE_MARKER).read_text())
        self.assertEqual(marker["schema_version"], 2)
        self.assertEqual(marker["generation"], generation.token)
        self.assertEqual(marker["job_id"], "job-2")
        self.assertEqual(marker["kind"], "full")
        self.assertEqual(marker["tier_counts"], {"small": 3, "full": 3})

    def test_published_reads_never_enumerate_generation_frames(self) -> None:
        self._publish(frames=2)
        with patch.object(Path, "glob", side_effect=AssertionError("request enumerou frames")):
            self.assertEqual(proxy.is_complete(self.ctx, "video-1"), 2)
            self.assertEqual(proxy.available_ranges(self.ctx, "video-1"), [[0, 1]])
            self.assertIsNotNone(proxy.locate_frame(self.ctx, "video-1", 1, proxy.FULL))

    def test_publish_failures_never_replace_the_old_current_pointer(self) -> None:
        old = self._publish(frames=1)
        pointer = self.root / proxy.CURRENT_POINTER
        old_pointer = pointer.read_bytes()

        invalid = self._staging(frames=2)
        (invalid / proxy.FULL / "000001.jpg").unlink()
        with self.assertRaises(ValueError):
            proxy.publish_generation(invalid, self.root, self.cache, kind="full", job_id="bad")
        self.assertEqual(pointer.read_bytes(), old_pointer)
        self.assertEqual(proxy.current_generation(self.root, expected_kind="full").token, old.token)

        after_rename = self._staging(frames=2)
        real_replace = os.replace

        def fail_pointer_replace(source, destination):
            if Path(destination) == pointer:
                raise OSError("crash before CURRENT")
            return real_replace(source, destination)

        with patch("server.proxy.os.replace", side_effect=fail_pointer_replace):
            with self.assertRaisesRegex(OSError, "crash before CURRENT"):
                proxy.publish_generation(
                    after_rename, self.root, self.cache, kind="full", job_id="job-3"
                )

        self.assertEqual(pointer.read_bytes(), old_pointer)
        self.assertEqual(proxy.current_generation(self.root, expected_kind="full").token, old.token)

    def test_window_ranges_and_frames_resolve_only_through_current(self) -> None:
        root = proxy.window_dir(self.ctx, "video-1", 8, 10)
        staging = proxy.new_staging_generation(root)
        for tier in proxy.TIERS:
            (staging / tier).mkdir(parents=True)
            for frame in range(3):
                (staging / tier / f"{frame:06d}.jpg").write_bytes(b"jpeg")

        self.assertEqual(proxy.available_ranges(self.ctx, "video-1"), [])
        proxy.publish_generation(
            staging,
            root,
            self.cache,
            kind="window",
            job_id="window-job",
            start=8,
            end=10,
        )

        self.assertEqual(proxy.available_ranges(self.ctx, "video-1"), [[8, 10]])
        located = proxy.locate_frame(self.ctx, "video-1", 9, proxy.SMALL)
        self.assertEqual(located.name, "000001.jpg")
        self.assertNotIn(".part", str(located))


if __name__ == "__main__":
    unittest.main()
