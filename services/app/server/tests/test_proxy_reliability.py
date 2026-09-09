from __future__ import annotations

import json
import os
import tempfile
import threading
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

    def test_guard_rechecks_after_pointer_is_durable_and_immediately_before_commit(self) -> None:
        staging = self._staging(frames=1)
        events: list[str] = []
        real_fsync = proxy._fsync_file
        real_replace = os.replace

        def observed_fsync(path: Path, data: str) -> None:
            real_fsync(path, data)
            if path.name.endswith(".CURRENT.part"):
                events.append("pointer-durable")

        def observed_guard() -> bool:
            events.append("guard")
            return True

        def observed_replace(source, destination) -> None:
            if Path(destination) == self.root / proxy.CURRENT_POINTER:
                events.append("commit")
            real_replace(source, destination)

        with patch("server.proxy._fsync_file", side_effect=observed_fsync), patch(
            "server.proxy.os.replace", side_effect=observed_replace
        ):
            proxy.publish_generation(
                staging,
                self.root,
                self.cache,
                kind="full",
                job_id="guarded",
                publish_guard=observed_guard,
            )

        self.assertEqual(events[-3:], ["pointer-durable", "guard", "commit"])

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

        self.assertFalse(old.path.exists())
        self.assertTrue(new.path.exists())
        self.assertTrue(concurrent_staging.exists())
        self.assertEqual(proxy.current_generation(self.root).token, new.token)

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
            self.root, self.cache, max_delete=1
        )
        self.assertEqual(removed, 1)
        self.assertEqual(sum(path.exists() for path in stale_paths), 2)
        self.assertTrue(current.path.exists())

        outside = Path(self.temporary.name) / "outside-root"
        outside.mkdir()
        with self.assertRaisesRegex(ValueError, "fora do cache"):
            proxy.collect_stale_generations(outside, self.cache)

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
                proxy.collect_stale_generations(self.root, self.cache)
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
                "server.proxy.collect_stale_generations",
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
        self.assertIn("falha ao coletar gerações antigas", logs.output[0])


if __name__ == "__main__":
    unittest.main()
