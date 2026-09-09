from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from server import proxy
from services.app.worker import Cancelled, _remove_tree, run_proxy_full


class CpuWorkerSafetyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
