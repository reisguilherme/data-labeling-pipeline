from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server import proxy
from starlette.responses import FileResponse


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

    def _window_staging(
        self, start: int, end: int, payload: bytes = b"jpeg"
    ) -> tuple[Path, Path]:
        root = proxy.window_dir(self.ctx, "video-1", start, end)
        staging = proxy.new_staging_generation(root)
        for tier in proxy.TIERS:
            (staging / tier).mkdir(parents=True)
            for frame in range(end - start + 1):
                (staging / tier / f"{frame:06d}.jpg").write_bytes(payload)
        return root, staging

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

    def test_guard_rechecks_after_pointer_is_durable_and_immediately_before_commit(self) -> None:
        staging = self._staging(frames=1)
        events: list[str] = []
        real_fsync = proxy._fsync_file
        real_replace = os.replace
        real_bounded_text = proxy._bounded_text

        def observed_fsync(path: Path, data: str) -> None:
            real_fsync(path, data)
            if path.name.endswith(".CURRENT.part"):
                events.append("pointer-durable")

        def observed_guard() -> bool:
            events.append("guard")
            return True

        def observed_bounded_text(path: Path, limit: int) -> str | None:
            if path == self.root / proxy.CURRENT_POINTER:
                events.append("previous-token-read")
            return real_bounded_text(path, limit)

        def observed_replace(source, destination) -> None:
            if Path(destination) == self.root / proxy.CURRENT_POINTER:
                events.append("commit")
            real_replace(source, destination)

        with patch("server.proxy._fsync_file", side_effect=observed_fsync), patch(
            "server.proxy._bounded_text", side_effect=observed_bounded_text
        ), patch("server.proxy.os.replace", side_effect=observed_replace):
            proxy.publish_generation(
                staging,
                self.root,
                self.cache,
                kind="full",
                job_id="guarded",
                publish_guard=observed_guard,
            )

        self.assertEqual(
            events[-4:],
            ["pointer-durable", "previous-token-read", "guard", "commit"],
        )

    def test_guard_can_cancel_after_pointer_build_without_replacing_current(self) -> None:
        old = self._publish(frames=1)
        pointer = self.root / proxy.CURRENT_POINTER
        pointer_before = pointer.read_bytes()
        allow_commit = True
        real_fsync = proxy._fsync_file

        def cancel_after_pointer_fsync(path: Path, data: str) -> None:
            nonlocal allow_commit
            real_fsync(path, data)
            if path.name.endswith(".CURRENT.part"):
                allow_commit = False

        with patch("server.proxy._fsync_file", side_effect=cancel_after_pointer_fsync):
            with self.assertRaisesRegex(RuntimeError, "cancelada"):
                proxy.publish_generation(
                    self._staging(frames=2),
                    self.root,
                    self.cache,
                    kind="full",
                    job_id="cancelled",
                    publish_guard=lambda: allow_commit,
                )

        self.assertEqual(pointer.read_bytes(), pointer_before)
        self.assertEqual(proxy.current_generation(self.root).token, old.token)

    def test_failed_overlapping_republish_preserves_index_current_and_winners(self) -> None:
        broad_root, broad_staging = self._window_staging(0, 4, b"broad-old")
        broad = proxy.publish_generation(
            broad_staging,
            broad_root,
            self.cache,
            kind="window",
            job_id="broad-old",
            start=0,
            end=4,
        )
        narrow_root, narrow_staging = self._window_staging(2, 2, b"narrow")
        narrow = proxy.publish_generation(
            narrow_staging,
            narrow_root,
            self.cache,
            kind="window",
            job_id="narrow",
            start=2,
            end=2,
        )
        index_path = broad_root.parent / proxy.WINDOWS_INDEX
        expected_index = index_path.read_bytes()
        expected_frames = [
            proxy.locate_frame(self.ctx, "video-1", frame, proxy.FULL).read_bytes()
            for frame in (1, 2, 3)
        ]

        def assert_original_publication() -> None:
            self.assertEqual(index_path.read_bytes(), expected_index)
            self.assertEqual(proxy.current_generation(broad_root).token, broad.token)
            self.assertEqual(proxy.current_generation(narrow_root).token, narrow.token)
            self.assertEqual(
                [
                    proxy.locate_frame(
                        self.ctx, "video-1", frame, proxy.FULL
                    ).read_bytes()
                    for frame in (1, 2, 3)
                ],
                expected_frames,
            )

        _, rejected = self._window_staging(0, 4, b"guard-rejected")
        with self.assertRaisesRegex(RuntimeError, "cancelada"):
            proxy.publish_generation(
                rejected,
                broad_root,
                self.cache,
                kind="window",
                job_id="guard-rejected",
                start=0,
                end=4,
                publish_guard=lambda: False,
            )
        assert_original_publication()

        _, crashed = self._window_staging(0, 4, b"replace-failed")
        real_replace = os.replace

        def fail_current(source, destination):
            if Path(destination) == broad_root / proxy.CURRENT_POINTER:
                raise OSError("CURRENT replace failed")
            return real_replace(source, destination)

        with patch("server.proxy.os.replace", side_effect=fail_current):
            with self.assertRaisesRegex(OSError, "CURRENT replace failed"):
                proxy.publish_generation(
                    crashed,
                    broad_root,
                    self.cache,
                    kind="window",
                    job_id="replace-failed",
                    start=0,
                    end=4,
                )
        assert_original_publication()

    def test_successful_overlapping_republish_indexes_exact_generation_identity(self) -> None:
        broad_root, broad_staging = self._window_staging(0, 4, b"broad-old")
        proxy.publish_generation(
            broad_staging,
            broad_root,
            self.cache,
            kind="window",
            job_id="broad-old",
            start=0,
            end=4,
        )
        narrow_root, narrow_staging = self._window_staging(2, 2, b"narrow")
        proxy.publish_generation(
            narrow_staging,
            narrow_root,
            self.cache,
            kind="window",
            job_id="narrow",
            start=2,
            end=2,
        )
        _, replacement_staging = self._window_staging(0, 4, b"broad-new")
        replacement = proxy.publish_generation(
            replacement_staging,
            broad_root,
            self.cache,
            kind="window",
            job_id="broad-new",
            start=0,
            end=4,
        )

        overlap = proxy.locate_frame(self.ctx, "video-1", 2, proxy.FULL)
        self.assertIsNotNone(overlap)
        self.assertEqual(overlap.read_bytes(), b"broad-new")
        index = json.loads(
            (broad_root.parent / proxy.WINDOWS_INDEX).read_text(encoding="utf-8")
        )
        broad_entry = next(item for item in index["windows"] if item["start"] == 0)
        narrow_entry = next(item for item in index["windows"] if item["start"] == 2)
        self.assertEqual(index["schema_version"], 3)
        self.assertEqual(broad_entry["generation"], replacement.token)
        self.assertGreater(broad_entry["seq"], narrow_entry["seq"])

    @unittest.skipUnless(hasattr(os, "symlink"), "sistema sem suporte a symlink")
    def test_symlinked_tier_is_never_a_valid_generation_or_frame_source(self) -> None:
        generation = self._publish(frames=1)
        external = Path(self.temporary.name) / "outside"
        external.mkdir()
        (external / "000000.jpg").write_bytes(b"outside")
        tier = generation.path / proxy.SMALL
        for child in tier.iterdir():
            child.unlink()
        tier.rmdir()
        try:
            tier.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink indisponivel: {exc}")

        self.assertIsNone(proxy.current_generation(self.root, expected_kind="full"))
        self.assertIsNone(proxy.locate_frame(self.ctx, "video-1", 0, proxy.SMALL))

    @unittest.skipUnless(hasattr(os, "symlink"), "sistema sem suporte a symlink")
    def test_locate_rechecks_candidate_containment_after_generation_resolution(self) -> None:
        generation = self._publish(frames=1)
        external = Path(self.temporary.name) / "outside"
        external.mkdir()
        (external / "000000.jpg").write_bytes(b"outside")
        tier = generation.path / proxy.SMALL
        for child in tier.iterdir():
            child.unlink()
        tier.rmdir()
        try:
            tier.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink indisponivel: {exc}")

        with patch("server.proxy.current_generation", return_value=generation):
            self.assertIsNone(proxy.locate_frame(self.ctx, "video-1", 0, proxy.SMALL))

    def test_malformed_marker_types_are_cache_misses_never_server_errors(self) -> None:
        generation = self._publish(frames=1)
        marker_path = generation.path / proxy.COMPLETE_MARKER
        valid = json.loads(marker_path.read_text(encoding="utf-8"))
        malformed = (
            {**valid, "tiers": 1},
            {**valid, "kind": []},
            {**valid, "frames": "1"},
            {**valid, "tier_counts": []},
        )

        for marker in malformed:
            with self.subTest(marker=marker):
                marker_path.write_text(json.dumps(marker), encoding="utf-8")
                self.assertIsNone(
                    proxy.current_generation(self.root, expected_kind="full")
                )

        marker_path.write_text(
            '{"schema_version":2,"frames":' + ("9" * 5000) + "}",
            encoding="utf-8",
        )
        self.assertIsNone(proxy.current_generation(self.root, expected_kind="full"))

    def test_malformed_window_index_integer_is_an_empty_cache_not_an_error(self) -> None:
        base = self.cache / "windows" / "video-1"
        base.mkdir(parents=True)
        (base / proxy.WINDOWS_INDEX).write_text(
            '{"schema_version":1,"windows":[{"start":' + ("9" * 5000) + "}]}",
            encoding="utf-8",
        )

        self.assertEqual(proxy.available_ranges(self.ctx, "video-1"), [])
        self.assertIsNone(proxy.locate_frame(self.ctx, "video-1", 0, proxy.SMALL))

    def test_window_reads_use_atomic_index_without_directory_enumeration(self) -> None:
        roots = [
            proxy.window_dir(self.ctx, "video-1", 0, 1),
            proxy.window_dir(self.ctx, "video-1", 4, 5),
        ]

        def publish_window(root: Path, start: int, end: int) -> None:
            staging = proxy.new_staging_generation(root)
            for tier in proxy.TIERS:
                (staging / tier).mkdir(parents=True)
                for frame in range(end - start + 1):
                    (staging / tier / f"{frame:06d}.jpg").write_bytes(b"jpeg")
            proxy.publish_generation(
                staging,
                root,
                self.cache,
                kind="window",
                job_id=f"window-{start}",
                start=start,
                end=end,
            )

        threads = [
            threading.Thread(target=publish_window, args=(roots[0], 0, 1)),
            threading.Thread(target=publish_window, args=(roots[1], 4, 5)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        with patch.object(Path, "iterdir", side_effect=AssertionError("request varreu diretorio")), patch.object(
            Path, "glob", side_effect=AssertionError("request varreu frames")
        ):
            self.assertEqual(proxy.available_ranges(self.ctx, "video-1"), [[0, 1], [4, 5]])
            self.assertIsNotNone(proxy.locate_frame(self.ctx, "video-1", 5, proxy.FULL))

    def test_frame_lookup_reads_only_constant_candidate_markers_with_many_windows(self) -> None:
        for start in range(0, 120, 2):
            root = proxy.window_dir(self.ctx, "video-1", start, start)
            staging = proxy.new_staging_generation(root)
            for tier in proxy.TIERS:
                (staging / tier).mkdir(parents=True, exist_ok=True)
                (staging / tier / "000000.jpg").write_bytes(b"jpeg")
            proxy.publish_generation(
                staging,
                root,
                self.cache,
                kind="window",
                job_id=f"window-{start}",
                start=start,
                end=start,
            )

        marker_reads = 0
        real_bounded_text = proxy._bounded_text

        def count_marker_reads(path: Path, limit: int):
            nonlocal marker_reads
            if path.name == proxy.COMPLETE_MARKER:
                marker_reads += 1
            return real_bounded_text(path, limit)

        with patch("server.proxy._bounded_text", side_effect=count_marker_reads):
            located = proxy.locate_frame(self.ctx, "video-1", 118, proxy.FULL)

        self.assertIsNotNone(located)
        self.assertLessEqual(marker_reads, 2)

    def test_newest_overlapping_window_wins_only_inside_its_coverage(self) -> None:
        for start, end, payload in ((0, 4, b"old"), (2, 2, b"new")):
            root = proxy.window_dir(self.ctx, "video-1", start, end)
            staging = proxy.new_staging_generation(root)
            for tier in proxy.TIERS:
                (staging / tier).mkdir()
                for local in range(end - start + 1):
                    (staging / tier / f"{local:06d}.jpg").write_bytes(payload)
            proxy.publish_generation(
                staging,
                root,
                self.cache,
                kind="window",
                job_id=f"window-{start}-{end}",
                start=start,
                end=end,
            )

        left = proxy.locate_frame(self.ctx, "video-1", 1, proxy.FULL)
        overlap = proxy.locate_frame(self.ctx, "video-1", 2, proxy.FULL)
        right = proxy.locate_frame(self.ctx, "video-1", 3, proxy.FULL)
        self.assertEqual(left.read_bytes(), b"old")
        self.assertEqual(overlap.read_bytes(), b"new")
        self.assertEqual(right.read_bytes(), b"old")

    def test_many_stale_overlaps_materialize_and_validate_only_one_winner(self) -> None:
        count = 24
        target = count
        for offset in range(count):
            start, end = offset, (2 * count) - offset
            root, staging = self._window_staging(start, end, bytes([offset]))
            proxy.publish_generation(
                staging,
                root,
                self.cache,
                kind="window",
                job_id=f"overlap-{offset}",
                start=start,
                end=end,
            )

        base = self.cache / "windows" / "video-1"
        snapshot = proxy._windows_index(base)
        self.assertEqual(
            sum(len(winners) for _, _, winners in snapshot.spans),
            len(snapshot.spans),
            "cada span deve materializar somente seu vencedor",
        )
        for start, end, name, _sequence, _generation in snapshot.entries:
            generation = proxy.current_generation(
                base / name, expected_kind="window", cache_root=self.cache
            )
            self.assertIsNotNone(generation)
            (generation.path / proxy.COMPLETE_MARKER).write_text("{}", encoding="utf-8")

        marker_reads = 0
        real_bounded_text = proxy._bounded_text

        def count_marker_reads(path: Path, limit: int) -> str | None:
            nonlocal marker_reads
            if path.name == proxy.COMPLETE_MARKER:
                marker_reads += 1
            return real_bounded_text(path, limit)

        with patch("server.proxy._bounded_text", side_effect=count_marker_reads):
            self.assertIsNone(
                proxy.locate_frame(self.ctx, "video-1", target, proxy.FULL)
            )
        self.assertLessEqual(marker_reads, 1)

    def test_failed_window_index_swap_preserves_previous_manifest(self) -> None:
        first_root = proxy.window_dir(self.ctx, "video-1", 0, 0)
        first_staging = proxy.new_staging_generation(first_root)
        for tier in proxy.TIERS:
            (first_staging / tier).mkdir(parents=True)
            (first_staging / tier / "000000.jpg").write_bytes(b"jpeg")
        proxy.publish_generation(
            first_staging,
            first_root,
            self.cache,
            kind="window",
            job_id="first",
            start=0,
            end=0,
        )
        index_path = first_root.parent / proxy.WINDOWS_INDEX
        index_before = index_path.read_bytes()

        second_root = proxy.window_dir(self.ctx, "video-1", 4, 4)
        second_staging = proxy.new_staging_generation(second_root)
        for tier in proxy.TIERS:
            (second_staging / tier).mkdir(parents=True)
            (second_staging / tier / "000000.jpg").write_bytes(b"jpeg")
        real_replace = os.replace

        def fail_index_swap(source, destination):
            if Path(destination) == index_path:
                raise OSError("index swap failed")
            return real_replace(source, destination)

        with patch("server.proxy.os.replace", side_effect=fail_index_swap):
            with self.assertRaisesRegex(OSError, "index swap failed"):
                proxy.publish_generation(
                    second_staging,
                    second_root,
                    self.cache,
                    kind="window",
                    job_id="second",
                    start=4,
                    end=4,
                )

        self.assertEqual(index_path.read_bytes(), index_before)
        self.assertEqual(proxy.available_ranges(self.ctx, "video-1"), [[0, 0]])
        self.assertIsNotNone(
            proxy.current_generation(
                second_root, expected_kind="window", cache_root=self.cache
            ),
            "CURRENT pode ter sido publicado antes da falha de INDEX",
        )
        self.assertIsNone(proxy.locate_frame(self.ctx, "video-1", 4, proxy.FULL))

        sweep = getattr(proxy, "sweep_cache", None)
        self.assertTrue(callable(sweep), "sweep operacional ausente")
        sweep(self.ctx, max_roots=8)
        self.assertEqual(proxy.available_ranges(self.ctx, "video-1"), [[0, 0], [4, 4]])
        self.assertIsNotNone(proxy.locate_frame(self.ctx, "video-1", 4, proxy.FULL))

    def test_stale_generation_gc_keeps_current_and_concurrent_staging(self) -> None:
        old = self._publish(frames=1)
        concurrent_staging = self._staging(frames=1)
        new = proxy.publish_generation(
            self._staging(frames=2),
            self.root,
            self.cache,
            kind="full",
            job_id="new",
        )

        self.assertTrue(old.path.exists(), "geração resolvida precisa de grace period")
        self.assertTrue(new.path.exists())
        self.assertTrue(concurrent_staging.exists())
        self.assertEqual(proxy.current_generation(self.root).token, new.token)

        newest = proxy.publish_generation(
            self._staging(frames=3),
            self.root,
            self.cache,
            kind="full",
            job_id="newest",
        )
        proxy.collect_stale_generations(
            self.root,
            self.cache,
            max_delete=8,
            retire_grace_seconds=0,
        )
        self.assertFalse(old.path.exists())
        self.assertTrue(new.path.exists(), "a geração anterior imediata é preservada")
        self.assertTrue(newest.path.exists())
        self.assertTrue(concurrent_staging.exists())

    def test_gc_is_bounded_and_refuses_roots_outside_cache(self) -> None:
        current = self._publish(frames=1)
        stale_paths: list[Path] = []
        for _ in range(3):
            staging = self._staging(frames=1)
            token = staging.name[1:-5]
            final = staging.parent / token
            staging.replace(final)
            stale_paths.append(final)

        removed = proxy.collect_stale_generations(
            self.root, self.cache, max_delete=1, retire_grace_seconds=0
        )
        self.assertEqual(removed, 1)
        self.assertEqual(sum(path.exists() for path in stale_paths), 2)
        self.assertTrue(current.path.exists())

        outside = Path(self.temporary.name) / "outside-root"
        outside.mkdir()
        with self.assertRaisesRegex(ValueError, "fora do cache"):
            proxy.collect_stale_generations(outside, self.cache)

    def test_file_response_path_survives_publication_and_immediate_gc(self) -> None:
        old = self._publish(frames=1)
        resolved = proxy.locate_frame(self.ctx, "video-1", 0, proxy.FULL)
        self.assertIsNotNone(resolved)
        response = FileResponse(resolved, media_type="image/jpeg")

        proxy.publish_generation(
            self._staging(frames=2),
            self.root,
            self.cache,
            kind="full",
            job_id="replacement",
        )
        proxy.collect_stale_generations(self.root, self.cache)

        self.assertEqual(Path(response.path), resolved)
        self.assertTrue(resolved.exists())
        self.assertTrue(old.path.exists())

    def test_lru_keeps_current_when_a_staging_generation_exists(self) -> None:
        window_root = proxy.window_dir(self.ctx, "video-1", 0, 0)
        staging = proxy.new_staging_generation(window_root)
        self.assertTrue(staging.is_dir(), "staging precisa ser reservado sob a trava")
        for tier in proxy.TIERS:
            (staging / tier).mkdir()
            (staging / tier / "000000.jpg").write_bytes(b"jpeg")
        published = proxy.publish_generation(
            staging,
            window_root,
            self.cache,
            kind="window",
            job_id="published",
            start=0,
            end=0,
        )
        active_staging = proxy.new_staging_generation(window_root)

        with patch("server.proxy.CACHE_LIMIT_GB", 0):
            proxy._evict_if_needed(self.ctx)

        self.assertTrue(active_staging.exists())
        self.assertTrue(published.path.exists())
        self.assertEqual(proxy.available_ranges(self.ctx, "video-1"), [[0, 0]])

    def test_lru_unpublishes_window_index_but_preserves_resolved_frame(self) -> None:
        window_root = proxy.window_dir(self.ctx, "video-1", 4, 4)
        staging = proxy.new_staging_generation(window_root)
        for tier in proxy.TIERS:
            (staging / tier).mkdir()
            (staging / tier / "000000.jpg").write_bytes(b"jpeg")
        proxy.publish_generation(
            staging,
            window_root,
            self.cache,
            kind="window",
            job_id="published",
            start=4,
            end=4,
        )
        resolved = proxy.locate_frame(self.ctx, "video-1", 4, proxy.FULL)

        with patch("server.proxy.CACHE_LIMIT_GB", 0):
            proxy._evict_if_needed(self.ctx)

        self.assertEqual(proxy.available_ranges(self.ctx, "video-1"), [])
        self.assertTrue(resolved.exists(), "request já resolvido não pode perder o arquivo")

    def test_lru_marker_failure_keeps_published_window_discoverable(self) -> None:
        window_root = proxy.window_dir(self.ctx, "video-1", 7, 7)
        staging = proxy.new_staging_generation(window_root)
        for tier in proxy.TIERS:
            (staging / tier).mkdir()
            (staging / tier / "000000.jpg").write_bytes(b"jpeg")
        published = proxy.publish_generation(
            staging,
            window_root,
            self.cache,
            kind="window",
            job_id="marker-failure",
            start=7,
            end=7,
        )
        real_fsync = proxy._fsync_file

        def fail_retirement_marker(path: Path, data: str) -> None:
            if path.name == proxy.RETIRE_MARKER:
                raise OSError("retirement marker failed")
            real_fsync(path, data)

        with patch("server.proxy._fsync_file", side_effect=fail_retirement_marker):
            with self.assertRaisesRegex(OSError, "retirement marker failed"):
                proxy._retire_proxy_root(window_root, self.cache)

        self.assertEqual(proxy.current_generation(window_root).token, published.token)
        self.assertIsNotNone(proxy.locate_frame(self.ctx, "video-1", 7, proxy.FULL))

    def test_expired_staging_does_not_pin_lru_and_is_eventually_collected(self) -> None:
        window_root = proxy.window_dir(self.ctx, "video-1", 9, 9)
        staging = proxy.new_staging_generation(window_root)
        for tier in proxy.TIERS:
            (staging / tier).mkdir()
            (staging / tier / "000000.jpg").write_bytes(b"jpeg")
        proxy.publish_generation(
            staging,
            window_root,
            self.cache,
            kind="window",
            job_id="published-before-crash",
            start=9,
            end=9,
        )
        abandoned = proxy.new_staging_generation(window_root)
        (abandoned / "partial").mkdir()
        (abandoned / "partial" / "000000.jpg").write_bytes(b"partial")
        old = time.time() - proxy.STAGING_ORPHAN_GRACE_SECONDS - 10
        for entry in sorted(abandoned.rglob("*"), reverse=True):
            os.utime(entry, (old, old))
        os.utime(abandoned, (old, old))

        with patch("server.proxy._schedule_stale_generation_gc"):
            self.assertTrue(proxy._retire_proxy_root(window_root, self.cache))

        proxy.collect_stale_generations(
            window_root,
            self.cache,
            max_delete=8,
            retire_grace_seconds=0,
            staging_grace_seconds=0,
        )
        self.assertFalse(abandoned.exists())
        self.assertIsNone(proxy.current_generation(window_root))

    def test_operational_sweep_resumes_across_crashed_first_extractions(self) -> None:
        sweep = getattr(proxy, "sweep_cache", None)
        self.assertTrue(callable(sweep), "sweep operacional ausente")
        abandoned: list[Path] = []
        for index in range(3):
            root = proxy.proxy_dir(self.ctx, f"orphan-{index}")
            staging = proxy.new_staging_generation(root)
            (staging / "partial").mkdir()
            (staging / "partial" / "000000.jpg").write_bytes(b"partial")
            old = time.time() - proxy.STAGING_ORPHAN_GRACE_SECONDS - 10
            for entry in sorted(staging.rglob("*"), reverse=True):
                os.utime(entry, (old, old))
            os.utime(staging, (old, old))
            abandoned.append(staging)

        remaining = 3
        for _ in range(3):
            result = sweep(
                self.ctx,
                max_roots=1,
                max_delete_per_root=4,
                retire_grace_seconds=0,
                staging_grace_seconds=0,
            )
            remaining -= 1
            self.assertEqual(result["roots_scanned"], 1)
            self.assertEqual(sum(path.exists() for path in abandoned), remaining)

    def test_operational_sweep_retries_gc_trash_after_delete_failure(self) -> None:
        sweep = getattr(proxy, "sweep_cache", None)
        self.assertTrue(callable(sweep), "sweep operacional ausente")
        root = proxy.proxy_dir(self.ctx, "trash-retry")
        trash = root / (".gc-" + ("a" * 32))
        trash.mkdir(parents=True)
        (trash / "payload").write_bytes(b"garbage")

        with patch("server.proxy.shutil.rmtree", side_effect=OSError("busy")):
            result = sweep(self.ctx, max_roots=1)

        self.assertEqual(result["errors"], 1)
        self.assertTrue(trash.exists())
        recovered = sweep(self.ctx, max_roots=1)
        self.assertEqual(recovered["trash_removed"], 1)
        self.assertFalse(trash.exists())

    def test_operational_sweep_repairs_index_after_partial_eviction(self) -> None:
        sweep = getattr(proxy, "sweep_cache", None)
        self.assertTrue(callable(sweep), "sweep operacional ausente")
        broad_root, broad_staging = self._window_staging(0, 4, b"broad")
        proxy.publish_generation(
            broad_staging,
            broad_root,
            self.cache,
            kind="window",
            job_id="broad",
            start=0,
            end=4,
        )
        narrow_root, narrow_staging = self._window_staging(2, 2, b"narrow")
        proxy.publish_generation(
            narrow_staging,
            narrow_root,
            self.cache,
            kind="window",
            job_id="narrow",
            start=2,
            end=2,
        )

        with self.assertLogs("movies-screening-tool.proxy", level="WARNING"):
            with patch(
                "server.proxy._remove_window_index",
                side_effect=OSError("index busy"),
            ):
                self.assertTrue(proxy._retire_proxy_root(narrow_root, self.cache))

        self.assertIsNone(
            proxy.locate_frame(self.ctx, "video-1", 2, proxy.FULL),
            "índice stale deve virar miss, nunca fallback silencioso",
        )
        sweep(
            self.ctx,
            max_roots=8,
            max_delete_per_root=4,
            retire_grace_seconds=0,
            staging_grace_seconds=0,
        )
        repaired = proxy.locate_frame(self.ctx, "video-1", 2, proxy.FULL)
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.read_bytes(), b"broad")

    def test_lru_skips_busy_publication_lock_without_waiting(self) -> None:
        published = self._publish(frames=1)
        result: list[bool] = []

        def retire() -> None:
            result.append(proxy._retire_proxy_root(self.root, self.cache))

        with proxy._exclusive_file_lock(self.root / ".publish.lock"):
            contender = threading.Thread(target=retire)
            contender.start()
            contender.join(timeout=0.2)
            self.assertFalse(contender.is_alive(), "LRU bloqueou esperando publisher")

        self.assertEqual(result, [False])
        self.assertTrue(published.path.exists())
        self.assertEqual(proxy.current_generation(self.root).token, published.token)

    def test_gc_cannot_delete_generation_while_another_publisher_is_committing(self) -> None:
        self._publish(frames=1)
        staged = self._staging(frames=2)
        pointer_ready = threading.Event()
        allow_commit = threading.Event()
        gc_finished = threading.Event()
        errors: list[BaseException] = []
        real_fsync = proxy._fsync_file

        def pause_after_pointer_fsync(path: Path, data: str) -> None:
            real_fsync(path, data)
            if path.name.endswith(".CURRENT.part"):
                pointer_ready.set()
                if not allow_commit.wait(timeout=2):
                    raise TimeoutError("test nao liberou commit")

        def publish() -> None:
            try:
                proxy.publish_generation(
                    staged,
                    self.root,
                    self.cache,
                    kind="full",
                    job_id="concurrent",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        def collect() -> None:
            try:
                proxy.collect_stale_generations(self.root, self.cache)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                gc_finished.set()

        with patch("server.proxy._fsync_file", side_effect=pause_after_pointer_fsync):
            publisher = threading.Thread(target=publish)
            publisher.start()
            self.assertTrue(pointer_ready.wait(timeout=2))
            collector = threading.Thread(target=collect)
            collector.start()
            self.assertFalse(gc_finished.wait(timeout=0.05))
            allow_commit.set()
            publisher.join(timeout=2)
            collector.join(timeout=2)

        self.assertEqual(errors, [])
        current = proxy.current_generation(self.root)
        self.assertIsNotNone(current)
        self.assertTrue(current.path.exists())

    def test_slow_gc_deletion_does_not_hold_the_publication_lock(self) -> None:
        self._publish(frames=1)
        stale_staging = self._staging(frames=1)
        stale = stale_staging.parent / stale_staging.name[1:-5]
        stale_staging.replace(stale)
        newer_staging = self._staging(frames=1)
        newer_staging.replace(newer_staging.parent / newer_staging.name[1:-5])
        deletion_started = threading.Event()
        allow_deletion = threading.Event()
        errors: list[BaseException] = []
        real_rmtree = proxy.shutil.rmtree

        def slow_rmtree(path, *args, **kwargs):
            if Path(path).name == stale.name or Path(path).name.startswith(".gc-"):
                deletion_started.set()
                if not allow_deletion.wait(timeout=2):
                    raise TimeoutError("test nao liberou GC")
            return real_rmtree(path, *args, **kwargs)

        def collect() -> None:
            try:
                proxy.collect_stale_generations(
                    self.root, self.cache, retire_grace_seconds=0
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        new_staging = self._staging(frames=2)
        new_token = new_staging.name[1:-5]

        def publish() -> None:
            try:
                proxy.publish_generation(
                    new_staging,
                    self.root,
                    self.cache,
                    kind="full",
                    job_id="new-during-gc",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        with patch("server.proxy.shutil.rmtree", side_effect=slow_rmtree):
            collector = threading.Thread(target=collect)
            collector.start()
            self.assertTrue(deletion_started.wait(timeout=2))
            publisher = threading.Thread(target=publish)
            publisher.start()
            pointer = self.root / proxy.CURRENT_POINTER
            committed = False
            for _ in range(50):
                if pointer.read_text(encoding="utf-8").strip() == new_token:
                    committed = True
                    break
                threading.Event().wait(0.01)
            self.assertTrue(committed, "GC lento segurou a trava de publicação")
            allow_deletion.set()
            collector.join(timeout=2)
            publisher.join(timeout=2)

        self.assertEqual(errors, [])

    def test_post_commit_gc_failure_does_not_turn_publication_into_failure(self) -> None:
        old = self._publish(frames=1)
        with self.assertLogs("movies-screening-tool.proxy", level="WARNING") as logs:
            with patch(
                "server.proxy._schedule_stale_generation_gc",
                side_effect=RuntimeError("gc crashed"),
            ):
                generation = proxy.publish_generation(
                    self._staging(frames=2),
                    self.root,
                    self.cache,
                    kind="full",
                    job_id="survives-gc",
                )

        self.assertNotEqual(generation.token, old.token)
        self.assertEqual(proxy.current_generation(self.root).token, generation.token)
        self.assertIn("não foi possível agendar GC", logs.output[0])


if __name__ == "__main__":
    unittest.main()
