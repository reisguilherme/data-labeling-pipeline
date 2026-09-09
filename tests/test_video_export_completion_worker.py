from __future__ import annotations

import json
import os
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.app import worker
from server import export as local_export
from server.videos import VideoFile


class StopWorker(RuntimeError):
    pass


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        assert limit == 65_537
        return json.dumps({"ok": True, "digest": "abc", "replayed": False}).encode()


class VideoExportCompletionWorkerTests(unittest.TestCase):
    def _job(self) -> dict:
        return {
            "id": "11111111-1111-1111-1111-111111111111",
            "kind": "video_export",
            "lease_token": "22222222-2222-2222-2222-222222222222",
            "payload": {"object_id": "boom class", "video_id": "video/1"},
        }

    def test_callback_is_bounded_authenticated_and_encodes_path_components(self) -> None:
        send = getattr(worker, "_report_video_export_completion", None)
        self.assertTrue(callable(send), "callback de conclusao ausente")
        captured = {}

        def open_request(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response()

        with patch.dict(
            os.environ,
            {"MST_API": "http://app:8000/", "MST_WORKER_TOKEN": "worker-secret"},
        ), patch("services.app.worker.urllib.request.urlopen", side_effect=open_request):
            response = send(self._job(), self._job()["lease_token"], {"root": "/x"})

        request = captured["request"]
        self.assertEqual(
            request.full_url,
            "http://app:8000/api/objects/boom%20class/videos/video%2F1/export/complete",
        )
        self.assertEqual(captured["timeout"], 30)
        self.assertEqual(request.get_header("X-mst-worker"), "worker-secret")
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            json.loads(request.data),
            {
                "job_id": self._job()["id"],
                "lease_token": self._job()["lease_token"],
                "result": {"root": "/x"},
            },
        )
        self.assertTrue(response["ok"])

    def test_callback_ack_happens_before_done_while_heartbeat_is_alive(self) -> None:
        job = self._job()
        queue = MagicMock()
        queue.claim.side_effect = [job, StopWorker("stop test loop")]
        order: list[str] = []

        def callback(*_args):
            heartbeat = next(
                item
                for item in threading.enumerate()
                if item.name == f"lease-{job['id']}"
            )
            self.assertTrue(heartbeat.is_alive())
            order.append("callback")
            return {"ok": True}

        def finish(*_args, **_kwargs):
            order.append("finish")
            return True

        queue.finish.side_effect = finish
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://unused"}), patch(
            "services.app.worker.PostgresJobQueue", return_value=queue
        ), patch(
            "services.app.worker._ProxyMaintenanceScheduler"
        ) as scheduler, patch(
            "services.app.worker.run_video_export", return_value={"root": "/x"}
        ), patch(
            "services.app.worker._report_video_export_completion", side_effect=callback
        ):
            scheduler.return_value.maybe_start.return_value = False
            with self.assertRaisesRegex(StopWorker, "stop test loop"):
                worker.main()

        self.assertEqual(order, ["callback", "finish"])
        queue.retry_or_fail.assert_not_called()

    def test_callback_failure_retries_and_never_marks_job_done(self) -> None:
        job = self._job()
        queue = MagicMock()
        queue.claim.side_effect = [job, StopWorker("stop test loop")]
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://unused"}), patch(
            "services.app.worker.PostgresJobQueue", return_value=queue
        ), patch(
            "services.app.worker._ProxyMaintenanceScheduler"
        ) as scheduler, patch(
            "services.app.worker.run_video_export", return_value={"root": "/x"}
        ), patch(
            "services.app.worker._report_video_export_completion",
            side_effect=RuntimeError("completion rejected"),
        ):
            scheduler.return_value.maybe_start.return_value = False
            with self.assertRaisesRegex(StopWorker, "stop test loop"):
                worker.main()

        queue.finish.assert_not_called()
        queue.retry_or_fail.assert_called_once_with(
            job["id"], job["lease_token"], "completion rejected"
        )

    def test_stale_export_is_acknowledged_without_completion_callback(self) -> None:
        job = self._job()
        queue = MagicMock()
        queue.claim.side_effect = [job, StopWorker("stop test loop")]
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://unused"}), patch(
            "services.app.worker.PostgresJobQueue", return_value=queue
        ), patch(
            "services.app.worker._ProxyMaintenanceScheduler"
        ) as scheduler, patch(
            "services.app.worker.run_video_export", return_value={"stale": True}
        ), patch(
            "services.app.worker._report_video_export_completion"
        ) as callback:
            scheduler.return_value.maybe_start.return_value = False
            with self.assertRaisesRegex(StopWorker, "stop test loop"):
                worker.main()

        callback.assert_not_called()
        queue.finish.assert_called_once_with(
            job["id"], job["lease_token"], state="done", result={"stale": True}
        )


class _LocalStore:
    def __init__(self, relpath: str, entry: dict) -> None:
        import asyncio

        self._relpath = relpath
        self._entry = entry
        self._lock = asyncio.Lock()

    def entry(self, relpath: str) -> dict | None:
        return self._entry if relpath == self._relpath else None


class LocalExportCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_fallback_finalizes_before_marking_job_done(self) -> None:
        root = Path(tempfile.mkdtemp())
        source = root / "raw" / "clip.mp4"
        source.parent.mkdir()
        source.write_bytes(b"video")
        output = root / "dataset"
        output.mkdir()
        video = VideoFile("video-1", "clip.mp4", source, "clip", 5, "now")
        entry = {
            "annotation_revision": 3,
            "status": "in_progress",
            "media": {"width": 10, "height": 8},
            "intervals": [
                {
                    "segment": "seg_00",
                    "index": 0,
                    "start_frame": 0,
                    "end_frame": 0,
                    "frame_count": 1,
                    "prompt_frame": 0,
                    "bboxes": [],
                    "flags": [],
                }
            ],
        }
        store = _LocalStore(video.relpath, entry)
        index = SimpleNamespace(
            get=lambda video_id: video if video_id == video.video_id else None,
            cached_probe=lambda _video_id: entry["media"],
            resolve_path=lambda _video_id: source,
        )
        ctx = SimpleNamespace(
            object_id="boom", output_root=output, store=store, index=index
        )
        observed: list[str] = []

        def argv(_source, segment_dir, _start, _end):
            (segment_dir / "000000.jpg").write_bytes(b"not-a-real-jpeg")
            return ["ffmpeg"]

        async def run(sub, _argv):
            sub.state = "done"

        async def finalize(*args, **kwargs):
            self.assertEqual(kwargs["expected_revision"], 3)
            self.assertEqual(args[2]["segments"], ["seg_00"])
            observed.append("finalize")
            return SimpleNamespace(digest="digest", replayed=False)

        with patch("server.export.ffmpeg.resolve", return_value=SimpleNamespace(version="v")), patch(
            "server.export.ffmpeg.export_segment_argv", side_effect=argv
        ), patch("server.export.jobs.run", side_effect=run), patch(
            "server.video_export_completion.finalize_video_export", side_effect=finalize
        ):
            job = await local_export.export_video(ctx, video.video_id, entry, user="ana")
            for _ in range(100):
                if job.terminal:
                    break
                await __import__("asyncio").sleep(0.01)

        self.assertEqual(job.state, "done", job.error)
        self.assertEqual(observed, ["finalize"])


if __name__ == "__main__":
    unittest.main()
