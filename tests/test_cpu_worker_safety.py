from __future__ import annotations

import os
import tempfile
import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()
