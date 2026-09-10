from __future__ import annotations

import os
import unittest
import uuid

from server.pipeline_projection import (
    apply_intent,
    fail_intent,
    get_many,
    pending_intents,
    reserve_intent,
)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL ausente")
class PipelineProjectionPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        self.psycopg = psycopg
        self.url = os.environ["TEST_DATABASE_URL"]
        self.schema = "pipeline_projection_test_" + uuid.uuid4().hex
        migration = (
            __import__("pathlib").Path(__file__).resolve().parents[4]
            / "migrations"
            / "005_video_pipeline_projection.sql"
        ).read_text(encoding="utf-8")
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(f'CREATE SCHEMA "{self.schema}"')
            connection.execute(f'SET search_path TO "{self.schema}"')
            connection.execute(migration)

    def tearDown(self) -> None:
        with self.psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def _connect(self):
        return self.psycopg.connect(
            self.url,
            options=f"-c search_path={self.schema}",
            connect_timeout=5,
        )

    def test_newer_event_wins_when_events_apply_in_reverse_order(self) -> None:
        old = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="annotation_saved",
            source_identity={"annotation_revision": 1},
            connect=self._connect,
        )
        new = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="annotation_saved",
            source_identity={"annotation_revision": 2},
            connect=self._connect,
        )
        self.assertIsNotNone(old)
        self.assertIsNotNone(new)

        apply_intent(new, {"pipeline_stage": "sam3", "revision": 2}, connect=self._connect)
        with self._connect() as connection:
            old_status = connection.execute(
                "SELECT status FROM video_pipeline_projection_events WHERE event_seq=%s",
                (old.event_seq,),
            ).fetchone()[0]
        self.assertEqual(old_status, "superseded")
        current = apply_intent(
            old,
            {"pipeline_stage": "triage", "revision": 1},
            connect=self._connect,
        )

        self.assertEqual(current.event_seq, new.event_seq)
        self.assertEqual(current.snapshot["revision"], 2)
        with self._connect() as connection:
            statuses = dict(
                connection.execute(
                    "SELECT event_seq, status FROM video_pipeline_projection_events"
                ).fetchall()
            )
        self.assertEqual(statuses[old.event_seq], "superseded")
        self.assertEqual(statuses[new.event_seq], "applied")

    def test_same_source_reservation_is_idempotent_and_pending_is_retryable(self) -> None:
        first = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="review_saved",
            source_identity={"review_manifest_sha256": "a" * 64},
            connect=self._connect,
        )
        retry = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="review_saved",
            source_identity={"review_manifest_sha256": "a" * 64},
            connect=self._connect,
        )

        self.assertEqual(first.event_seq, retry.event_seq)
        self.assertEqual(retry.status, "pending")

        failed = fail_intent(
            retry,
            "postgresql://user:database-secret@postgres/db token=worker-secret",
            connect=self._connect,
        )
        self.assertEqual(failed.status, "pending")
        self.assertEqual(failed.attempts, 1)
        self.assertNotIn("database-secret", failed.error)
        self.assertNotIn("worker-secret", failed.error)
        self.assertEqual(
            [item.event_seq for item in pending_intents(connect=self._connect)],
            [first.event_seq],
        )

        applied = apply_intent(
            retry,
            {"pipeline_stage": "completed", "complete": True},
            connect=self._connect,
        )
        rows = get_many("boom", ["video-1", "missing"], connect=self._connect)
        self.assertEqual(rows["video-1"], applied)
        self.assertNotIn("missing", rows)


if __name__ == "__main__":
    unittest.main()
