from __future__ import annotations

import unittest
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from server import durable_jobs


class ProjectionRepairJobTests(unittest.TestCase):
    def test_repair_connection_has_finite_timeout(self):
        with patch.dict(os.environ, {"DATABASE_URL": "synthetic"}), patch("psycopg.connect") as connect:
            durable_jobs.enqueue_projection_reconciles("boom", [{"video_id": "v", "source_identity": {}}])
        self.assertEqual(connect.call_args.kwargs.get("connect_timeout"), 1)

    def test_cpu_dispatch_finishes_metadata_repair_without_media_handlers(self):
        import worker
        queue = MagicMock()
        job = {"id": "repair", "kind": "pipeline_projection_reconcile", "lease_token": "token", "payload": {}}
        queue.claim.side_effect = [job, KeyboardInterrupt]
        with patch.dict(os.environ, {"DATABASE_URL": "synthetic"}), \
             patch.object(worker, "PostgresJobQueue", return_value=queue), \
             patch.object(worker, "_ProxyMaintenanceScheduler"), \
             patch.object(worker.threading, "Thread"), \
             patch.object(worker, "run_projection_reconcile", create=True, return_value={"projection_event_seq": 4}), \
             patch.object(worker, "run_video_export", side_effect=AssertionError("media processing forbidden")):
            with self.assertRaises(KeyboardInterrupt):
                worker.main()
        queue.finish.assert_called_once_with("repair", "token", state="done", result={"projection_event_seq": 4})
        queue.retry_or_fail.assert_not_called()

    def test_every_job_opens_fresh_context_and_reconciles_current_metadata(self):
        from server import pipeline_projection_jobs as jobs
        from server.workspace import workspace
        contexts = [SimpleNamespace(marker=1), SimpleNamespace(marker=2)]
        job = {"payload": {"object_id": "boom", "video_id": "clip", "source_identity": {"old": 1}}}
        with patch.object(workspace, "load"), patch.object(workspace, "get", return_value="config"), \
             patch.object(jobs, "ObjectContext", side_effect=contexts) as factory, \
             patch.object(jobs, "reconcile_video", return_value=None) as reconcile:
            jobs.run_projection_reconcile(job)
            jobs.run_projection_reconcile(job)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual([call.args for call in reconcile.call_args_list], [(contexts[0], "clip"), (contexts[1], "clip")])


if __name__ == "__main__":
    unittest.main()
