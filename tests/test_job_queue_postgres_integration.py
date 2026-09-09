from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

CORE_SRC = Path(__file__).resolve().parents[1] / "packages" / "pipeline-core" / "src"
sys.path.insert(0, str(CORE_SRC))

from pipeline_core.jobs import PostgresJobQueue


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL ausente")
class PostgresLeaseExpiryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        self.psycopg = psycopg
        self.url = os.environ["TEST_DATABASE_URL"]
        self.schema = "lease_test_" + uuid.uuid4().hex
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(f'CREATE SCHEMA "{self.schema}"')
            connection.execute(
                f'CREATE TYPE "{self.schema}".job_state AS ENUM '
                "('queued','leased','running','done','error','cancelled')"
            )
            connection.execute(
                f'''CREATE TABLE "{self.schema}".jobs (
                    id uuid PRIMARY KEY,
                    kind text NOT NULL DEFAULT 'proxy_full',
                    worker_kind text NOT NULL DEFAULT 'cpu',
                    payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                    priority integer NOT NULL DEFAULT 40,
                    state "{self.schema}".job_state NOT NULL,
                    progress jsonb,
                    result jsonb,
                    error text,
                    worker_id text,
                    lease_token uuid,
                    lease_expires_at timestamptz,
                    cancel_requested boolean NOT NULL DEFAULT false,
                    attempts integer NOT NULL DEFAULT 1,
                    max_attempts integer NOT NULL DEFAULT 3,
                    started_at timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    finished_at timestamptz,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )'''
            )
            self.job_id = uuid.uuid4()
            self.token = uuid.uuid4()
            connection.execute(
                f'''INSERT INTO "{self.schema}".jobs
                    (id, state, lease_token, lease_expires_at)
                    VALUES (%s, 'running', %s, now() - interval '1 second')''',
                (self.job_id, self.token),
            )

    def tearDown(self) -> None:
        with self.psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def _connect(self):
        return self.psycopg.connect(
            self.url,
            options=f"-c search_path={self.schema}",
        )

    def test_expired_owner_cannot_heartbeat_progress_retry_or_finish(self) -> None:
        queue = PostgresJobQueue(self._connect)
        operations = (
            lambda: queue.heartbeat(str(self.job_id), str(self.token), lease_seconds=30),
            lambda: queue.update_progress(str(self.job_id), str(self.token), {"current": 1}),
            lambda: queue.retry_or_fail(str(self.job_id), str(self.token), "failed"),
            lambda: queue.finish(str(self.job_id), str(self.token), state="done"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(RuntimeError, "lease perdido"):
                    operation()

        with self._connect() as connection:
            row = connection.execute(
                "SELECT state::text, progress, result, error FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(row, ("running", None, None, None))

    def test_claim_reaps_expired_last_attempt_and_cancelled_jobs(self) -> None:
        queue = PostgresJobQueue(self._connect)
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET attempts=max_attempts WHERE id=%s", (self.job_id,)
            )
            connection.commit()

        self.assertIsNone(
            queue.claim(worker_id="test-worker", worker_kind="cpu", lease_seconds=30)
        )
        with self._connect() as connection:
            exhausted = connection.execute(
                "SELECT state::text, lease_token, finished_at IS NOT NULL "
                "FROM jobs WHERE id=%s",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(exhausted, ("error", None, True))

        cancelled_id = uuid.uuid4()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs "
                "(id, state, lease_token, lease_expires_at, cancel_requested) "
                "VALUES (%s, 'running', %s, now() - interval '1 second', true)",
                (cancelled_id, uuid.uuid4()),
            )
            connection.commit()

        self.assertIsNone(
            queue.claim(worker_id="test-worker", worker_kind="cpu", lease_seconds=30)
        )
        with self._connect() as connection:
            cancelled = connection.execute(
                "SELECT state::text, lease_token, finished_at IS NOT NULL "
                "FROM jobs WHERE id=%s",
                (cancelled_id,),
            ).fetchone()
        self.assertEqual(cancelled, ("cancelled", None, True))


if __name__ == "__main__":
    unittest.main()
