from __future__ import annotations

import time
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sam3_runner.queue_client import JobHeartbeat, process_job
from sam3_runner.segment import PropagationCancelled


class FakeClient:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str | None]] = []
        self.heartbeats: list[dict] = []
        self.finishes: list[tuple[str, str, dict | None, str | None]] = []

    def status(self, state: str, message: str | None = None) -> None:
        self.statuses.append((state, message))

    def heartbeat(self, lease_id: str, progress: dict) -> bool:
        self.heartbeats.append({"lease_id": lease_id, **progress})
        return True

    def finish(
        self,
        lease_id: str,
        state: str,
        result: dict | None,
        error: str | None,
    ) -> None:
        self.finishes.append((lease_id, state, result, error))


class JobHeartbeatTests(unittest.TestCase):
    def test_renews_lease_and_publishes_busy_status_while_segment_runs(self) -> None:
        client = FakeClient()
        progress = {
            "segments_done": 0,
            "segments_total": 1,
            "frames_done": 4,
            "current": "seg_00",
        }
        heartbeat = JobHeartbeat(client, "lease-1", lambda: progress, interval=0.01)

        heartbeat.start()
        time.sleep(0.035)
        heartbeat.stop()

        self.assertGreaterEqual(len(client.heartbeats), 2)
        self.assertTrue(all(item["current"] == "seg_00" for item in client.heartbeats))
        self.assertTrue(any(state == "busy" for state, _ in client.statuses))
        self.assertFalse(heartbeat.cancel_requested)

    def test_process_job_acknowledges_cancel_detected_at_frame_boundary(self) -> None:
        class ControlledHeartbeat:
            current = None

            def __init__(self, *_args, **_kwargs) -> None:
                self.cancel_requested = False
                type(self).current = self

            def start(self) -> None:
                return None

            def stop(self) -> None:
                return None

            def raise_if_lease_lost(self) -> None:
                return None

        def propagate(*_args, on_frame, **_kwargs):
            ControlledHeartbeat.current.cancel_requested = True
            if on_frame(0) is False:
                raise PropagationCancelled("cancelamento solicitado")
            return SimpleNamespace(
                status="done",
                frames_written=3,
                error=None,
                to_json=lambda: {
                    "status": "done",
                    "artifacts": {},
                    "objects": [],
                },
            )

        segment_dir = Path(tempfile.mkdtemp()) / "seg_00"
        segment_dir.mkdir()
        prompt = SimpleNamespace(
            frame_count=3,
            marker_path=segment_dir / "_sam3" / "run.json",
        )
        cfg = SimpleNamespace(
            lease_seconds=180,
            params=lambda: {"runner_version": "test"},
        )
        client = FakeClient()
        job = {
            "lease_id": "lease-1",
            "segments": [{"segment": "seg_00", "dir": str(segment_dir)}],
            "force": False,
        }

        with patch("sam3_runner.queue_client.JobHeartbeat", ControlledHeartbeat), patch(
            "sam3_runner.prompt.load_prompt", return_value=prompt
        ), patch("sam3_runner.segment.should_skip", return_value=False), patch(
            "sam3_runner.segment.run_segment", side_effect=propagate
        ), patch("sam3_runner.model.autocast_context", return_value=nullcontext()):
            process_job(
                client,
                job,
                cfg,
                predictor=object(),
                classes=SimpleNamespace(names=[]),
            )

        self.assertEqual([call[1] for call in client.finishes], ["cancelled"])


if __name__ == "__main__":
    unittest.main()
