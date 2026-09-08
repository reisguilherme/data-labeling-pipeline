from __future__ import annotations

import time
import unittest

from sam3_runner.queue_client import JobHeartbeat


class FakeClient:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str | None]] = []
        self.heartbeats: list[dict] = []

    def status(self, state: str, message: str | None = None) -> None:
        self.statuses.append((state, message))

    def heartbeat(self, lease_id: str, progress: dict) -> bool:
        self.heartbeats.append({"lease_id": lease_id, **progress})
        return True


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


if __name__ == "__main__":
    unittest.main()
