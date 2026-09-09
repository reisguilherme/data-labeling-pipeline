from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from server import proxy
from services.app.worker import (
    Cancelled,
    _remove_tree,
    main as worker_main,
    run_proxy_full,
    run_proxy_window,
)


@contextmanager
def _unlocked_video(*_args, **_kwargs):
    yield


def _owner(*, video_id: str = "video-1", relpath: str = "clip.mp4", revision: int = 2) -> dict:
    return {
        "schema_version": 1,
        "object_id": "boom",
        "video_id": video_id,
        "relpath": relpath,
        "annotation_revision": revision,
    }


def _write_owner(root: Path, owner: dict) -> None:
    (root / ".export-owner.json").write_text(
        json.dumps(owner, sort_keys=True), encoding="utf-8"
    )


class CpuWorkerSafetyTests(unittest.TestCase):
    def test_export_staging_is_lease_specific(self) -> None:
        from services.app import worker as worker_module

        root = Path("dataset") / "clip"
        first = worker_module._staging_export_root(root, "job-1", "lease-a")
        second = worker_module._staging_export_root(root, "job-1", "lease-b")
        self.assertNotEqual(first, second)

    def test_removes_only_a_child_of_the_declared_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            target = root / "proxy" / "video"
            target.mkdir(parents=True)
            (target / "frame.jpg").write_bytes(b"frame")
            _remove_tree(target, root)
            self.assertFalse(target.exists())
            self.assertTrue(root.exists())

    def test_refuses_root_and_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "cache"
            root.mkdir()
            sibling = base / "raw"
            sibling.mkdir()
            with self.assertRaisesRegex(ValueError, "recusa remover"):
                _remove_tree(root, root)
            with self.assertRaisesRegex(ValueError, "recusa remover"):
                _remove_tree(sibling, root)

    def test_cleanup_root_is_one_child_with_plain_basename(self) -> None:
        from services.app import worker as worker_module

        resolver = getattr(worker_module, "_export_root_from_basename", None)
        self.assertTrue(callable(resolver), "resolvedor seguro de export ausente")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dataset"
            output.mkdir()
            ctx = SimpleNamespace(output_root=output)
            expected = output / "clip"
            self.assertEqual(resolver(ctx, "clip"), expected)
            for unsafe in ("", ".", "..", "../clip", r"..\clip", "/tmp/clip"):
                with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                    resolver(ctx, unsafe)

    def test_cleanup_root_rejects_symlink(self) -> None:
        from services.app import worker as worker_module

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dataset"
            output.mkdir()
            ctx = SimpleNamespace(output_root=output)
            outside = Path(temporary) / "outside"
            outside.mkdir()
            link = output / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink indisponivel neste host: {exc}")
            with self.assertRaises(ValueError):
                worker_module._export_root_from_basename(ctx, "linked")

    def test_cleanup_preserves_legacy_root_without_owner_marker(self) -> None:
        from services.app import worker as worker_module

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dataset"
            root = output / "clip"
            segment = root / "seg_00"
            segment.mkdir(parents=True)
            (segment / "000000.jpg").write_bytes(b"legacy")
            owner = _owner(revision=2)
            store = MagicMock()
            store.entry.return_value = {
                "status": "no_boom",
                "annotation_revision": 3,
                "export_cleanup": {
                    "root_basename": "clip",
                    "owner": owner,
                },
            }
            ctx = SimpleNamespace(output_root=output, store=store)
            queue = MagicMock()
            queue.update_progress.return_value = True
            job = {
                "id": "cleanup-legacy",
                "payload": {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "relpath": "clip.mp4",
                    "annotation_revision": 3,
                    "root_basename": "clip",
                    "owner": owner,
                },
            }

            with patch("services.app.worker._context", return_value=ctx), patch(
                "server.durable_jobs.video_advisory_lock", _unlocked_video
            ):
                result = worker_module.run_video_export_cleanup(job, queue, "lease")

            self.assertTrue(result["deferred"])
            self.assertEqual(result["reason"], "owner_marker_missing")
            self.assertEqual((segment / "000000.jpg").read_bytes(), b"legacy")

    def test_cleanup_preserves_root_owned_by_another_video(self) -> None:
        from services.app import worker as worker_module

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dataset"
            root = output / "clip"
            segment = root / "seg_00"
            segment.mkdir(parents=True)
            (segment / "000000.jpg").write_bytes(b"other")
            expected_owner = _owner(revision=2)
            _write_owner(
                root,
                _owner(video_id="other-video", relpath="other/clip.mp4", revision=2),
            )
            store = MagicMock()
            store.entry.return_value = {
                "status": "no_boom",
                "annotation_revision": 3,
                "export_cleanup": {
                    "root_basename": "clip",
                    "owner": expected_owner,
                },
            }
            ctx = SimpleNamespace(output_root=output, store=store)
            queue = MagicMock()
            queue.update_progress.return_value = True
            job = {
                "id": "cleanup-wrong-owner",
                "payload": {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "relpath": "clip.mp4",
                    "annotation_revision": 3,
                    "root_basename": "clip",
                    "owner": expected_owner,
                },
            }

            with patch("services.app.worker._context", return_value=ctx), patch(
                "server.durable_jobs.video_advisory_lock", _unlocked_video
            ):
                result = worker_module.run_video_export_cleanup(job, queue, "lease")

            self.assertTrue(result["deferred"])
            self.assertEqual(result["reason"], "owner_mismatch")
            self.assertEqual((segment / "000000.jpg").read_bytes(), b"other")

    def test_cleanup_deletes_only_direct_segments_and_is_idempotent(self) -> None:
        from services.app import worker as worker_module

        cleanup = getattr(worker_module, "run_video_export_cleanup", None)
        self.assertTrue(callable(cleanup), "handler video_export_cleanup ausente")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dataset"
            root = output / "clip"
            segment = root / "seg_00"
            segment.mkdir(parents=True)
            owner = _owner(revision=2)
            _write_owner(root, owner)
            (segment / "000000.jpg").write_bytes(b"jpeg")
            (root / "notes.txt").write_text("keep", encoding="utf-8")
            sibling_segment = output / "other" / "seg_00"
            sibling_segment.mkdir(parents=True)
            store = MagicMock()
            store.entry.return_value = {
                "status": "no_boom",
                "annotation_revision": 3,
                "export_cleanup": {"root_basename": "clip", "owner": owner},
            }
            ctx = SimpleNamespace(output_root=output, store=store)
            queue = MagicMock()
            queue.update_progress.return_value = True
            job = {
                "id": "cleanup-1",
                "payload": {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "relpath": "clip.mp4",
                    "annotation_revision": 3,
                    "root_basename": "clip",
                    "owner": owner,
                },
            }
            with patch("services.app.worker._context", return_value=ctx), patch(
                "server.durable_jobs.video_advisory_lock", _unlocked_video
            ):
                first = cleanup(job, queue, "lease")
                second = cleanup(job, queue, "lease")

            self.assertEqual(first["removed"], ["seg_00"])
            self.assertEqual(second["removed"], [])
            self.assertTrue((root / "notes.txt").exists())
            self.assertTrue(sibling_segment.exists())

    def test_cleanup_checks_cancel_and_revision_inside_shared_lock(self) -> None:
        from services.app import worker as worker_module

        cleanup = getattr(worker_module, "run_video_export_cleanup", None)
        self.assertTrue(callable(cleanup), "handler video_export_cleanup ausente")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dataset"
            segment = output / "clip" / "seg_00"
            segment.mkdir(parents=True)
            store = MagicMock()
            store.entry.return_value = {
                "status": "no_boom",
                "annotation_revision": 4,
                "export_cleanup": {"root_basename": "clip"},
            }
            ctx = SimpleNamespace(output_root=output, store=store)
            job = {
                "id": "cleanup-1",
                "payload": {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "relpath": "clip.mp4",
                    "annotation_revision": 3,
                    "root_basename": "clip",
                },
            }
            queue = MagicMock()
            queue.update_progress.return_value = True
            with patch("services.app.worker._context", return_value=ctx), patch(
                "server.durable_jobs.video_advisory_lock", _unlocked_video
            ):
                result = cleanup(job, queue, "lease")
            self.assertTrue(result["stale"])
            self.assertTrue(segment.exists())
            store.load.assert_called()

            queue.update_progress.return_value = False
            with patch("services.app.worker._context", return_value=ctx), patch(
                "server.durable_jobs.video_advisory_lock", _unlocked_video
            ), self.assertRaises(Cancelled):
                cleanup(job, queue, "lease")
            self.assertTrue(segment.exists())

    def test_cleanup_releases_advisory_lock_before_recursive_delete(self) -> None:
        from services.app import worker as worker_module

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dataset"
            segment = output / "clip" / "seg_00"
            segment.mkdir(parents=True)
            owner = _owner(revision=2)
            _write_owner(output / "clip", owner)
            (segment / "000000.jpg").write_bytes(b"jpeg")
            store = MagicMock()
            store.entry.return_value = {
                "status": "no_boom",
                "annotation_revision": 3,
                "export_cleanup": {"root_basename": "clip", "owner": owner},
            }
            ctx = SimpleNamespace(output_root=output, store=store)
            queue = MagicMock()
            queue.update_progress.return_value = True
            job = {
                "id": "cleanup-1",
                "payload": {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "relpath": "clip.mp4",
                    "annotation_revision": 3,
                    "root_basename": "clip",
                    "owner": owner,
                },
            }
            held = False

            @contextmanager
            def tracked_lock(*_args, **_kwargs):
                nonlocal held
                held = True
                try:
                    yield
                finally:
                    held = False

            original_remove = worker_module._remove_tree

            def checked_remove(path, root):
                self.assertFalse(held, "recursive delete segurou o advisory lock")
                return original_remove(path, root)

            with (
                patch("services.app.worker._context", return_value=ctx),
                patch("server.durable_jobs.video_advisory_lock", tracked_lock),
                patch("services.app.worker._remove_tree", side_effect=checked_remove),
            ):
                result = worker_module.run_video_export_cleanup(job, queue, "lease")

            self.assertEqual(result["removed"], ["seg_00"])
            self.assertFalse(segment.exists())
            store.load.assert_called()

    def test_stale_video_export_never_touches_newer_segments(self) -> None:
        from services.app import worker as worker_module

        export = worker_module.run_video_export
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "dataset"
            newer = output / "clip" / "seg_00" / "new.jpg"
            newer.parent.mkdir(parents=True)
            newer.write_bytes(b"new")
            source = base / "clip.mp4"
            source.write_bytes(b"video")
            video = SimpleNamespace(video_id="video-1", relpath="clip.mp4")
            store = MagicMock()
            store.entry.return_value = {
                "status": "no_boom",
                "annotation_revision": 6,
                "intervals": [],
            }
            ctx = SimpleNamespace(
                object_id="boom",
                output_root=output,
                store=store,
                index=SimpleNamespace(get=lambda _id: video),
            )
            queue = MagicMock()
            queue.update_progress.return_value = True
            job = {
                "id": "export-5",
                "payload": {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "annotation_revision": 5,
                    "root_basename": "clip",
                },
            }
            with patch("services.app.worker._context", return_value=ctx), patch(
                "server.durable_jobs.video_advisory_lock", _unlocked_video
            ), patch("services.app.worker._run_ffmpeg") as ffmpeg:
                result = export(job, queue, "lease")

            self.assertTrue(result["stale"])
            ffmpeg.assert_not_called()
            self.assertEqual(newer.read_bytes(), b"new")

    def test_export_job_cannot_target_another_root(self) -> None:
        from services.app import worker as worker_module

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "dataset"
            victim = output / "victim" / "seg_00" / "000000.jpg"
            victim.parent.mkdir(parents=True)
            victim.write_bytes(b"victim")
            source = base / "clip.mp4"
            source.write_bytes(b"video")
            video = SimpleNamespace(video_id="video-1", relpath="clip.mp4", name="clip")
            entry = {
                "status": "in_progress",
                "annotation_revision": 5,
                "intervals": [{"segment": "seg_00", "frame_count": 1}],
            }
            ctx = SimpleNamespace(
                object_id="boom",
                output_root=output,
                store=MagicMock(entry=MagicMock(return_value=entry)),
                index=SimpleNamespace(
                    get=lambda _id: video,
                    cached_probe=lambda _id: {},
                ),
            )
            queue = MagicMock()
            queue.update_progress.return_value = True
            job = {
                "id": "malicious-export",
                "payload": {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "annotation_revision": 5,
                    "root_basename": "victim",
                    "owner": _owner(revision=5),
                },
            }

            with patch("services.app.worker._context", return_value=ctx), patch(
                "server.durable_jobs.video_advisory_lock", _unlocked_video
            ), patch("services.app.worker._run_ffmpeg") as ffmpeg, self.assertRaisesRegex(
                ValueError, "corresponde ao video"
            ):
                worker_module.run_video_export(job, queue, "lease")

            ffmpeg.assert_not_called()
            self.assertEqual(victim.read_bytes(), b"victim")

    def test_export_rechecks_revision_after_extraction_before_publishing(self) -> None:
        from services.app import worker as worker_module

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "dataset"
            final_frame = output / "clip__video-1" / "seg_00" / "new.jpg"
            final_frame.parent.mkdir(parents=True)
            final_frame.write_bytes(b"new")
            _write_owner(output / "clip__video-1", _owner(revision=4))
            source = base / "clip.mp4"
            source.write_bytes(b"video")
            video = SimpleNamespace(video_id="video-1", relpath="clip.mp4", name="clip")
            current = {
                "status": "in_progress",
                "annotation_revision": 5,
                "media": {"width": 640, "height": 360},
                "intervals": [
                    {
                        "segment": "seg_00",
                        "frame_count": 1,
                        "start_frame": 0,
                        "end_frame": 0,
                    }
                ],
            }
            stale = {
                "status": "no_boom",
                "annotation_revision": 6,
                "intervals": [],
            }
            store = MagicMock()
            # A seleção da raiz consulta o mesmo snapshot carregado antes de o
            # worker liberar o lock; a terceira leitura simula a edição concorrente.
            store.entry.side_effect = [current, current, stale]
            ctx = SimpleNamespace(
                object_id="boom",
                output_root=output,
                store=store,
                index=SimpleNamespace(
                    get=lambda _id: video,
                    resolve_path=lambda _id: source,
                    cached_probe=lambda _id: {},
                ),
            )
            queue = MagicMock()
            queue.update_progress.return_value = True
            job = {
                "id": "export-5",
                "payload": {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "annotation_revision": 5,
                    "root_basename": "clip__video-1",
                },
            }

            def prepare(_source, segment_dir, _start, _end):
                segment_dir.mkdir(parents=True, exist_ok=True)
                (segment_dir / "000000.jpg").write_bytes(b"staged")
                return ["fake-ffmpeg"]

            with (
                patch("services.app.worker._context", return_value=ctx),
                patch("server.durable_jobs.video_advisory_lock", _unlocked_video),
                patch("server.ffmpeg.resolve", return_value=SimpleNamespace(version="test")),
                patch("server.ffmpeg.export_segment_argv", side_effect=prepare),
                patch("server.export._frame_size", return_value=(640, 360)),
                patch("server.export.write_prompt_json"),
                patch("services.app.worker._run_ffmpeg"),
                patch("server.export.clean_segments") as clean_segments,
            ):
                result = worker_module.run_video_export(job, queue, "lease")

            self.assertTrue(result["stale"])
            self.assertEqual(final_frame.read_bytes(), b"new")
            self.assertEqual(
                json.loads(
                    (output / "clip__video-1" / ".export-owner.json").read_text(
                        encoding="utf-8"
                    )
                )["annotation_revision"],
                4,
            )
            clean_segments.assert_not_called()
            self.assertFalse(any(output.glob(".clip__video-1.export-*.part")))

    def test_export_releases_advisory_lock_before_recursive_gc(self) -> None:
        from services.app import worker as worker_module

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "dataset"
            old = output / "clip__video-1" / "seg_00" / "old.jpg"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"old")
            _write_owner(output / "clip__video-1", _owner(revision=4))
            source = base / "clip.mp4"
            source.write_bytes(b"video")
            video = SimpleNamespace(video_id="video-1", relpath="clip.mp4", name="clip")
            entry = {
                "status": "in_progress",
                "annotation_revision": 5,
                "media": {"width": 640, "height": 360},
                "intervals": [
                    {
                        "segment": "seg_00",
                        "frame_count": 1,
                        "start_frame": 0,
                        "end_frame": 0,
                    }
                ],
            }
            store = MagicMock()
            store.entry.return_value = entry
            ctx = SimpleNamespace(
                object_id="boom",
                output_root=output,
                store=store,
                index=SimpleNamespace(
                    get=lambda _id: video,
                    resolve_path=lambda _id: source,
                    cached_probe=lambda _id: {},
                ),
            )
            queue = MagicMock()
            queue.update_progress.return_value = True
            job = {
                "id": "export-5",
                "payload": {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "annotation_revision": 5,
                    "root_basename": "clip__video-1",
                },
            }
            held = False

            @contextmanager
            def tracked_lock(*_args, **_kwargs):
                nonlocal held
                held = True
                try:
                    yield
                finally:
                    held = False

            original_remove = worker_module._remove_tree

            def checked_remove(path, root):
                self.assertFalse(held, "recursive delete segurou o advisory lock")
                return original_remove(path, root)

            def prepare(_source, segment_dir, _start, _end):
                (segment_dir / "000000.jpg").write_bytes(b"new")
                return ["fake-ffmpeg"]

            with (
                patch("services.app.worker._context", return_value=ctx),
                patch("server.durable_jobs.video_advisory_lock", tracked_lock),
                patch("server.ffmpeg.resolve", return_value=SimpleNamespace(version="test")),
                patch("server.ffmpeg.export_segment_argv", side_effect=prepare),
                patch("server.export._frame_size", return_value=(640, 360)),
                patch("server.export.write_prompt_json"),
                patch("server.export.clean_segments", side_effect=AssertionError("slow gc")),
                patch("services.app.worker._run_ffmpeg"),
                patch("services.app.worker._remove_tree", side_effect=checked_remove),
            ):
                result = worker_module.run_video_export(job, queue, "lease")

            self.assertEqual(result["annotation_revision"], 5)
            self.assertEqual(result["owner"], _owner(revision=5))
            self.assertEqual(
                (output / "clip__video-1" / "seg_00" / "000000.jpg").read_bytes(),
                b"new",
            )
            self.assertEqual(
                json.loads(
                    (output / "clip__video-1" / ".export-owner.json").read_text(
                        encoding="utf-8"
                    )
                ),
                _owner(revision=5),
            )
            self.assertFalse(old.exists())

    def test_owner_marker_failure_rolls_back_published_segments(self) -> None:
        from services.app import worker as worker_module

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "dataset"
            root = output / "clip__video-1"
            old = root / "seg_00" / "old.jpg"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"old")
            _write_owner(root, _owner(revision=4))
            source = base / "clip.mp4"
            source.write_bytes(b"video")
            video = SimpleNamespace(video_id="video-1", relpath="clip.mp4", name="clip")
            entry = {
                "status": "in_progress",
                "annotation_revision": 5,
                "media": {"width": 640, "height": 360},
                "intervals": [
                    {
                        "segment": "seg_00",
                        "frame_count": 1,
                        "start_frame": 0,
                        "end_frame": 0,
                    }
                ],
            }
            store = MagicMock()
            store.entry.return_value = entry
            ctx = SimpleNamespace(
                object_id="boom",
                output_root=output,
                store=store,
                index=SimpleNamespace(
                    get=lambda _id: video,
                    resolve_path=lambda _id: source,
                    cached_probe=lambda _id: {},
                ),
            )
            queue = MagicMock()
            queue.update_progress.return_value = True
            job = {
                "id": "export-marker-failure",
                "payload": {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "annotation_revision": 5,
                    "root_basename": root.name,
                    "owner": _owner(revision=5),
                },
            }

            def prepare(_source, segment_dir, _start, _end):
                (segment_dir / "000000.jpg").write_bytes(b"new")
                return ["fake-ffmpeg"]

            with (
                patch("services.app.worker._context", return_value=ctx),
                patch("server.durable_jobs.video_advisory_lock", _unlocked_video),
                patch("server.ffmpeg.resolve", return_value=SimpleNamespace(version="test")),
                patch("server.ffmpeg.export_segment_argv", side_effect=prepare),
                patch("server.export._frame_size", return_value=(640, 360)),
                patch("server.export.write_prompt_json"),
                patch("services.app.worker._run_ffmpeg"),
                patch(
                    "server.export.write_export_owner",
                    side_effect=RuntimeError("marker falhou"),
                ),
                self.assertRaisesRegex(RuntimeError, "marker falhou"),
            ):
                worker_module.run_video_export(job, queue, "lease")

            self.assertEqual(old.read_bytes(), b"old")
            self.assertFalse((root / "seg_00" / "000000.jpg").exists())
            self.assertEqual(
                json.loads((root / ".export-owner.json").read_text(encoding="utf-8")),
                _owner(revision=4),
            )
            self.assertFalse(any(output.glob(".clip__video-1.export-*.part")))

    def test_failed_proxy_replacement_preserves_published_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            source = Path(temporary) / "video.mp4"
            source.write_bytes(b"video")
            ctx = SimpleNamespace(
                cache_dir=cache,
                index=SimpleNamespace(
                    resolve_path=lambda _video_id: source,
                    update_frame_count=MagicMock(),
                ),
            )
            root = proxy.proxy_dir(ctx, "video-1")
            staging = proxy.new_staging_generation(root)
            for tier in proxy.TIERS:
                (staging / tier).mkdir(parents=True)
                (staging / tier / "000000.jpg").write_bytes(b"old")
            old = proxy.publish_generation(
                staging, root, cache, kind="full", job_id="old-job"
            )
            pointer_before = (root / proxy.CURRENT_POINTER).read_bytes()
            old_marker_before = (old.path / proxy.COMPLETE_MARKER).read_bytes()

            job = {
                "id": "job-2",
                "payload": {"object_id": "boom", "video_id": "video-1", "frame_count": 2},
            }
            with patch("services.app.worker._context", return_value=ctx), patch(
                "services.app.worker._run_ffmpeg", side_effect=RuntimeError("ffmpeg falhou")
            ):
                with self.assertRaisesRegex(RuntimeError, "ffmpeg falhou"):
                    run_proxy_full(job, MagicMock(), "lease-token")

            self.assertEqual((root / proxy.CURRENT_POINTER).read_bytes(), pointer_before)
            self.assertEqual((old.path / proxy.COMPLETE_MARKER).read_bytes(), old_marker_before)
            self.assertEqual(proxy.is_complete(ctx, "video-1"), 1)
            self.assertEqual(list((root / proxy.GENERATIONS_DIR).glob(".*.part")), [])

    def test_proxy_publish_is_fenced_by_current_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            source = Path(temporary) / "video.mp4"
            source.write_bytes(b"video")
            ctx = SimpleNamespace(
                cache_dir=cache,
                index=SimpleNamespace(
                    resolve_path=lambda _video_id: source,
                    update_frame_count=MagicMock(),
                ),
            )
            queue = MagicMock()
            queue.update_progress.return_value = False

            def produce(argv, *_args, **_kwargs):
                outputs = [Path(value).parent for value in argv if "%06d.jpg" in str(value)]
                for output in outputs:
                    output.mkdir(parents=True, exist_ok=True)
                    (output / "000000.jpg").write_bytes(b"jpeg")

            job = {
                "id": "job-2",
                "payload": {"object_id": "boom", "video_id": "video-1", "frame_count": 1},
            }
            with patch("services.app.worker._context", return_value=ctx), patch(
                "services.app.worker._run_ffmpeg", side_effect=produce
            ):
                with self.assertRaisesRegex(Cancelled, "cancelamento"):
                    run_proxy_full(job, queue, "lease-token")

            self.assertFalse((proxy.proxy_dir(ctx, "video-1") / proxy.CURRENT_POINTER).exists())

    def test_proxy_commit_revalidates_lease_after_pointer_is_prepared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            source = Path(temporary) / "video.mp4"
            source.write_bytes(b"video")
            ctx = SimpleNamespace(
                cache_dir=cache,
                index=SimpleNamespace(
                    resolve_path=lambda _video_id: source,
                    update_frame_count=MagicMock(),
                ),
            )
            queue = MagicMock()
            queue.update_progress.side_effect = [True, False]

            def produce(argv, *_args, **_kwargs):
                outputs = [Path(value).parent for value in argv if "%06d.jpg" in str(value)]
                for output in outputs:
                    output.mkdir(parents=True, exist_ok=True)
                    (output / "000000.jpg").write_bytes(b"jpeg")

            job = {
                "id": "job-2",
                "payload": {"object_id": "boom", "video_id": "video-1", "frame_count": 1},
            }
            with patch("services.app.worker._context", return_value=ctx), patch(
                "services.app.worker._run_ffmpeg", side_effect=produce
            ):
                with self.assertRaisesRegex(Exception, "cancelamento"):
                    run_proxy_full(job, queue, "lease-token")

            self.assertEqual(queue.update_progress.call_count, 2)
            self.assertFalse((proxy.proxy_dir(ctx, "video-1") / proxy.CURRENT_POINTER).exists())

    def test_post_commit_lru_failure_does_not_fail_window_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            source = Path(temporary) / "video.mp4"
            source.write_bytes(b"video")
            ctx = SimpleNamespace(
                cache_dir=cache,
                index=SimpleNamespace(resolve_path=lambda _video_id: source),
            )
            queue = MagicMock()
            queue.update_progress.return_value = True

            def produce(argv, *_args, **_kwargs):
                outputs = [Path(value).parent for value in argv if "%06d.jpg" in str(value)]
                for output in outputs:
                    output.mkdir(parents=True, exist_ok=True)
                    (output / "000000.jpg").write_bytes(b"jpeg")

            job = {
                "id": "window-job",
                "payload": {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "start": 0,
                    "end": 0,
                },
            }
            with self.assertLogs("services.app.worker", level="WARNING") as logs:
                with patch("services.app.worker._context", return_value=ctx), patch(
                    "services.app.worker._run_ffmpeg", side_effect=produce
                ), patch(
                    "server.proxy._evict_if_needed",
                    side_effect=RuntimeError("lru crashed"),
                ):
                    result = run_proxy_window(job, queue, "lease-token")

            self.assertEqual(result["frames"], 1)
            self.assertIn("falha na manutenção LRU", logs.output[0])
            self.assertIsNotNone(proxy.locate_frame(ctx, "video-1", 0, proxy.FULL))

    def test_lost_lease_during_finalization_never_stops_worker_loop(self) -> None:
        class StopWorker(RuntimeError):
            pass

        jobs = [
            {
                "id": "cancelled-job",
                "kind": "proxy_full",
                "lease_token": "cancelled-token",
            },
            {
                "id": "failed-job",
                "kind": "proxy_full",
                "lease_token": "failed-token",
            },
        ]
        queue = MagicMock()
        queue.claim.side_effect = [*jobs, StopWorker("stop test loop")]
        queue.finish.side_effect = RuntimeError("lease perdido")
        queue.retry_or_fail.side_effect = RuntimeError("lease perdido")

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://unused"}), patch(
            "services.app.worker.PostgresJobQueue", return_value=queue
        ), patch(
            "services.app.worker.run_proxy_full",
            side_effect=[Cancelled("cancelled"), RuntimeError("lease perdido")],
        ), self.assertLogs("pipeline.cpu-worker", level="WARNING"):
            with self.assertRaisesRegex(StopWorker, "stop test loop"):
                worker_main()

        queue.finish.assert_called_once()
        queue.retry_or_fail.assert_called_once()
        self.assertEqual(queue.claim.call_count, 3)

    def test_worker_runs_proxy_sweep_at_startup_and_periodically_while_idle(self) -> None:
        class StopWorker(RuntimeError):
            pass

        queue = MagicMock()
        first_run = threading.Event()
        second_run = threading.Event()
        calls = 0

        def maintenance() -> None:
            nonlocal calls
            calls += 1
            (first_run if calls == 1 else second_run).set()

        claims = 0

        def claim(**_kwargs):
            nonlocal claims
            claims += 1
            expected = first_run if claims == 1 else second_run
            self.assertTrue(expected.wait(timeout=1), "maintenance nao foi agendada")
            if claims == 1:
                return None
            raise StopWorker("stop test loop")

        queue.claim.side_effect = claim
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://unused"}), patch(
            "services.app.worker.PostgresJobQueue", return_value=queue
        ), patch(
            "services.app.worker._run_proxy_maintenance", side_effect=maintenance
        ), patch(
            "services.app.worker.time.monotonic", side_effect=[0.0, 301.0]
        ), patch(
            "services.app.worker.time.sleep"
        ):
            with self.assertRaisesRegex(StopWorker, "stop test loop"):
                worker_main()

        self.assertEqual(calls, 2)

    def test_blocked_proxy_gc_rmtree_never_delays_job_claim(self) -> None:
        class StopWorker(RuntimeError):
            pass

        started = threading.Event()
        release = threading.Event()
        claimed = threading.Event()
        errors: list[BaseException] = []

        def blocked_rmtree(_path) -> None:
            started.set()
            release.wait(timeout=2)

        def maintenance() -> None:
            proxy.shutil.rmtree(Path("maintenance-tombstone"))

        queue = MagicMock()

        def claim(**_kwargs):
            claimed.set()
            raise StopWorker("stop test loop")

        queue.claim.side_effect = claim

        def run_worker() -> None:
            try:
                worker_main()
            except BaseException as exc:  # surfaced below
                errors.append(exc)

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://unused"}), patch(
            "services.app.worker.PostgresJobQueue", return_value=queue
        ), patch(
            "services.app.worker._run_proxy_maintenance", side_effect=maintenance
        ), patch(
            "server.proxy.shutil.rmtree", side_effect=blocked_rmtree
        ):
            worker = threading.Thread(target=run_worker)
            worker.start()
            try:
                self.assertTrue(started.wait(timeout=1))
                self.assertTrue(
                    claimed.wait(timeout=0.2),
                    "claim ficou bloqueado pela maintenance",
                )
            finally:
                release.set()
                worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StopWorker)

    def test_proxy_maintenance_scheduler_is_single_flight(self) -> None:
        from services.app import worker as worker_module

        scheduler_type = getattr(worker_module, "_ProxyMaintenanceScheduler", None)
        self.assertTrue(callable(scheduler_type), "scheduler de maintenance ausente")
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def maintenance() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                release.wait(timeout=2)

        scheduler = scheduler_type(maintenance, interval_seconds=300)
        self.assertTrue(scheduler.maybe_start(0.0))
        self.assertTrue(started.wait(timeout=1))
        self.assertFalse(scheduler.maybe_start(301.0))
        self.assertEqual(calls, 1)
        release.set()
        self.assertTrue(scheduler.wait(timeout=1))
        self.assertTrue(scheduler.maybe_start(301.0))
        self.assertTrue(scheduler.wait(timeout=1))
        self.assertEqual(calls, 2)

    def test_proxy_maintenance_never_reloads_workspace_during_object_purge(
        self,
    ) -> None:
        from server.config import settings
        from server.workspace import ObjectConfig, Workspace
        from services.app import worker as worker_module

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            videos_root = root / "boom" / "raw"
            output_root = root / "boom" / "dataset"
            videos_root.mkdir(parents=True)
            (output_root / "_cache" / "proxy").mkdir(parents=True)
            config = ObjectConfig(
                object_id="boom",
                display_name="Boom",
                label="boom",
                videos_root=videos_root,
                output_root=output_root,
                archived=True,
            )
            registry = root / "objects.json"
            registry.write_text(
                json.dumps({"schema_version": 2, "objects": [config.to_json(root)]}),
                encoding="utf-8",
            )

            test_workspace = Workspace()
            purge_reached_save = threading.Event()
            release_save = threading.Event()
            purge_errors: list[BaseException] = []

            with patch.object(settings, "workspace_root", root), patch.object(
                settings, "legacy", None
            ):
                test_workspace.load()
                original_save = test_workspace.save

                def blocked_save() -> None:
                    purge_reached_save.set()
                    if not release_save.wait(timeout=2):
                        raise TimeoutError("purge save was not released")
                    original_save()

                job = {
                    "id": "purge-1",
                    "payload": {
                        "object_id": "boom",
                        "actor": "guilherme",
                        "managed_paths": [str(videos_root), str(output_root)],
                    },
                }
                inventory = root / "purge-inventory.json"

                def purge() -> None:
                    try:
                        worker_module.run_object_purge(job, MagicMock(), "lease")
                    except BaseException as exc:  # surfaced below
                        purge_errors.append(exc)

                with patch.dict(
                    os.environ,
                    {
                        "MST_WORKSPACE": str(root),
                        "DATABASE_URL": "postgresql://unused",
                    },
                ), patch(
                    "server.workspace.workspace", test_workspace
                ), patch.object(
                    test_workspace, "save", side_effect=blocked_save
                ), patch(
                    "services.app.worker.MinioBlobStore.from_env", return_value=None
                ), patch(
                    "server.object_lifecycle.write_purge_inventory",
                    return_value=(inventory, {"file_count": 0, "total_bytes": 0}),
                ), patch(
                    "server.object_lifecycle.preserve_exported_datasets",
                    return_value=[],
                ), patch(
                    "server.object_lifecycle.delete_project_records", return_value={}
                ):
                    purge_thread = threading.Thread(target=purge)
                    purge_thread.start()
                    try:
                        self.assertTrue(
                            purge_reached_save.wait(timeout=1),
                            f"purge did not reach save: {purge_errors!r}",
                        )
                        with patch.object(
                            test_workspace, "load", wraps=test_workspace.load
                        ) as load, patch.object(
                            test_workspace, "list", wraps=test_workspace.list
                        ) as list_objects, patch.object(
                            test_workspace, "invalidate", wraps=test_workspace.invalidate
                        ) as invalidate, patch.object(
                            test_workspace, "context", wraps=test_workspace.context
                        ) as context:
                            worker_module._run_proxy_maintenance()
                            load.assert_not_called()
                            list_objects.assert_not_called()
                            invalidate.assert_not_called()
                            context.assert_not_called()
                    finally:
                        release_save.set()
                        purge_thread.join(timeout=2)

            self.assertFalse(purge_thread.is_alive())
            self.assertEqual(purge_errors, [])
            saved = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(saved["objects"], [])


if __name__ == "__main__":
    unittest.main()
