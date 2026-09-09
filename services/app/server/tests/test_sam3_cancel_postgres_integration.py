from __future__ import annotations

import os
import unittest
import uuid

from server.sam3_postgres import PostgresSam3Queue


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL ausente")
class PostgresSam3CancellationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        self.psycopg = psycopg
        self.url = os.environ["TEST_DATABASE_URL"]
        self.schema = "sam3_cancel_test_" + uuid.uuid4().hex
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(f'CREATE SCHEMA "{self.schema}"')
            connection.execute(
                f'CREATE TYPE "{self.schema}".job_state AS ENUM '
                "('queued','leased','running','done','error','cancelled')"
            )
            connection.execute(
                f'''CREATE TABLE "{self.schema}".jobs (
                    id uuid PRIMARY KEY,
                    kind text NOT NULL DEFAULT 'sam3_propagation',
                    worker_kind text NOT NULL DEFAULT 'gpu',
                    payload jsonb NOT NULL,
                    idempotency_key text UNIQUE,
                    priority integer NOT NULL DEFAULT 50,
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
        self.queue = PostgresSam3Queue(self.url)
        self.queue._connect = self._connect

    def tearDown(self) -> None:
        with self.psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def _connect(self):
        return self.psycopg.connect(
            self.url,
            options=f"-c search_path={self.schema}",
        )

    def _insert_cancelled_lease(
        self, *, expired: bool, attempts: int = 1
    ) -> tuple[uuid.UUID, uuid.UUID]:
        job_id = uuid.uuid4()
        token = uuid.uuid4()
        interval = "-1 second" if expired else "30 seconds"
        with self._connect() as connection:
            connection.execute(
                f'''INSERT INTO jobs
                    (id, payload, state, worker_id, lease_token, lease_expires_at,
                     cancel_requested, attempts, max_attempts)
                    VALUES (%s, %s::jsonb, 'running', 'worker', %s,
                            now() + interval '{interval}', true, %s, 3)''',
                (
                    job_id,
                    '{"object_id":"boom","video_id":"video-1","relpath":"clip.mp4"}',
                    token,
                    attempts,
                ),
            )
        return job_id, token

    def test_cancel_wins_worker_error_instead_of_requeueing(self) -> None:
        job_id, token = self._insert_cancelled_lease(expired=False)

        found = self.queue.finish(
            str(token),
            state="error",
            result={"partial": True},
            error="worker failed",
        )

        self.assertIsNotNone(found)
        self.assertEqual(found[1].state, "cancelled")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state::text, result, error, lease_token, cancel_requested, "
                "finished_at IS NOT NULL FROM jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
        self.assertEqual(
            row, ("cancelled", None, "worker failed", None, True, True)
        )

    def test_cancel_wins_expired_lease_on_retry_and_last_attempt(self) -> None:
        for attempts in (1, 3):
            with self.subTest(attempts=attempts):
                job_id, _ = self._insert_cancelled_lease(
                    expired=True, attempts=attempts
                )

                self.assertIsNone(self.queue.take("another-worker", 180))

                with self._connect() as connection:
                    row = connection.execute(
                        "SELECT state::text, lease_token, cancel_requested, "
                        "finished_at IS NOT NULL FROM jobs WHERE id=%s",
                        (job_id,),
                    ).fetchone()
                self.assertEqual(row, ("cancelled", None, True, True))


if __name__ == "__main__":
    unittest.main()
