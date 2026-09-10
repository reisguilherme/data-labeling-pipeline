from __future__ import annotations
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from server import durable_jobs, pipeline_projection as projection
from server.tests import test_pipeline_projection_postgres as fixtures


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL ausente")
class ProjectionListingPostgresTests(unittest.TestCase):
    setUp = fixtures.PipelineProjectionPostgresTests.setUp
    tearDown = fixtures.PipelineProjectionPostgresTests.tearDown
    _connect = fixtures.PipelineProjectionPostgresTests._connect

    def test_bulk_lookup_reports_pending_before_first_projection_exists(self):
        projection.reserve_intent(object_id="boom", video_id="first", event_kind="mutation",
                                  source_identity={"revision": 1}, connect=self._connect)
        rows = projection.get_many("boom", ["first", "missing"], connect=self._connect)
        self.assertIn("first", rows)
        self.assertEqual(rows["first"].projection_status, "pending")
        self.assertIsNone(rows["first"].projected_at)
        self.assertEqual(rows["first"].snapshot, {})
        self.assertNotIn("missing", rows)

    def test_bulk_lookup_observes_new_pending_event_even_for_same_identity(self):
        first = projection.reserve_intent(object_id="boom", video_id="v", event_kind="reconcile",
                                          source_identity={"revision": 1}, connect=self._connect)
        projection.apply_intent(first, {"pipeline_stage": "triage"}, connect=self._connect)
        before = projection.get_many("boom", ["v"], connect=self._connect)["v"]
        self.assertEqual(getattr(before, "projection_status", None), "current")
        projection.reserve_intent(object_id="boom", video_id="v", event_kind="mutation",
                                  source_identity={"revision": 1}, connect=self._connect)
        after = projection.get_many("boom", ["v"], connect=self._connect)["v"]
        self.assertEqual(after.projection_status, "pending")

    def test_bulk_enqueue_is_bounded_idempotent_and_preserves_active_lease(self):
        migration = Path(__file__).resolve().parents[4] / "migrations" / "001_initial.sql"
        with self._connect() as connection:
            connection.execute(migration.read_text(encoding="utf-8"))
        requests = [{"video_id": f"v-{n}", "source_identity": {"revision": 1}} for n in range(100)]
        with patch.object(durable_jobs, "enabled", return_value=True), patch.object(durable_jobs, "_connect", lambda **_: self._connect()):
            self.assertEqual(durable_jobs.enqueue_projection_reconciles("boom", requests), 20)
            with self._connect() as connection:
                connection.execute("UPDATE jobs SET state='running', attempts=2, lease_token='11111111-1111-1111-1111-111111111111'")
            self.assertEqual(durable_jobs.enqueue_projection_reconciles("boom", requests), 0)
            with self._connect() as connection:
                rows = connection.execute("SELECT kind, state::text, attempts, payload, lease_token::text FROM jobs").fetchall()
                self.assertEqual(len(rows), 20)
                self.assertTrue(all(row[0:3] == ('pipeline_projection_reconcile', 'running', 2) for row in rows))
                self.assertTrue(all(set(row[3]) == {"object_id", "video_id", "source_identity"} for row in rows))
                self.assertTrue(all(row[4] == '11111111-1111-1111-1111-111111111111' for row in rows))
                connection.execute("UPDATE jobs SET state='error' WHERE payload->>'video_id'='v-0'")
            self.assertEqual(durable_jobs.enqueue_projection_reconciles("boom", requests), 1)
