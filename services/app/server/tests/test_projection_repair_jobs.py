from __future__ import annotations

import unittest
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from server import durable_jobs


class ProjectionRepairJobTests(unittest.TestCase):
    def job(self):
        return {"id": "11111111-1111-1111-1111-111111111111", "lease_token": "22222222-2222-2222-2222-222222222222",
                "kind": "pipeline_projection_reconcile",
                "payload": {"object_id": "boom", "video_id": "clip", "source_identity": {"old": 1}}}

    def owned(self):
        return {"job_id": self.job()["id"], "kind": "pipeline_projection_reconcile", "worker_kind": "cpu",
                "object_id": "boom", "video_id": "clip"}

    def test_unowned_or_mismatched_job_never_opens_context_or_reconciles(self):
        from server import pipeline_projection_jobs as jobs
        for owned in [None, {**self.owned(), "kind": "video_export"}, {**self.owned(), "object_id": "other"},
                      {**self.owned(), "video_id": "other"}, {**self.owned(), "worker_kind": "gpu"}]:
            with self.subTest(owned=owned), patch.object(durable_jobs, "renew_owned_lease", return_value=owned), \
                 patch.object(jobs.workspace, "load"), patch.object(jobs.workspace, "get", return_value="config"), \
                 patch.object(jobs, "ObjectContext") as factory, patch.object(jobs, "reconcile_video") as reconcile:
                with self.assertRaises(RuntimeError):
                    jobs.run_projection_reconcile(self.job())
                factory.assert_not_called()
                reconcile.assert_not_called()

    def test_lease_lost_after_metadata_read_prevents_publication(self):
        from server import pipeline_projection_jobs as jobs
        published = []

        @contextmanager
        def lost_lease(*args, **kwargs):
            yield None

        def reconcile(ctx, video_id, **kwargs):
            guard = kwargs.get("publication_fence")
            if guard is None:
                published.append(video_id)
            else:
                with guard():
                    published.append(video_id)
            return None

        with patch.object(durable_jobs, "renew_owned_lease", return_value=self.owned()), \
             patch.object(durable_jobs, "owned_job_lease", lost_lease), \
             patch.object(jobs.workspace, "load"), patch.object(jobs.workspace, "get", return_value="config"), \
             patch.object(jobs, "ObjectContext"), patch.object(jobs, "reconcile_video", reconcile):
            with self.assertRaises(RuntimeError):
                jobs.run_projection_reconcile(self.job())
        self.assertEqual(published, [])

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

    def test_cpu_dispatch_treats_lost_repair_lease_as_cancelled(self):
        import worker
        from server.pipeline_projection_jobs import ProjectionRepairLeaseLost
        queue = MagicMock()
        queue.claim.side_effect = [self.job(), KeyboardInterrupt]
        with patch.dict(os.environ, {"DATABASE_URL": "synthetic"}), \
             patch.object(worker, "PostgresJobQueue", return_value=queue), \
             patch.object(worker, "_ProxyMaintenanceScheduler"), patch.object(worker.threading, "Thread"), \
             patch.object(worker, "run_projection_reconcile", side_effect=ProjectionRepairLeaseLost("lost")):
            with self.assertRaises(KeyboardInterrupt):
                worker.main()
        queue.finish.assert_called_once_with(self.job()["id"], self.job()["lease_token"], state="cancelled", error="lost")
        queue.retry_or_fail.assert_not_called()

    def test_every_job_opens_fresh_context_and_reconciles_current_metadata(self):
        from server import pipeline_projection_jobs as jobs
        from server.workspace import workspace
        contexts = [SimpleNamespace(marker=1), SimpleNamespace(marker=2)]
        job = self.job()
        with patch.object(workspace, "load"), patch.object(workspace, "get", return_value="config"), \
             patch.object(durable_jobs, "renew_owned_lease", return_value=self.owned()), \
             patch.object(jobs, "ObjectContext", side_effect=contexts) as factory, \
             patch.object(jobs, "reconcile_video", return_value=None) as reconcile:
            jobs.run_projection_reconcile(job)
            jobs.run_projection_reconcile(job)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual([call.args for call in reconcile.call_args_list], [(contexts[0], "clip"), (contexts[1], "clip")])


if __name__ == "__main__":
    unittest.main()
