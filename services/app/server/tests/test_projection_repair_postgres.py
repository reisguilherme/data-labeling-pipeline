"""Real PostgreSQL ownership checks around projection publication."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server import durable_jobs, pipeline_projection as projection, pipeline_projection_jobs as jobs
from server import pipeline_reconcile as reconciler
from server.pipeline_state import PipelineSnapshot, PipelineSource
from server.tests import test_pipeline_projection_postgres as fixtures


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL ausente")
class ProjectionRepairPostgresTests(unittest.TestCase):
    tearDown = fixtures.PipelineProjectionPostgresTests.tearDown
    _connect = fixtures.PipelineProjectionPostgresTests._connect

    def setUp(self):
        fixtures.PipelineProjectionPostgresTests.setUp(self)
        migration = Path(__file__).resolve().parents[4] / "migrations" / "001_initial.sql"
        self.token = "22222222-2222-2222-2222-222222222222"
        payload = {"object_id": "boom", "video_id": "v", "source_identity": {"stale": 1}}
        with self._connect() as connection:
            connection.execute(migration.read_text(encoding="utf-8"))
            row = connection.execute(
                """INSERT INTO jobs(kind, worker_kind, state, payload, lease_token, lease_expires_at)
                   VALUES ('pipeline_projection_reconcile', 'cpu', 'running', %s::jsonb, %s::uuid, now()+interval '3 minutes')
                   RETURNING id::text""", (json.dumps(payload), self.token)).fetchone()
        self.job = {"id": row[0], "kind": "pipeline_projection_reconcile", "lease_token": self.token, "payload": payload}
        self.source = PipelineSource({"revision": 1}, PipelineSnapshot(stage="triage", status="pending"))

    @contextmanager
    def execution(self, derive=None):
        ctx = SimpleNamespace(object_id="boom", config=SimpleNamespace(archived=False), output_root=Path("/synthetic"),
                              index=SimpleNamespace(get=lambda _: SimpleNamespace(relpath="v.mp4")),
                              store=SimpleNamespace(load=lambda: None, entry=lambda _: {}))
        with patch.object(durable_jobs, "enabled", return_value=True), \
             patch.object(durable_jobs, "_connect", lambda **_: self._connect()), \
             patch.object(projection, "_open_connection", lambda *_: self._connect()), \
             patch.object(jobs.workspace, "load"), patch.object(jobs.workspace, "get", return_value=ctx.config), \
             patch.object(jobs, "ObjectContext", return_value=ctx), \
             patch.object(reconciler.sam3_queue, "public", return_value=None), \
             patch.object(reconciler, "derive_pipeline_source", side_effect=derive or (lambda *_: self.source)):
            yield

    def test_stale_token_and_cancelled_or_expired_lease_never_reconcile(self):
        mutations = ["cancel_requested=TRUE", "lease_expires_at=now()-interval '1 minute'",
                     "lease_token='33333333-3333-3333-3333-333333333333'",
                     "kind='video_export'", "payload=jsonb_set(payload, '{object_id}', '\"other\"')",
                     "payload=jsonb_set(payload, '{video_id}', '\"other\"')"]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self._connect() as connection:
                    connection.execute("UPDATE jobs SET cancel_requested=FALSE, lease_expires_at=now()+interval '3 minutes', lease_token=%s::uuid, kind='pipeline_projection_reconcile', payload=%s::jsonb",
                                       (self.token, json.dumps(self.job["payload"])))
                    connection.execute("UPDATE jobs SET " + mutation)
                with self.execution(), patch.object(jobs, "reconcile_video") as reconcile:
                    with self.assertRaises(jobs.ProjectionRepairLeaseLost):
                        jobs.run_projection_reconcile(self.job)
                    reconcile.assert_not_called()

    def test_cancellation_during_metadata_read_prevents_any_projection_write(self):
        def derive(*_):
            # Preflight has finished, video fence is held, publication has not
            # started: another connection can still cancel the CPU job.
            with self._connect() as connection:
                connection.execute("SET LOCAL lock_timeout='500ms'")
                connection.execute("UPDATE jobs SET cancel_requested=TRUE WHERE id=%s::uuid", (self.job["id"],))
            return self.source

        with self.execution(derive):
            with self.assertRaises(jobs.ProjectionRepairLeaseLost):
                jobs.run_projection_reconcile(self.job)
        with self._connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM video_pipeline_projection_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM video_pipeline_projection").fetchone()[0], 0)

    def test_publication_holds_owned_job_row_until_projection_commits(self):
        apply = reconciler.apply_intent

        def guarded_apply(*args, **kwargs):
            with self.assertRaises(self.psycopg.errors.LockNotAvailable):
                with self._connect() as connection:
                    connection.execute("SELECT id FROM jobs WHERE id=%s::uuid FOR UPDATE NOWAIT", (self.job["id"],))
            return apply(*args, **kwargs)

        with self.execution(), patch.object(reconciler, "apply_intent", guarded_apply):
            result = jobs.run_projection_reconcile(self.job)
        with self._connect() as connection:
            connection.execute("SELECT id FROM jobs WHERE id=%s::uuid FOR UPDATE NOWAIT", (self.job["id"],))
            row = connection.execute("SELECT event_seq, snapshot->>'pipeline_stage' FROM video_pipeline_projection").fetchone()
        self.assertEqual(row, (result["projection_event_seq"], "triage"))
