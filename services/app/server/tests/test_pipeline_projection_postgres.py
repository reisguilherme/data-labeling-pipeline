from __future__ import annotations

import os
import threading
import unittest
import uuid
from pathlib import Path

from server.pipeline_projection import (
    apply_intent,
    fail_intent,
    get_many,
    pending_intents,
    reserve_intent,
    reserve_repair_intent,
)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL ausente")
class PipelineProjectionPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        self.psycopg = psycopg
        self.url = os.environ["TEST_DATABASE_URL"]
        self.schema = "pipeline_projection_test_" + uuid.uuid4().hex
        migration = (
            Path(__file__).resolve().parents[4]
            / "migrations"
            / "005_video_pipeline_projection.sql"
        ).read_text(encoding="utf-8")
        self.migration = migration
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

    def test_same_identity_can_be_reserved_again_after_it_was_superseded(self) -> None:
        first_a = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="reconcile",
            source_identity={"identity": "a"},
            connect=self._connect,
        )
        b = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="reconcile",
            source_identity={"identity": "b"},
            connect=self._connect,
        )
        apply_intent(first_a, {"identity": "a"}, connect=self._connect)
        apply_intent(b, {"identity": "b"}, connect=self._connect)

        second_a = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="reconcile",
            source_identity={"identity": "a"},
            connect=self._connect,
        )
        current = apply_intent(
            second_a,
            {"identity": "a-restored"},
            connect=self._connect,
        )

        self.assertGreater(second_a.event_seq, b.event_seq)
        self.assertEqual(current.event_seq, second_a.event_seq)
        self.assertEqual(current.snapshot["identity"], "a-restored")

    def test_repair_is_newer_than_an_unpublished_pending_mutation(self) -> None:
        source_a = {"identity": "a"}
        snapshot_a = {"pipeline_stage": "completed", "complete": True}
        applied_a = reserve_intent(
            object_id="boom", video_id="video-1", event_kind="reconcile",
            source_identity=source_a, connect=self._connect,
        )
        apply_intent(applied_a, snapshot_a, connect=self._connect)
        pending_b = reserve_intent(
            object_id="boom", video_id="video-1", event_kind="mutation",
            source_identity={"identity": "b"}, connect=self._connect,
        )

        repair, current = reserve_repair_intent(
            object_id="boom", video_id="video-1", source_identity=source_a,
            snapshot=snapshot_a, connect=self._connect,
        )

        self.assertIsNone(current)
        self.assertGreater(repair.event_seq, pending_b.event_seq)
        repaired = apply_intent(repair, snapshot_a, connect=self._connect)
        self.assertEqual(repaired.source_identity, source_a)
        with self._connect() as connection:
            state_b = connection.execute(
                "SELECT status FROM video_pipeline_projection_events WHERE event_seq=%s",
                (pending_b.event_seq,),
            ).fetchone()[0]
        self.assertEqual(state_b, "superseded")

    def test_repair_rewrites_corrupt_snapshot_then_becomes_idempotent(self) -> None:
        source = {"identity": "a"}
        expected = {"pipeline_stage": "completed", "complete": True}
        original = reserve_intent(
            object_id="boom", video_id="video-1", event_kind="reconcile",
            source_identity=source, connect=self._connect,
        )
        apply_intent(original, expected, connect=self._connect)
        with self._connect() as connection:
            connection.execute(
                "UPDATE video_pipeline_projection SET snapshot=%s::jsonb "
                "WHERE object_id='boom' AND video_id='video-1'",
                ('{"complete":false,"pipeline_stage":"broken"}',),
            )

        repair, current = reserve_repair_intent(
            object_id="boom", video_id="video-1", source_identity=source,
            snapshot=expected, connect=self._connect,
        )
        self.assertIsNone(current)
        self.assertGreater(repair.event_seq, original.event_seq)
        fixed = apply_intent(repair, expected, connect=self._connect)

        retry, unchanged = reserve_repair_intent(
            object_id="boom", video_id="video-1", source_identity=source,
            snapshot=expected, connect=self._connect,
        )
        self.assertIsNone(retry)
        self.assertEqual(unchanged.event_seq, fixed.event_seq)
        with self._connect() as connection:
            count = connection.execute(
                "SELECT count(*) FROM video_pipeline_projection_events "
                "WHERE object_id='boom' AND video_id='video-1'"
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_concurrent_repair_reservations_reuse_one_pending_event(self) -> None:
        source = {"identity": "a"}
        snapshot = {"pipeline_stage": "review", "complete": False}
        barrier = threading.Barrier(2)
        results = []

        def reserve() -> None:
            barrier.wait(timeout=5)
            results.append(
                reserve_repair_intent(
                    object_id="boom", video_id="video-1",
                    source_identity=source, snapshot=snapshot,
                    connect=self._connect,
                )[0]
            )

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(item is not None for item in results))
        self.assertEqual(results[0].event_seq, results[1].event_seq)

    def test_failed_intent_never_persists_unterminated_json_secret_tails(self) -> None:
        intent = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="reconcile",
            source_identity={"revision": 1},
            connect=self._connect,
        )
        for message in (
            r'''db rejected {"password":"\"SYNTHETIC_SECRET_TAIL}''',
            r"""db rejected {'token':'\'SYNTHETIC_SECRET_TAIL}""",
        ):
            with self.subTest(message=message):
                failed = fail_intent(intent, message, connect=self._connect)
                with self._connect() as connection:
                    persisted = connection.execute(
                        "SELECT last_error FROM video_pipeline_projection_events "
                        "WHERE event_seq = %s",
                        (intent.event_seq,),
                    ).fetchone()[0]

                self.assertNotIn("SYNTHETIC_SECRET_TAIL", persisted)
                self.assertIn("[REDACTED]", persisted)
                self.assertEqual(failed.error, persisted)
                self.assertEqual(failed.status, "pending")

    def test_pending_scan_wraps_to_older_failures_after_resume_cursor(self) -> None:
        older = reserve_intent(
            object_id="boom",
            video_id="video-older",
            event_kind="reconcile",
            source_identity={"revision": 1},
            connect=self._connect,
        )
        newer = reserve_intent(
            object_id="boom",
            video_id="video-newer",
            event_kind="reconcile",
            source_identity={"revision": 1},
            connect=self._connect,
        )
        fail_intent(older, "retry later", connect=self._connect)
        apply_intent(newer, {"complete": True}, connect=self._connect)

        resumed = pending_intents(
            after_event_seq=newer.event_seq,
            connect=self._connect,
        )

        self.assertEqual([item.event_seq for item in resumed], [older.event_seq])

    def test_migration_is_safe_to_replay_after_schema_marker_crash(self) -> None:
        with self.psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(f'SET search_path TO "{self.schema}"')
            connection.execute(self.migration)

        with self._connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT tablename
                      FROM pg_tables
                     WHERE schemaname = %s
                    """,
                    (self.schema,),
                ).fetchall()
            }
        self.assertIn("video_pipeline_projection_events", tables)
        self.assertIn("video_pipeline_projection", tables)

    def test_concurrent_applies_for_same_video_do_not_deadlock(self) -> None:
        old = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="annotation_saved",
            source_identity={"revision": 1},
            connect=self._connect,
        )
        new = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="annotation_saved",
            source_identity={"revision": 2},
            connect=self._connect,
        )
        old_event_locked = threading.Event()
        new_projection_written = threading.Event()
        start = threading.Barrier(2)
        errors: list[BaseException] = []

        class Cursor:
            def __init__(self, cursor, role: str) -> None:
                self._cursor = cursor
                self._role = role
                self._advisory_lock_seen = False

            def __enter__(self):
                self._cursor.__enter__()
                return self

            def __exit__(self, *args):
                return self._cursor.__exit__(*args)

            def execute(self, query, params=None):
                normalized = " ".join(str(query).split())
                if "pg_advisory_xact_lock" in normalized:
                    self._advisory_lock_seen = True
                result = self._cursor.execute(query, params)
                legacy_event_lock = (
                    not self._advisory_lock_seen
                    and "FROM video_pipeline_projection_events" in normalized
                    and "FOR UPDATE" in normalized
                )
                if legacy_event_lock and self._role == "old":
                    old_event_locked.set()
                    if not new_projection_written.wait(3):
                        raise AssertionError("new apply did not reach projection insert")
                elif legacy_event_lock and self._role == "new":
                    if not old_event_locked.wait(3):
                        raise AssertionError("old apply did not lock its event")
                if (
                    legacy_event_lock is False
                    and not self._advisory_lock_seen
                    and self._role == "new"
                    and normalized.startswith("INSERT INTO video_pipeline_projection ")
                ):
                    new_projection_written.set()
                return result

            def fetchone(self):
                return self._cursor.fetchone()

            def fetchall(self):
                return self._cursor.fetchall()

        class Connection:
            def __init__(self, connection, role: str) -> None:
                self._connection = connection
                self._role = role

            def __enter__(self):
                self._connection.__enter__()
                return self

            def __exit__(self, *args):
                return self._connection.__exit__(*args)

            def cursor(self):
                return Cursor(self._connection.cursor(), self._role)

        def run(role: str, intent, revision: int) -> None:
            def connect():
                raw = self.psycopg.connect(
                    self.url,
                    options=(
                        f"-c search_path={self.schema} "
                        "-c statement_timeout=5000 -c deadlock_timeout=100"
                    ),
                    connect_timeout=5,
                )
                return Connection(raw, role)

            try:
                start.wait(timeout=3)
                apply_intent(intent, {"revision": revision}, connect=connect)
            except BaseException as exc:  # collect the exact database failure
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=("old", old, 1), daemon=True),
            threading.Thread(target=run, args=("new", new, 2), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        current = get_many("boom", ["video-1"], connect=self._connect)["video-1"]
        self.assertEqual(current.event_seq, new.event_seq)
        self.assertEqual(current.snapshot["revision"], 2)


if __name__ == "__main__":
    unittest.main()
